"""P6：compose_track_audio 切片批量重写必须与旧逐样本实现逐样本、逐统计字段等价。

覆盖：单段、多段首尾相接、中间空洞（静音）、重叠（含 32767 钳制与 overlap 计数的
「零点」边界语义）、源尾部不足（补静音统计）、乱序输入（按 valid 原顺序处理）。
参考实现为重构前的逐样本循环，直接对比输出 WAV 字节与返回的统计字典。
"""

import importlib.util
import wave
from array import array
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v4_compose_tests", ROOT / "subfix_generate_v4.py")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)

FPS = 25.0
RATE = 16000
SAMPLES_PER_FRAME = RATE / FPS  # 640


def _write_clip(directory: Path, name: str, samples: array) -> Path:
    path = directory / name
    v4._write_mono_pcm16(path, RATE, samples)
    return path


def _ramp(n: int, *, base: int = 1000, step: int = 1) -> array:
    return array("h", [base + i * step for i in range(n)])


def reference_compose(items, output_path, sample_rate=RATE):
    """重构前的旧实现（含校验/统计前导 + 逐样本混音），仅作测试基准。"""
    import math

    valid = []
    for raw_item in items or []:
        path = Path(raw_item.get("cut_path") or "")
        if not path.is_file():
            continue
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise ValueError(f"v4 轨道片段必须是 mono PCM16 WAV: {path.name}")
            if rate != sample_rate:
                raise ValueError(f"v4 轨道片段采样率必须为 {sample_rate}: {path.name}={rate}")
            sample_count = handle.getnframes()
        valid.append((dict(raw_item), path, sample_count))
    if not valid:
        raise ValueError("v4 没有可拼接的轨道音频")

    timeline_start = min(int(item.get("timeline_start_frame") or 0) for item, _p, _c in valid)
    fps_values = [float(item.get("fps") or item.get("batch_fps") or 30.0) for item, _p, _c in valid]
    fps = fps_values[0]
    if any(abs(value - fps) > 1e-4 for value in fps_values[1:]):
        raise ValueError("v4 同一轨道的时间线 FPS 不一致")
    source_audio_tail_truncated_count = 0
    source_audio_tail_silence_seconds = 0.0
    for item, path, sample_count in valid:
        if item.get("timeline_end_frame") is None:
            continue
        expected_frames = int(item["timeline_end_frame"]) - int(item.get("timeline_start_frame") or 0)
        actual_frames = int(round(sample_count / sample_rate * fps))
        tolerance = max(2, int(math.ceil(max(1, expected_frames) * 0.01)))
        if expected_frames <= 0:
            raise ValueError(f"v4 时间线音频片段跨度无效: {path.name}")
        if actual_frames - expected_frames > tolerance:
            raise ValueError(f"v4 音频时长超过时间线跨度，暂不支持重定时片段: {path.name}")
        if expected_frames - actual_frames > tolerance:
            source_audio_tail_truncated_count += 1
            source_audio_tail_silence_seconds += (expected_frames - actual_frames) / fps
    timeline_end = max(
        int(item["timeline_end_frame"])
        if item.get("timeline_end_frame") is not None
        else int(item.get("timeline_start_frame") or 0) + int(math.ceil(sample_count / sample_rate * fps))
        for item, _p, sample_count in valid
    )
    total_samples = max(1, int(math.ceil((timeline_end - timeline_start) / fps * sample_rate)))
    mixed = array("h", [0]) * total_samples
    overlap_sample_count = 0
    for item, path, _sample_count in valid:
        _rate, samples = v4._read_mono_pcm16(path)
        offset = int(round((int(item.get("timeline_start_frame") or 0) - timeline_start) / fps * sample_rate))
        for index, value in enumerate(samples):
            target = offset + index
            if target < 0 or target >= total_samples:
                continue
            if mixed[target] != 0:
                overlap_sample_count += 1
            mixed[target] = max(-32768, min(32767, mixed[target] + int(value)))
    v4._write_mono_pcm16(Path(output_path), sample_rate, mixed)
    return {
        "timeline_start_frame": timeline_start,
        "timeline_end_frame": timeline_end,
        "sample_rate": sample_rate,
        "item_count": len(valid),
        "overlap_sample_count": overlap_sample_count,
        "output_sample_count": len(mixed),
        "source_audio_tail_truncated_count": source_audio_tail_truncated_count,
        "source_audio_tail_silence_seconds": source_audio_tail_silence_seconds,
    }


def _item(path: Path, start_frame: int, end_frame):
    return {
        "cut_path": str(path),
        "timeline_start_frame": start_frame,
        "timeline_end_frame": end_frame,
        "fps": FPS,
    }


def _assert_equivalent(tmp_path: Path, items, *, sample_rate=RATE):
    ref_out = tmp_path / "ref.wav"
    new_out = tmp_path / "new.wav"
    ref_stats = reference_compose(
        [dict(item) for item in items], ref_out, sample_rate=sample_rate
    )
    new_stats = v4.compose_track_audio(
        [dict(item) for item in items], new_out, sample_rate=sample_rate
    )
    assert new_stats == ref_stats
    assert ref_out.read_bytes() == new_out.read_bytes()
    return new_stats


def test_single_segment(tmp_path):
    clip = _write_clip(tmp_path, "a.wav", _ramp(6400))  # 10 帧
    stats = _assert_equivalent(tmp_path, [_item(clip, 0, 10)])
    assert stats["item_count"] == 1
    assert stats["overlap_sample_count"] == 0
    assert stats["output_sample_count"] == 6400


def test_adjacent_segments(tmp_path):
    a = _write_clip(tmp_path, "a.wav", _ramp(6400, base=10))
    b = _write_clip(tmp_path, "b.wav", _ramp(6400, base=20000))
    c = _write_clip(tmp_path, "c.wav", _ramp(3200, base=-500, step=-1))
    stats = _assert_equivalent(tmp_path, [_item(a, 0, 10), _item(b, 10, 20), _item(c, 20, 25)])
    assert stats["overlap_sample_count"] == 0
    assert stats["output_sample_count"] == 16000


def test_gap_between_segments(tmp_path):
    a = _write_clip(tmp_path, "a.wav", _ramp(3200))
    b = _write_clip(tmp_path, "b.wav", _ramp(3200, base=7))
    # 0-5 帧，空洞 5-20 帧（9600 个零），20-25 帧
    stats = _assert_equivalent(tmp_path, [_item(a, 0, 5), _item(b, 20, 25)])
    assert stats["output_sample_count"] == 16000
    _rate, mixed = v4._read_mono_pcm16(tmp_path / "new.wav")
    assert all(value == 0 for value in mixed[3200:12800])


def test_overlap_with_clamp(tmp_path):
    # 两段在 10-20 帧区间重叠；大正值求和触发 32767 上钳
    a = _write_clip(tmp_path, "a.wav", array("h", [20000] * 12800))  # 20 帧, 0-20
    b = _write_clip(tmp_path, "b.wav", array("h", [20000] * 12800))  # 20 帧, 10-30
    stats = _assert_equivalent(tmp_path, [_item(a, 0, 20), _item(b, 10, 30)])
    assert stats["overlap_sample_count"] == 6400
    _rate, mixed = v4._read_mono_pcm16(tmp_path / "new.wav")
    assert all(value == 32767 for value in mixed[6400:12800])
    assert stats["output_sample_count"] == 19200


def test_overlap_zero_count_semantics(tmp_path):
    # 重叠区第一段写 0：旧实现里第二段写入前 mixed==0，不计 overlap——新实现必须一致
    a = _write_clip(tmp_path, "a.wav", array("h", [0] * 12800))
    b = _write_clip(tmp_path, "b.wav", array("h", [123] * 12800))
    stats = _assert_equivalent(tmp_path, [_item(a, 0, 20), _item(b, 10, 30)])
    assert stats["overlap_sample_count"] == 0
    _rate, mixed = v4._read_mono_pcm16(tmp_path / "new.wav")
    assert all(value == 123 for value in mixed[6400:12800])
    # 反序再验：先非零后含零，零点写入仍会计数（旧实现顺序语义）
    stats_rev = _assert_equivalent(tmp_path, [_item(b, 0, 20), _item(a, 10, 30)])
    assert stats_rev["overlap_sample_count"] == 6400


def test_negative_clamp_and_mixed_order(tmp_path):
    a = _write_clip(tmp_path, "a.wav", array("h", [-20000] * 12800))  # 0-20 帧
    b = _write_clip(tmp_path, "b.wav", array("h", [-20000] * 6400))   # 10-20 帧
    third = _write_clip(tmp_path, "c.wav", _ramp(6400, base=32000, step=-5))  # 20-30 帧
    # 乱序输入（后段先给）：overlap 计数按 valid 原顺序——b 先写、a 后写重叠区
    stats = _assert_equivalent(
        tmp_path,
        [_item(third, 20, 30), _item(b, 10, 20), _item(a, 0, 20)],
    )
    assert stats["overlap_sample_count"] == 6400
    _rate, mixed = v4._read_mono_pcm16(tmp_path / "new.wav")
    assert all(value == -20000 for value in mixed[0:6400])
    assert all(value == -32768 for value in mixed[6400:12800])
    assert mixed[12800] == 32000
    assert mixed[-1] == 32000 - 5 * 6399


def test_tail_silence_stats(tmp_path):
    # 片段跨度 30 帧但音频只有 20 帧（超出 1% 容差）：统计尾部补静音，输出仍铺满
    clip = _write_clip(tmp_path, "a.wav", _ramp(12800))
    stats = _assert_equivalent(tmp_path, [_item(clip, 0, 30)])
    assert stats["source_audio_tail_truncated_count"] == 1
    assert stats["source_audio_tail_silence_seconds"] == pytest.approx(10 / FPS)
    _rate, mixed = v4._read_mono_pcm16(tmp_path / "new.wav")
    assert len(mixed) == 19200
    assert all(value == 0 for value in mixed[12800:])


def test_nonzero_timeline_start(tmp_path):
    a = _write_clip(tmp_path, "a.wav", _ramp(6400))
    b = _write_clip(tmp_path, "b.wav", _ramp(6400, base=900))
    stats = _assert_equivalent(tmp_path, [_item(a, 100, 110), _item(b, 110, 120)])
    assert stats["timeline_start_frame"] == 100
    assert stats["timeline_end_frame"] == 120
    assert stats["output_sample_count"] == 12800
