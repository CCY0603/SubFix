"""外部 ASR 命令模板必须走 argv 数组（shell=False），Windows 下不得被引号/路径破坏。"""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("asr_argv_tests", ROOT / "subfix_asr_transcribe.py")
asr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(asr)


VALUES = {
    "audio": r"C:\Users\My User\Audio Files\cut 01.wav",
    "output": r"C:\Temp\transcribe out.json",
    "model": "mimo-asr-large",
    "language": "zh",
}


def test_windows_template_strips_quotes_and_keeps_paths_intact(monkeypatch):
    monkeypatch.setattr(asr.os, "name", "nt")
    template = '"C:\\Program Files\\mimo\\asr.exe" --audio "{audio}" --out "{output}" --model {model}'
    argv = asr._external_command_argv(template, VALUES)
    assert argv == [
        r"C:\Program Files\mimo\asr.exe",
        "--audio",
        VALUES["audio"],
        "--out",
        VALUES["output"],
        "--model",
        "mimo-asr-large",
    ]
    # 含空格/特殊字符的路径作为单个 argv 元素原样传递，不经过 cmd.exe 解释。
    assert any(" " in token for token in argv)
    assert all('"' not in token and "'" not in token for token in argv[:1])


def test_windows_backslash_not_swallowed_by_posix_split(monkeypatch):
    monkeypatch.setattr(asr.os, "name", "nt")
    argv = asr._external_command_argv(r"C:\tools\asr.exe {audio}", VALUES)
    assert argv[0] == r"C:\tools\asr.exe"
    assert argv[1] == VALUES["audio"]


def test_posix_template_uses_posix_split(monkeypatch):
    monkeypatch.setattr(asr.os, "name", "posix")
    argv = asr._external_command_argv(
        "/usr/local/bin/asr --audio '{audio}' --lang {language}",
        VALUES,
    )
    assert argv == ["/usr/local/bin/asr", "--audio", VALUES["audio"], "--lang", "zh"]


def test_unknown_placeholder_is_rejected(monkeypatch):
    monkeypatch.setattr(asr.os, "name", "nt")
    with pytest.raises(RuntimeError, match="外部 ASR 命令模板"):
        asr._external_command_argv('asr.exe --audio "{audio}" --bogus {secret}', VALUES)


def test_empty_template_is_rejected(monkeypatch):
    monkeypatch.setattr(asr.os, "name", "nt")
    with pytest.raises(RuntimeError, match="不能为空"):
        asr._external_command_argv("", VALUES)
