"""文稿匹配尾部坍缩切片重对齐 + clamp 硬化测试。

验证：
  * 对齐器尾部退化（时间戳坍缩）后，原本会被“按字数比例机械重排”覆盖；
    新逻辑对尾部被拒段单独切片重跑 Qwen3 强制对齐，用大模型相对时序替换机械分配。
  * clamp_last_row_to_speech_end 不再把片尾音乐/环境音误判的语音区外推进 speech_end。
  * 重对齐成功后 skip_redistribute 跳过二次机械重排。
"""

import importlib.util
import math
import wave
from array import array
from pathlib import Path

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


def make_row(start, end, text="二零二五年十月三十日", decision="accepted_local_match", fps=25.0, tl=0):
    return {
        "text": text,
        "start": round(start, 3),
        "end": round(end, 3),
        "start_frame": int(round(tl + start * fps)),
        "end_frame": int(round(tl + end * fps)),
        "row_remap_decision": decision,
    }


# ---------- clamp_last_row_to_speech_end 收紧 speech_end 外推 ----------

def test_trailing_music_not_over_extends_speech_end(tmp_path):
    # 语音 0.3-3.0s；随后 3s 静音 + 6.0-8.0s 片尾“音乐”（高幅，被 VAD 误判为语音区）。
    # 旧逻辑会把 speech_end 外推到 8.0s（gap=6s <= 15s 阈值），把尾部字幕窗口拉到音乐区；
    # 新逻辑仅当最后语音区与匹配区连续（间隔<=1.5s）才外推，故 speech_end 留在 3.0s。
    audio = tmp_path / "trailing-music.wav"
    write_audio(audio, [(0.3, 0), (2.7, 8000), (3.0, 0), (2.0, 8000)])
    rows = [make_row(0.3, 1.5)]

    diag = asr.clamp_last_row_to_speech_end(rows, audio, fps=25.0, timeline_start_frame=0)

    # 末行终点应跟随真实语音终点（~3.0s），而非被拉到 ~8.0s 的音乐区。
    assert diag is not None
    assert rows[0]["end_frame"] <= 80  # 3.0s*25 ≈ 75
    assert rows[0]["end_frame"] < 8 * 25 - 10


def test_trailing_speech_still_extends_when_contiguous(tmp_path):
    # 真实语音连续到 3.2s（无远距片尾噪声），speech_end 应外推到 3.2s。
    audio = tmp_path / "contiguous-end.wav"
    write_audio(audio, [(0.3, 0), (2.9, 8000), (0.3, 0)])
    rows = [make_row(0.3, 2.0)]

    diag = asr.clamp_last_row_to_speech_end(rows, audio, fps=25.0, timeline_start_frame=0)

    assert diag is not None
    assert 76 <= rows[0]["end_frame"] <= 82  # ~3.12s


def test_skip_redistribute_preserves_external_timings(tmp_path):
    # 重对齐已写入正确时间后，clamp 不应再二次机械重排覆盖它们。
    audio = tmp_path / "skip.wav"
    write_audio(audio, [(0.3, 0), (2.9, 8000), (0.3, 0)])
    rows = [make_row(0.3, 2.0), make_row(2.0, 2.0)]
    # 模拟重对齐结果：已给出合理尾部时间
    rows[1]["start"] = 2.4
    rows[1]["end"] = 3.1
    rows[1]["start_frame"] = int(2.4 * 25)
    rows[1]["end_frame"] = int(3.1 * 25)

    diag = asr.clamp_last_row_to_speech_end(
        rows, audio, fps=25.0, timeline_start_frame=0, skip_redistribute=True
    )

    # 跳过重排：时间应保持不变（不抛出、不覆盖）
    assert diag is None
    assert rows[1]["start"] == 2.4
    assert rows[1]["end"] == 3.1


# ---------- 尾部坍缩切片重对齐 ----------

def _mock_align_items_factory(combined_text, dur):
    """构造一个非坍缩的逐字时间戳列表，供 monkeypatch qwen3_force_align_items 使用。"""
    step = dur / max(1, len(combined_text))
    items = []
    for i, ch in enumerate(combined_text):
        items.append({
            "word": ch,
            "start": round(i * step, 4),
            "end": round((i + 1) * step, 4),
        })
    return items


def test_tail_realign_replaces_rejected_rows(tmp_path, monkeypatch):
    # 模拟：前部分 row 已对齐，尾部 2 行被对齐器坍缩拒收；
    # 重对齐应对尾部切片重跑对齐器并给出真实时间，替换机械分配。
    audio = tmp_path / "realign.wav"
    write_audio(audio, [(0.3, 0), (9.0, 8000), (0.3, 0)])

    anchor_text = "开头第一句"
    tail_texts = ["中间第二句", "结尾第三句"]
    combined = anchor_text + "。" + "".join(tail_texts)

    # 对齐器在切片内给出非坍缩、单调递增的时间戳
    def fake_align(audio_path, text, language):
        return _mock_align_items_factory(text, dur=8.5)

    monkeypatch.setattr(asr, "qwen3_force_align_items", fake_align)

    rows = [
        make_row(0.3, 2.0, text=anchor_text, decision="accepted_local_match"),
        make_row(2.0, 2.0, text=tail_texts[0], decision="rejected_low_score"),
        make_row(2.0, 2.0, text=tail_texts[1], decision="rejected_low_score"),
    ]

    info = asr._try_realign_tail_with_aligner(
        audio, rows, "zh", 25.0, 0, speech_end=9.0
    )

    assert info is not None
    assert len(info["rows"]) == 2
    # 行索引应为尾部被拒段（1, 2）
    idxs = [r[0] for r in info["rows"]]
    assert idxs == [1, 2]
    # 单调且落在切片窗口内（slice_start≈0.5, slice_end≈9.3）
    starts = [r[1] for r in info["rows"]]
    ends = [r[2] for r in info["rows"]]
    assert starts[0] < ends[0] <= starts[1] < ends[1]
    assert starts[0] >= 0.4
    assert ends[-1] <= 9.4
    # 末行终点应接近真实语音终点（9.0s），而非机械地全堆在坍缩点 2.0s
    assert ends[-1] >= 8.0


def test_tail_realign_falls_back_when_slice_collapsed(tmp_path, monkeypatch):
    # 切片内对齐器仍坍缩（所有 unit 共享同一时间戳）→ 放弃重对齐，回退原兜底。
    audio = tmp_path / "collapsed.wav"
    write_audio(audio, [(0.3, 0), (9.0, 8000), (0.3, 0)])

    anchor_text = "开头第一句"
    tail_texts = ["中间第二句", "结尾第三句"]
    combined = anchor_text + "。" + "".join(tail_texts)

    def fake_collapsed(audio_path, text, language):
        n = len(text)
        return [{"word": c, "start": 0.0, "end": 0.0} for c in text] if n else []

    monkeypatch.setattr(asr, "qwen3_force_align_items", fake_collapsed)

    rows = [
        make_row(0.3, 2.0, text=anchor_text, decision="accepted_local_match"),
        make_row(2.0, 2.0, text=tail_texts[0], decision="rejected_low_score"),
        make_row(2.0, 2.0, text=tail_texts[1], decision="rejected_low_score"),
    ]

    info = asr._try_realign_tail_with_aligner(
        audio, rows, "zh", 25.0, 0, speech_end=9.0
    )

    assert info is None  # 重对齐失败，走原 VAD 机械兜底


def test_tail_realign_skips_when_no_anchor(tmp_path, monkeypatch):
    # 没有已对齐锚点（被拒段从首行起）→ 无法切片重对齐，直接放弃。
    audio = tmp_path / "no-anchor.wav"
    write_audio(audio, [(0.3, 0), (9.0, 8000), (0.3, 0)])

    monkeypatch.setattr(
        asr, "qwen3_force_align_items",
        lambda p, t, l: _mock_align_items_factory(t, dur=8.5),
    )

    rows = [
        make_row(0.0, 0.0, text="首句被拒", decision="rejected_low_score"),
        make_row(0.0, 0.0, text="次句被拒", decision="rejected_low_score"),
    ]

    info = asr._try_realign_tail_with_aligner(
        audio, rows, "zh", 25.0, 0, speech_end=9.0
    )

    assert info is None
