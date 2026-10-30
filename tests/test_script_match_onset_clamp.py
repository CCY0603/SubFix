"""文稿匹配首行语音起点保护（clamp_first_row_to_speech_onset）。"""

import importlib.util
import math
from array import array
from pathlib import Path
import wave

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("subfix_asr_transcribe", ROOT / "subfix_asr_transcribe.py")
asr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(asr)


def write_audio(path, sections, sample_rate=16000):
    samples = array("h")
    for duration, amplitude in sections:
        samples.extend(
            int(amplitude * math.sin(i * 0.15))
            for i in range(round(duration * sample_rate))
        )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def make_row(start, end, fps=25.0, tl=0):
    return {
        "text": "二零二五年十月三十日",
        "start": round(start, 3),
        "end": round(end, 3),
        "start_frame": int(round(tl + start * fps)),
        "end_frame": int(round(tl + end * fps)),
    }


def test_leading_silence_pushes_first_row_to_onset(tmp_path):
    # 开头 0.6s 静音，对齐器却把首行首字压到 0 点；真实人声 0.6s 后才进入。
    audio = tmp_path / "leading-silence.wav"
    write_audio(audio, [(0.6, 0), (2.0, 8000), (0.3, 0)])
    rows = [make_row(0.0, 2.0)]

    diag = asr.clamp_first_row_to_speech_onset(rows, audio, fps=25.0, timeline_start_frame=0)

    assert diag and diag["first_row_onset_clamped"] is True
    # 夹紧到语音起点前约 80ms（0.52s ≈ 13 帧），容忍 VAD 10ms 窗量化。
    assert 10 <= rows[0]["start_frame"] <= 15
    assert rows[0]["end_frame"] == 50  # 结尾保持对齐结果不动
    assert rows[0]["onset_clamped"] is True


def test_timeline_start_offset_is_preserved(tmp_path):
    audio = tmp_path / "offset.wav"
    write_audio(audio, [(0.6, 0), (2.0, 8000), (0.3, 0)])
    tl = 90000
    rows = [make_row(0.0, 2.0, tl=tl)]

    asr.clamp_first_row_to_speech_onset(rows, audio, fps=25.0, timeline_start_frame=tl)

    assert tl + 10 <= rows[0]["start_frame"] <= tl + 15


def test_quiet_leading_speech_is_not_delayed(tmp_path):
    # 轻声开场（300 级）随后才是大声段落：VAD 必须收得到轻声，首行不得被推后。
    audio = tmp_path / "quiet-onset.wav"
    write_audio(audio, [(0.3, 0), (0.5, 300), (1.5, 8000), (0.3, 0)])
    rows = [make_row(0.3, 2.0)]
    original_frame = rows[0]["start_frame"]

    diag = asr.clamp_first_row_to_speech_onset(rows, audio, fps=25.0, timeline_start_frame=0)

    assert diag is None
    assert rows[0]["start_frame"] == original_frame


def test_small_gap_within_tolerance_is_left_alone(tmp_path):
    # 对齐起点只比语音早 100ms（<250ms 阈值），属于正常字头提前，不动。
    audio = tmp_path / "small-gap.wav"
    write_audio(audio, [(0.6, 0), (2.0, 8000), (0.3, 0)])
    rows = [make_row(0.5, 2.0)]
    original_frame = rows[0]["start_frame"]

    diag = asr.clamp_first_row_to_speech_onset(rows, audio, fps=25.0, timeline_start_frame=0)

    assert diag is None
    assert rows[0]["start_frame"] == original_frame


def test_unrelated_opening_noise_does_not_grab_first_row(tmp_path):
    # 开头一声短促噪声（时长不足 VAD 最小区段，应被滤除），不影响首行夹紧判断。
    audio = tmp_path / "blip.wav"
    write_audio(audio, [(0.2, 0), (0.03, 9000), (0.5, 0), (1.5, 8000), (0.2, 0)])
    rows = [make_row(0.0, 2.0)]

    diag = asr.clamp_first_row_to_speech_onset(rows, audio, fps=25.0, timeline_start_frame=0)

    assert diag and diag["first_row_onset_clamped"] is True
    # 起点必须跟随真正的语音段（约 0.73s），而不是 0.2s 的短噪声。
    assert rows[0]["start_frame"] >= 14


def test_empty_inputs_return_none(tmp_path):
    audio = tmp_path / "silence.wav"
    write_audio(audio, [(1.0, 0)])
    assert asr.clamp_first_row_to_speech_onset([], audio, 25.0, 0) is None
    rows = [make_row(0.0, 0.5)]
    # 全静音：检测不到任何语音区，保持原样。
    assert asr.clamp_first_row_to_speech_onset(rows, audio, 25.0, 0) is None
    assert rows[0]["start_frame"] == 0


# ---------- 末行语音终点保护（clamp_last_row_to_speech_end）----------


def test_trailing_silence_extends_last_row_to_speech_end(tmp_path):
    # 语音从 0.3s 持续到 3.2s，对齐器却把末行末字停在 2.0s（早 1.2s > 1s 阈值）；
    # 末行未被压缩（单行行长按语速算时长合理），仅把末行终点推到语音终点前 80ms。
    audio = tmp_path / "trailing-silence.wav"
    write_audio(audio, [(0.3, 0), (2.9, 8000), (0.3, 0)])
    rows = [make_row(0.3, 2.0)]

    diag = asr.clamp_last_row_to_speech_end(rows, audio, fps=25.0, timeline_start_frame=0)

    assert diag and diag["last_row_end_clamped"] is True
    # 语音终点约 3.2s，夹紧后约 3.2-0.08=3.12s ≈ 78 帧，容忍 VAD 10ms 窗量化。
    assert 76 <= rows[0]["end_frame"] <= 80
    assert rows[0]["start_frame"] == 8  # 起点保持对齐结果不动
    assert rows[0]["end_clamped"] is True


def test_last_row_timeline_offset_is_preserved(tmp_path):
    audio = tmp_path / "offset-end.wav"
    write_audio(audio, [(0.3, 0), (2.9, 8000), (0.3, 0)])
    tl = 90000
    rows = [make_row(0.3, 2.0, tl=tl)]

    asr.clamp_last_row_to_speech_end(rows, audio, fps=25.0, timeline_start_frame=tl)

    assert tl + 76 <= rows[0]["end_frame"] <= tl + 80


def test_small_end_gap_within_tolerance_is_left_alone(tmp_path):
    # 语音到 2.3s 结束，对齐终点 2.1s，仅早 200ms（<1.0s 阈值），属于正常字尾滞后，不动。
    audio = tmp_path / "small-end-gap.wav"
    write_audio(audio, [(0.3, 0), (2.0, 8000), (0.3, 0)])
    rows = [make_row(0.3, 2.1)]
    original_frame = rows[0]["end_frame"]

    diag = asr.clamp_last_row_to_speech_end(rows, audio, fps=25.0, timeline_start_frame=0)

    assert diag is None
    assert rows[0]["end_frame"] == original_frame


def test_unrelated_trailing_noise_does_not_grab_last_row(tmp_path):
    # 结尾一声短促噪声（时长不足 VAD 最小区段，应被滤除），不影响末行夹紧判断。
    audio = tmp_path / "trailing-blip.wav"
    write_audio(audio, [(0.3, 0), (1.7, 8000), (0.5, 0), (0.03, 9000), (0.2, 0)])
    rows = [make_row(0.3, 2.0)]

    diag = asr.clamp_last_row_to_speech_end(rows, audio, fps=25.0, timeline_start_frame=0)

    # 末行终点必须跟随真正的语音段（约 2.0s），而不是末尾 0.03s 短噪声（约 2.5s）。
    assert diag is None or rows[0]["end_frame"] <= 52


def test_first_row_starting_late_is_pulled_to_onset(tmp_path):
    # 语音从 0.5s 开始，但首行被对齐器排到了 2.0s（晚了 1.5s），应拉回到语音起点。
    audio = tmp_path / "late-first-row.wav"
    write_audio(audio, [(0.5, 0), (2.0, 8000), (0.3, 0)])
    rows = [make_row(2.0, 3.0)]

    diag = asr.clamp_first_row_to_speech_onset(rows, audio, fps=25.0, timeline_start_frame=0)

    assert diag and diag.get("first_row_pulled_earlier") is True
    # 拉回到语音起点前约 80ms（0.5 - 0.08 = 0.42s ≈ 10.5 帧）。
    assert 8 <= rows[0]["start_frame"] <= 13
    assert rows[0]["onset_clamped"] is True


def test_first_row_small_late_gap_is_left_alone(tmp_path):
    # 首行只比语音起点晚 300ms（<=300ms 阈值），不动。
    audio = tmp_path / "small-late-gap.wav"
    write_audio(audio, [(0.5, 0), (2.0, 8000), (0.3, 0)])
    rows = [make_row(0.8, 2.0)]
    original_frame = rows[0]["start_frame"]

    diag = asr.clamp_first_row_to_speech_onset(rows, audio, fps=25.0, timeline_start_frame=0)

    assert diag is None
    assert rows[0]["start_frame"] == original_frame


def test_leading_rejected_rows_left_align_to_speech_onset():
    # 首行被拒、第二行已接受在 4.0s。传入 speech_onset=0.5 后，首行应左对齐到 0.5s，
    # 而非像旧逻辑那样右对齐到第二行前面（~3.4s）。
    fps = 25.0
    tl = 0
    rej = asr.preserved_row_segment(
        {"index": 1, "start_frame": 0, "end_frame": 25, "text": "abcd"}, fps, tl,
        "rejected_low_score", 0.0, "", "abcd",
    )
    acc = {
        "index": 2, "start": 4.0, "end": 5.0,
        "ctc_start_frame": 100, "ctc_end_frame": 125,
        "ctc_char_count": 10, "row_remap_decision": "accepted_local_match",
    }
    segs = [dict(rej), dict(acc)]
    asr.repair_rejected_row_timings(segs, fps, tl, speech_onset=0.5)
    # 首行起点应接近 0.5s（12.5 帧），而非 3.4s。
    assert 10 <= segs[0]["ctc_start_frame"] <= 16
    assert segs[0]["ctc_end_frame"] < segs[1]["ctc_start_frame"]


def test_tail_rejected_rows_left_align_no_leading_gap():
    # 尾部被拒段（ASR 坍缩区）应左对齐到坍缩点，而非右对齐到 speech_end。
    # 场景：1 条 accepted（0-20s）+ 2 条 rejected 尾部行，坍缩点 20s，语音终点 35s。
    # 旧逻辑右对齐会让尾部行从 ~30s 才开始，20-30s 语音无字幕；
    # 新逻辑左对齐从 20s 开始铺。
    fps = 25.0
    tl = 0
    acc = {
        "index": 1, "start": 0.0, "end": 20.0,
        "ctc_start_frame": 0, "ctc_end_frame": 500,
        "ctc_char_count": 60, "row_remap_decision": "accepted_local_match",
    }
    rej1 = asr.preserved_row_segment(
        {"index": 2, "start_frame": 0, "end_frame": 25, "text": "abcdefghij"}, fps, tl,
        "rejected_low_score", 0.0, "", "abcdefghij",
    )
    rej2 = asr.preserved_row_segment(
        {"index": 3, "start_frame": 0, "end_frame": 25, "text": "klmnopqrst"}, fps, tl,
        "rejected_low_score", 0.0, "", "klmnopqrst",
    )
    segs = [dict(acc), dict(rej1), dict(rej2)]
    asr.repair_rejected_row_timings(
        segs, fps, tl, collapse_timestamp=20.0, speech_end=35.0, speech_onset=0.0,
    )
    # 尾部首行（rej1）起点应接近坍缩点 20s（500 帧），而非右对齐到 ~30s。
    assert 495 <= segs[1]["ctc_start_frame"] <= 510
    # 尾部末行（rej2）终点应 <= speech_end（35s = 875 帧）。
    assert segs[2]["ctc_end_frame"] <= 880
    # 两行连续无重叠。
    assert segs[1]["ctc_end_frame"] <= segs[2]["ctc_start_frame"]
