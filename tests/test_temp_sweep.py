"""TEMP 残留清扫器：只删 TEMP 根下小写 subfix_ 前缀且超过阈值的目录。"""

import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "asr_temp_sweep_tests", ROOT / "subfix_asr_transcribe.py"
)
asr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(asr)

DAY = 86400.0
NOW = 1_700_000_000.0


def _make(path: Path, mtime_days_ago: float) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    child = path / "payload.txt"
    child.write_text("x", encoding="utf-8")
    ts = NOW - mtime_days_ago * DAY
    os.utime(str(child), (ts, ts))
    os.utime(str(path), (ts, ts))
    return path


def test_sweep_removes_only_old_subfix_dirs(tmp_path):
    old_dir = _make(tmp_path / "subfix_old_aaa", 10)
    fresh_dir = _make(tmp_path / "subfix_new_bbb", 1)
    other_prefix = _make(tmp_path / "other_old_ccc", 10)
    capital_prefix = _make(tmp_path / "SubFixDumps", 10)

    # 前缀匹配但不是目录：必须保留
    old_file = tmp_path / "subfix_just_a_file"
    old_file.write_text("keep", encoding="utf-8")
    old_ts = NOW - 10 * DAY
    os.utime(str(old_file), (old_ts, old_ts))

    removed = asr.sweep_stale_temp_dirs(root=str(tmp_path), now=NOW)

    assert removed == 1
    assert not old_dir.exists()
    assert fresh_dir.exists()
    assert other_prefix.exists()
    assert capital_prefix.exists()
    assert old_file.exists()


def test_sweep_keeps_everything_within_threshold(tmp_path):
    _make(tmp_path / "subfix_recent_aaa", 0.5)
    _make(tmp_path / "subfix_recent_bbb", 6.9)

    removed = asr.sweep_stale_temp_dirs(root=str(tmp_path), now=NOW, max_age_days=7)

    assert removed == 0
    assert (tmp_path / "subfix_recent_aaa").exists()
    assert (tmp_path / "subfix_recent_bbb").exists()


def test_sweep_custom_max_age_boundary(tmp_path):
    eight_days = _make(tmp_path / "subfix_eight", 8)
    six_days = _make(tmp_path / "subfix_six", 6)

    removed = asr.sweep_stale_temp_dirs(root=str(tmp_path), now=NOW, max_age_days=7)

    assert removed == 1
    assert not eight_days.exists()
    assert six_days.exists()


def test_sweep_missing_root_returns_zero(tmp_path):
    assert asr.sweep_stale_temp_dirs(root=str(tmp_path / "does_not_exist"), now=NOW) == 0


def test_sweep_default_now_is_posix_time(tmp_path):
    # 不注入 now 时应使用真实时钟：刚创建的目录一定在阈值内。
    fresh = tmp_path / "subfix_fresh"
    fresh.mkdir()
    assert asr.sweep_stale_temp_dirs(root=str(tmp_path)) == 0
    assert fresh.exists()
