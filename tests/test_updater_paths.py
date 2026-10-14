"""更新器路径安全：POSIX 白名单、反斜杠归一化、穿越/绝对路径拒绝。"""

import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("updater_tests", ROOT / ".subfix_support" / "subfix_update.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def _build_archive(path: Path, files: list[str], *, entries: list[str] | None = None,
                   version: str = "1.2.3", payload: bytes = b"new-content"):
    """写一个测试更新 zip；entries 给定时 zip entry 与 manifest 可故意不一致。"""
    entries = entries if entries is not None else files
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            updater.MANIFEST_NAME,
            json.dumps({"schema_version": 1, "version": version, "files": files}),
        )
        for entry in entries:
            archive.writestr(entry, payload)


def _install(zip_path: Path, target_root: Path, version: str = "1.2.3"):
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    updater.install_archive(zip_path, digest, version, target_root=target_root)


def test_normalize_relative_path_converts_backslashes():
    assert updater.normalize_relative_path(r"SubFix\SubFix.lua") == "SubFix/SubFix.lua"
    assert updater.normalize_relative_path(r".subfix_support\bin\ffmpeg.exe") == (
        ".subfix_support/bin/ffmpeg.exe"
    )
    assert updater.normalize_relative_path("SubFix/SubFix.lua") == "SubFix/SubFix.lua"


def test_posix_manifest_entry_installs_on_windows(tmp_path):
    archive_path = tmp_path / "update.zip"
    target_root = tmp_path / "Utility"
    _build_archive(archive_path, ["SubFix/SubFix.lua"])
    _install(archive_path, target_root)
    installed = target_root / "SubFix" / "SubFix.lua"
    assert installed.read_bytes() == b"new-content"


def test_backslash_manifest_is_normalized_and_accepted(tmp_path):
    archive_path = tmp_path / "update.zip"
    target_root = tmp_path / "Utility"
    # manifest 用 Windows 反斜杠书写，zip entry 仍是标准 POSIX 形式。
    _build_archive(archive_path, [r"SubFix\SubFix.lua"], entries=["SubFix/SubFix.lua"])
    _install(archive_path, target_root)
    assert (target_root / "SubFix" / "SubFix.lua").is_file()


def test_ffmpeg_exe_is_whitelisted(tmp_path):
    archive_path = tmp_path / "update.zip"
    target_root = tmp_path / "Utility"
    _build_archive(archive_path, [".subfix_support/bin/ffmpeg.exe"])
    _install(archive_path, target_root)
    assert (target_root / ".subfix_support" / "bin" / "ffmpeg.exe").is_file()


@pytest.mark.parametrize(
    "bad_file",
    [
        "../evil.lua",
        "SubFix/../../evil.lua",
        "/SubFix/SubFix.lua",
        "C:/Windows/evil.lua",
        "pwned.txt",
    ],
)
def test_traversal_and_unauthorized_manifest_entries_rejected(tmp_path, bad_file):
    archive_path = tmp_path / "update.zip"
    # zip 内容与 manifest 保持一致，确保拒绝发生在白名单/路径校验阶段。
    _build_archive(archive_path, [bad_file], entries=[bad_file.lstrip("/")])
    with pytest.raises(updater.UpdateError):
        _install(archive_path, tmp_path / "Utility")


class _FakeInfo:
    def __init__(self, filename, directory=False):
        self.filename = filename
        self._directory = directory

    def is_dir(self):
        return self._directory


class _FakeArchive:
    """绕过 zipfile 自身的反斜杠清洗（ZipInfo 构造时会 replace），

    用以模拟外部工具产出的、local header 中带原始反斜杠条目的恶意更新包。
    """

    def __init__(self, manifest_bytes: bytes, entry_names: list[str]):
        self._manifest_bytes = manifest_bytes
        self._entry_names = entry_names

    def read(self, name):
        return self._manifest_bytes

    def infolist(self):
        return [_FakeInfo(name) for name in self._entry_names]


def test_backslash_zip_entry_is_rejected():
    manifest = json.dumps(
        {"schema_version": 1, "version": "1.2.3", "files": ["SubFix/SubFix.lua"]}
    ).encode("utf-8")
    archive = _FakeArchive(manifest, [r"SubFix\SubFix.lua"])
    with pytest.raises(updater.UpdateError, match="非法路径条目"):
        updater._safe_update_files(archive, "1.2.3")


def test_extra_archive_file_mismatches_manifest(tmp_path):
    archive_path = tmp_path / "update.zip"
    _build_archive(
        archive_path,
        ["SubFix/SubFix.lua"],
        entries=["SubFix/SubFix.lua", "evil.txt"],
    )
    with pytest.raises(updater.UpdateError, match="不一致"):
        _install(archive_path, tmp_path / "Utility")


def test_version_mismatch_rejected(tmp_path):
    archive_path = tmp_path / "update.zip"
    _build_archive(archive_path, ["SubFix/SubFix.lua"], version="9.9.9")
    with pytest.raises(updater.UpdateError, match="版本"):
        _install(archive_path, tmp_path / "Utility", version="1.2.3")


def test_windows_roots_include_support_only_for_user(monkeypatch, tmp_path):
    appdata = tmp_path / "Roaming"
    programdata = tmp_path / "ProgramData"
    appdata.mkdir()
    programdata.mkdir()
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("ProgramData", str(programdata))
    user_root, system_root = updater._resolve_utility_roots()
    expected_user = (
        appdata / "Blackmagic Design" / "DaVinci Resolve" / "Support"
        / "Fusion" / "Scripts" / "Utility"
    )
    expected_system = (
        programdata / "Blackmagic Design" / "DaVinci Resolve"
        / "Fusion" / "Scripts" / "Utility"
    )
    assert user_root == expected_user
    assert system_root == expected_system
    assert "Support" in user_root.parts
    assert "Support" not in system_root.parts
