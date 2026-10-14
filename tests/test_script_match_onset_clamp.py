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
