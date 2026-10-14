#!/usr/bin/env python3
"""构建 SubFix 在线更新包（zip + manifest + sha256）。

白名单直接复用 .subfix_support/subfix_update.py 的 UPDATE_FILE_PATHS，
保证「构建端允许打包什么」与「更新端允许安装什么」永远是同一个事实源，
避免发布包里混入口令外文件或更新器因白名单不一致而拒装。

产物命名与更新器 release_to_update_info() 的资源约定一致：
    SubFix-update-v<x.y.z>.zip
    SubFix-update-v<x.y.z>.sha256

用法：
    python scripts/build_update_package.py --version 3.0.1
    python scripts/build_update_package.py --version 3.0.1 --output dist \\
        --source f:/SubFix
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]

# 直接按文件路径加载更新器，避免 .subfix_support 不是 Python 包名的问题。
_updater_spec = importlib.util.spec_from_file_location(
    "subfix_update", REPO_ROOT / ".subfix_support" / "subfix_update.py"
)
assert _updater_spec is not None and _updater_spec.loader is not None
updater = importlib.util.module_from_spec(_updater_spec)
_updater_spec.loader.exec_module(updater)

# 平台二进制（ffmpeg / 许可文件）来自另外的构建流水线；源码仓库里通常没有。
# 默认缺失即跳过；显式 --require-all 时则报错退出。
OPTIONAL_UPDATE_PATHS = frozenset(
    {
        ".subfix_support/bin/ffmpeg",
        ".subfix_support/bin/ffmpeg.exe",
        ".subfix_support/licenses/FFmpeg-LGPL-2.1.txt",
    }
)

# 固定 zip 内时间戳，保证同内容构建出的包字节级可复现，便于核对。
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def resolve_source_file(source_root: Path, relative_path: str) -> Path | None:
    """把部署布局的相对路径映射回仓库源文件。

    部署布局固定为两级（``SubFix/SubFix.lua`` 与
    ``.subfix_support/<helper>``），而仓库是扁平布局：主入口与大部分
    Python helper 直接位于仓库根。因此依次尝试：
      1. <source>/<rel>                  （发布/展开布局）
      2. <source>/<去掉首段后的 rel>      （仓库扁平布局）
    """
    posix = PurePosixPath(relative_path)
    candidates = [source_root.joinpath(*posix.parts)]
    if len(posix.parts) > 1:
        candidates.append(source_root.joinpath(*posix.parts[1:]))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def collect_files(source_root: Path, require_all: bool) -> tuple[list[tuple[str, Path]], list[str]]:
    """返回 (将打包的 (部署相对路径, 源文件), 被跳过的可选项)。"""
    included: list[tuple[str, Path]] = []
    skipped: list[str] = []
    missing_required: list[str] = []
    for relative_path in sorted(updater.UPDATE_FILE_PATHS):
        source_file = resolve_source_file(source_root, relative_path)
        if source_file is not None:
            included.append((relative_path, source_file))
        elif relative_path in OPTIONAL_UPDATE_PATHS and not require_all:
            skipped.append(relative_path)
        else:
            missing_required.append(relative_path)
    if missing_required:
        details = "\n".join(f"  - {item}" for item in missing_required)
        raise SystemExit(f"缺少白名单要求的源文件：\n{details}")
    return included, skipped


def build_package(version: str, source_root: Path, output_dir: Path, require_all: bool) -> tuple[Path, Path]:
    normalized_version = updater.normalized_version(version)
    included, skipped = collect_files(source_root, require_all)
    file_list = [relative_path for relative_path, _ in included]

    stem = f"SubFix-update-v{normalized_version}"
    zip_path = output_dir / f"{stem}.zip"
    sha_path = output_dir / f"{stem}.sha256"
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": 1,
        "version": normalized_version,
        "files": file_list,
    }

    if zip_path.exists():
        zip_path.unlink()
    if sha_path.exists():
        sha_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_path, source_file in included:
            info = zipfile.ZipInfo(relative_path, date_time=_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            # zip 内统一外部属性为普通 0644 文件，避免平台差异。
            info.external_attr = 0o644 << 16
            archive.writestr(info, source_file.read_bytes())
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        manifest_info = zipfile.ZipInfo(updater.MANIFEST_NAME, date_time=_ZIP_DATE_TIME)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o644 << 16
        archive.writestr(manifest_info, manifest_bytes)

    # 用更新器自己的校验逻辑回归一遍：manifest 版本、白名单、条目一致性。
    with zipfile.ZipFile(zip_path) as archive:
        accepted = updater._safe_update_files(archive, normalized_version)
    if sorted(accepted) != file_list:
        raise SystemExit("更新器白名单校验结果与构建清单不一致，拒绝产出该包")

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")

    print(f"版本：{normalized_version}")
    print(f"已打包 {len(file_list)} 个文件：")
    for item in file_list:
        print(f"  + {item}")
    for item in skipped:
        print(f"  - 跳过可选项（源文件缺失）：{item}")
    print(f"更新包：{zip_path}")
    print(f"校验文件：{sha_path}")
    print(f"SHA-256：{digest}")
    return zip_path, sha_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 SubFix 在线更新包")
    parser.add_argument("--version", required=True, help="语义化版本号 x.y.z")
    parser.add_argument("--source", type=Path, default=REPO_ROOT, help="仓库/发布源根目录")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "dist", help="产物目录（默认 dist/）")
    parser.add_argument("--require-all", action="store_true", help="可选项（ffmpeg 等）缺失时也报错")
    args = parser.parse_args(argv)
    try:
        build_package(args.version, args.source.resolve(), args.output.resolve(), args.require_all)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
