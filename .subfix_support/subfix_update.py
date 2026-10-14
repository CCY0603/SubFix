#!/usr/bin/env python3
"""Safe GitHub Release updater for the SubFix Resolve scripts (macOS / Windows)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile


REPOSITORY = "CCY0603/SubFix"
RELEASE_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
def _resolve_utility_roots() -> "tuple[Path, Path]":
    if platform.system() == "Windows":
        appdata = Path(os.environ.get("APPDATA", ""))
        programdata = Path(os.environ.get("ProgramData", ""))
        # Resolve 的用户级脚本目录比全用户多一层 Support（与安装器、
        # SubFix.lua 的 subfix_dr_utility_root() 保持一致）：
        #   用户级：%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
        #   全用户：%ProgramData%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility
        user_rel = "Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Scripts\\Utility"
        system_rel = "Blackmagic Design\\DaVinci Resolve\\Fusion\\Scripts\\Utility"
        user = appdata / user_rel if str(appdata) else Path.home() / user_rel
        system = programdata / system_rel if str(programdata) else Path(programdata) / system_rel
        return user, system
    home = Path.home()
    rel = "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
    return home / rel, Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility")


USER_UTILITY_ROOT, SYSTEM_UTILITY_ROOT = _resolve_utility_roots()
MANIFEST_NAME = "subfix-update-manifest.json"
# 白名单、manifest、zip entry 一律使用 POSIX 相对路径（zip 标准分隔符为 "/"），
# 否则在 Windows 上 str(PurePath("SubFix/SubFix.lua")) 会变成反斜杠形式，
# 与 zip entry/白名单永远不相等，导致在线更新必然被判为「未授权文件」。
UPDATE_FILE_PATHS = frozenset(
    {
        "SubFix/SubFix.lua",
        ".subfix_support/subfix_generate_selection_core.lua",
        ".subfix_support/subfix_process_group.py",
        ".subfix_support/subfix_update.py",
        ".subfix_support/subfix_asr_transcribe.py",
        ".subfix_support/subfix_generate_v4.py",
        ".subfix_support/subfix_generate_v5.py",
        ".subfix_support/subfix_generate_textnorm.py",
        ".subfix_support/subfix_qwen_local_manager.py",
        ".subfix_support/bin/ffmpeg",
        ".subfix_support/bin/ffmpeg.exe",
        ".subfix_support/licenses/FFmpeg-LGPL-2.1.txt",
        ".subfix_support/setup_asr_env.sh",
        ".subfix_support/setup_asr_env.ps1",
        ".subfix_support/segmentation_profile.json",
        ".subfix_support/segmentation_profile_v3.json",
        ".subfix_support/segmentation_profile_v4.json",
    }
)


def normalize_relative_path(value: str) -> str:
    """Normalize manifest/zip relative paths to the POSIX form used in the whitelist."""
    text = str(value or "").replace("\\", "/")
    return PurePosixPath(text).as_posix()


class UpdateError(RuntimeError):
    pass


def parse_version(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip()
    if text.startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise UpdateError(f"版本号无效: {value}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def normalized_version(value: str) -> str:
    return ".".join(str(part) for part in parse_version(value))


def release_to_update_info(release: dict[str, Any], current_version: str) -> dict[str, Any]:
    if release.get("draft") is True or release.get("prerelease") is True:
        return {"ok": False, "error": "最新发布不是稳定版"}
    try:
        remote_version = normalized_version(str(release.get("tag_name") or ""))
        current = parse_version(current_version)
    except UpdateError as exc:
        return {"ok": False, "error": str(exc)}
    if parse_version(remote_version) <= current:
        return {"ok": False, "error": "当前已是最新稳定版", "version": remote_version}

    prefix = f"SubFix-update-v{remote_version}"
    assets = release.get("assets")
    if not isinstance(assets, list):
        return {"ok": False, "error": "Release 未提供更新资源"}
    asset_urls = {
        str(asset.get("name")): str(asset.get("browser_download_url"))
        for asset in assets
        if isinstance(asset, dict) and str(asset.get("browser_download_url") or "").startswith("https://")
    }
    zip_url = asset_urls.get(prefix + ".zip")
    sha256_url = asset_urls.get(prefix + ".sha256")
    if not zip_url or not sha256_url:
        return {"ok": False, "error": "Release 缺少更新 ZIP 或 SHA-256 校验文件"}
    return {
        "ok": True,
        "update_available": True,
        "version": remote_version,
        "name": str(release.get("name") or f"SubFix v{remote_version}"),
        "notes": str(release.get("body") or ""),
        "zip_url": zip_url,
        "sha256_url": sha256_url,
    }


def fetch_latest_release(timeout_seconds: int = 15) -> dict[str, Any]:
    request = Request(
        RELEASE_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "SubFix-Updater"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("GitHub 尚未发布首个稳定版本，请联系发布者") from exc
        raise UpdateError(f"无法读取 GitHub Release: HTTP {exc.code}") from exc
    except (URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"无法连接 GitHub Release: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("GitHub Release 响应格式错误")
    return payload


def download_to(url: str, destination: Path, timeout_seconds: int = 60, *,
                progress: Callable[[int, int | None], None] | None = None) -> None:
    if not str(url).startswith("https://"):
        raise UpdateError("更新资源地址必须使用 HTTPS")
    request = Request(url, headers={"User-Agent": "SubFix-Updater"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response, destination.open("wb") as output:
            if not str(response.geturl()).startswith("https://"):
                raise UpdateError("更新资源重定向到了非 HTTPS 地址")
            try:
                total = int(response.headers.get("Content-Length", ""))
            except (TypeError, ValueError):
                total = 0
            total = total if total > 0 else None
            downloaded = 0
            if progress:
                progress(downloaded, total)
            while chunk := response.read(256 * 1024):
                output.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)
            if total is not None and downloaded != total:
                raise UpdateError("更新包下载不完整，请重试")
    except (URLError, OSError) as exc:
        raise UpdateError(f"下载更新失败: {exc}") from exc


def read_sha256_file(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip().split(maxsplit=1)[0] if path.exists() else ""
    if len(token) != 64 or any(character not in "0123456789abcdefABCDEF" for character in token):
        raise UpdateError("SHA-256 校验文件格式错误")
    return token.lower()


def _safe_update_files(archive: zipfile.ZipFile, version: str) -> list[str]:
    try:
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("更新包缺少有效 manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise UpdateError("更新包 manifest 版本不支持")
    if normalized_version(str(manifest.get("version") or "")) != normalized_version(version):
        raise UpdateError("更新包版本与 Release 不一致")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or any(not isinstance(item, str) for item in files):
        raise UpdateError("更新包 manifest 文件列表错误")
    normalized_files = [normalize_relative_path(item) for item in files]
    if len(normalized_files) != len(set(normalized_files)) or any(
        item.startswith("/")
        or ":" in item
        or ".." in PurePosixPath(item).parts
        or item not in UPDATE_FILE_PATHS
        for item in normalized_files
    ):
        raise UpdateError("更新包包含未授权文件")
    archive_files = set()
    for info in archive.infolist():
        if info.is_dir():
            continue
        entry_name = info.filename
        # zip entry 必须就是 POSIX 相对路径；反斜杠/绝对路径条目一律拒绝，
        # 避免不同平台解压语义差异把文件写到白名单目录之外。
        if entry_name != normalize_relative_path(entry_name) or entry_name.startswith("/") or ":" in entry_name:
            raise UpdateError("更新包包含非法路径条目")
        archive_files.add(entry_name)
    if archive_files != {MANIFEST_NAME, *normalized_files}:
        raise UpdateError("更新包内容与 manifest 不一致")
    return normalized_files


def _safe_destination(target_root: Path, relative_path: str) -> Path:
    target_root.mkdir(parents=True, exist_ok=True)
    parent = target_root
    if parent.is_symlink():
        raise UpdateError("拒绝写入符号链接目录")
    # 仅允许在 Resolve 脚本目录的真实子目录中替换白名单文件。
    posix_path = PurePosixPath(relative_path)
    for part in posix_path.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise UpdateError(f"拒绝写入符号链接目录: {relative_path}")
        parent.mkdir(exist_ok=True)
    return parent / posix_path.name


def _remove_legacy_menu_within_root(root: Path) -> None:
    """Delete stale single-menu copies that live in the same Utility root.

    The current install keeps SubFix/SubFix.lua, so that live file is never
    touched here; only the retired 生成选区字幕 menu entry and the even older
    English launcher names are removed. An empty legacy ``SubFix`` menu
    directory is pruned as well; a directory still holding the live
    SubFix.lua (or any unknown file) is left untouched.
    """
    menu_directory = root / "SubFix"
    legacy_paths = (
        menu_directory / "生成选区字幕.lua",
        menu_directory / "SubFix_GenerateSelectionSubtitles.lua",
        root / "SubFix_GenerateSelectionSubtitles.lua",
        root / "SubFix.lua",
    )
    for legacy_path in legacy_paths:
        if legacy_path.is_file() or legacy_path.is_symlink():
            legacy_path.unlink()
    if menu_directory.is_dir() and not menu_directory.is_symlink():
        try:
            # 仅在目录已空时移除；含现役 SubFix.lua 或未知文件则保留，绝不扩大删除范围。
            menu_directory.rmdir()
        except OSError:
            pass


def cleanup_legacy_menu_files(target_root: Path) -> None:
    """Best-effort cleanup after a verified update succeeds.

    * Inside the updated root: remove the retired single-menu Lua entries.
    * Also sweep the *other* well-known root (user <-> system), because
      Resolve scans both script trees and a stale copy there keeps showing
      the retired standalone menu. Root-owned files may require admin and
      their failure must never fail a verified update.
    """
    candidate_roots = [Path(target_root)]
    if Path(target_root) == USER_UTILITY_ROOT:
        candidate_roots.append(SYSTEM_UTILITY_ROOT)
    elif Path(target_root) == SYSTEM_UTILITY_ROOT:
        candidate_roots.append(USER_UTILITY_ROOT)
    for root in candidate_roots:
        try:
            _remove_legacy_menu_within_root(root)
        except OSError:
            # 系统级残留可能需要管理员权限；不因旧副本清理失败而回滚已验证的更新。
            continue


def install_archive(archive_path: Path, expected_sha256: str, version: str, target_root: Path = USER_UTILITY_ROOT) -> None:
    actual_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual_sha256.lower() != str(expected_sha256).lower():
        raise UpdateError("SHA-256 校验失败，未安装更新")

    with zipfile.ZipFile(archive_path) as archive:
        files = _safe_update_files(archive, version)
        with tempfile.TemporaryDirectory(prefix="subfix_update_stage_") as temporary_dir:
            stage_root = Path(temporary_dir)
            for relative_path in files:
                staged_path = stage_root / relative_path
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_bytes(archive.read(relative_path))

            backups: dict[Path, bytes | None] = {}
            replaced: list[Path] = []
            try:
                for relative_path in files:
                    destination = _safe_destination(target_root, relative_path)
                    if destination.is_symlink():
                        raise UpdateError(f"拒绝覆盖符号链接: {relative_path}")
                    backups[destination] = destination.read_bytes() if destination.exists() else None
                    replacement = destination.with_name(destination.name + ".subfix-new")
                    shutil.copy2(stage_root / relative_path, replacement)
                    os.replace(replacement, destination)
                    if os.name != "nt" and relative_path == ".subfix_support/bin/ffmpeg":
                        destination.chmod(0o755)
                    replaced.append(destination)
                cleanup_legacy_menu_files(target_root)
            except Exception as exc:
                for destination in reversed(replaced):
                    original = backups[destination]
                    if original is None:
                        destination.unlink(missing_ok=True)
                    else:
                        destination.write_bytes(original)
                raise UpdateError(f"安装更新失败，已回滚: {exc}") from exc


def write_output(path: Path | None, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if path is None:
        print(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def write_progress(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    temporary = path.with_name(path.name + ".tmp")
    try:
        write_output(temporary, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SubFix GitHub Release 更新器")
    subparsers = parser.add_subparsers(dest="action", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--current-version", required=True)
    check.add_argument("--output", type=Path)
    install = subparsers.add_parser("install")
    install.add_argument("--zip-url", required=True)
    install.add_argument("--sha256-url", required=True)
    install.add_argument("--version", required=True)
    install.add_argument("--target-root", type=Path, default=USER_UTILITY_ROOT)
    install.add_argument("--output", type=Path)
    install.add_argument("--progress", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            payload = release_to_update_info(fetch_latest_release(), args.current_version)
            write_output(args.output, payload)
            return 0 if payload.get("ok") else 1
        with tempfile.TemporaryDirectory(prefix="subfix_update_download_") as temporary_dir:
            temp_root = Path(temporary_dir)
            archive_path = temp_root / "update.zip"
            checksum_path = temp_root / "update.sha256"
            def report_download(downloaded: int, total: int | None) -> None:
                amount = f"{downloaded / (1024 * 1024):.2f} MB"
                if total:
                    amount += f" / {total / (1024 * 1024):.2f} MB"
                write_progress(args.progress, {
                    "stage": "下载更新包", "message": f"正在下载更新包：{amount}",
                    "download_progress": True, "progress_index": downloaded,
                    "progress_total": total, "indeterminate": total is None,
                })

            write_progress(args.progress, {"stage": "连接下载服务器", "indeterminate": True})
            download_to(args.zip_url, archive_path, progress=report_download)
            write_progress(args.progress, {"stage": "校验并安装更新", "indeterminate": True})
            download_to(args.sha256_url, checksum_path)
            install_archive(archive_path, read_sha256_file(checksum_path), args.version, args.target_root)
        write_progress(args.progress, {"stage": "完成", "progress_index": 100, "progress_total": 100})
        write_output(args.output, {"ok": True, "version": normalized_version(args.version), "restart_required": True})
        return 0
    except (UpdateError, OSError, zipfile.BadZipFile) as exc:
        if getattr(args, "progress", None):
            try:
                write_progress(args.progress, {"stage": "失败", "message": str(exc)})
            except OSError:
                pass
        write_output(getattr(args, "output", None), {"ok": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
