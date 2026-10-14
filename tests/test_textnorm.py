"""概数（二三十/三四五条）不得被电话号规则误转。

回归：电话/编号 run 规则（连续 3+ 个位数字）先于量词规则执行，
把「三四五条/七八九条」误转成 345/789。修复后紧邻白名单量词的
数字 run 必须让位给量词主机制（概数安全阀保留原文）。
"""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("textnorm_tests", ROOT / "subfix_generate_textnorm.py")
textnorm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(textnorm)


@pytest.mark.parametrize(
    "text",
    [
        "二三十人",
        "三四十个",
        "七八个",
        "三四五条",
        "七八九条",
        "三四五六条",
        "几万块钱",
    ],
)
def test_approximate_numbers_are_preserved(text):
    assert textnorm.normalize_subtitle_text(text) == text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("四十五条", "45条"),
        ("七十八条", "78条"),
        ("一百二十公斤", "120公斤"),
        ("幺八幺八黄金眼", "1818黄金眼"),
    ],
)
def test_exact_numbers_still_convert(text, expected):
    assert textnorm.normalize_subtitle_text(text) == expected


def test_non_whitelisted_measure_still_treated_as_number_run():
    # 「页」不是白名单量词：三四五页 属于编号/页码 run，转 345 页是预期行为。
    assert textnorm.normalize_subtitle_text("三四五页") == "345页"
