"""v5 帧边界取整（half-up）、帧率缩放搜索窗、95 分位能量峰值。"""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v5_quant_tests", ROOT / "subfix_generate_v5.py")
v5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v5)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0.5, 1),
        (2.5, 3),   # 内建 round 是 banker's rounding，会给 2
        (-0.5, 0),
        (11.988012, 12),
    ],
)
def test_round_half_up(value, expected):
    assert v5._round_half_up(value) == expected


def test_round_half_up_differs_from_banker_rounding_at_2_5():
    assert round(2.5) == 2
    assert v5._round_half_up(2.5) == 3


@pytest.mark.parametrize(
    "fps,expected",
    [
        (24.0, 10),      # 9.6 -> 10
        (29.97003, 12),  # 11.988 -> 12
        (30.0, 12),
        (60.0, 24),
    ],
)
def test_search_frames_scale_with_fps(fps, expected):
    assert max(1, v5._round_half_up(0.4 * float(fps))) == expected


def test_single_spike_does_not_swallow_normal_speech_region():
    # 40 窗静音 + 20 窗正常语音（中间夹一个 30x 爆音）+ 40 窗静音。
    levels = [0.0] * 40 + [1000.0] * 10 + [30000.0] + [1000.0] * 9 + [0.0] * 40
    # 旧逻辑取 max 做峰值：on_threshold=4500，只有爆音本身越线，语音区丢失。
    # 95 分位峰值=1000：on_threshold=150，整段正常语音被保留。
    regions = v5._speech_regions(levels, 16000, 160, 30.0, 0)
    assert regions, "正常语音区被单个尖峰的阈值吞掉"
    start_frame, end_frame = regions[0]
    # 每个 160-sample 窗在 30fps 下为 0.3 帧；语音窗约为 index 40..59
    # （爆音在 index 50，即第 15 帧）。旧 max 逻辑只会圈住爆音本身（15-16），
    # 新逻辑必须把爆音前的正常语音起点（第 12 帧）纳入。
    assert start_frame <= 12
    assert end_frame >= 15
    assert end_frame - start_frame >= 3
