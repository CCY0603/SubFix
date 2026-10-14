"""v4 断句行必须带来源元数据（batch_id/track_order/track_index/item_index）
与非零 speaker_score_db，供 Lua 跨麦仲裁与条目归并。

回归：prepare_track_context_windows 已聚合轨道元数据，但 segment 行构造
只写了 speaker_track_index，其余字段全部丢失，Lua 读到的 speaker_score_db
恒为 nil（按 0 处理），分贝择优形同虚设。
"""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v4_metadata_tests", ROOT / "subfix_generate_v4.py")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)


def _unit(text, start, end, *, track_index, score, decision="",
          batch_id="doubao_track_2", track_order=3, item_index=7):
    return {
        "text": text,
        "start_frame": start,
        "end_frame": end,
        "track_index": track_index,
        "speaker_track_index": track_index,
        "speaker_score_db": score,
        "speaker_decision": decision,
        "batch_id": batch_id,
        "track_order": track_order,
        "item_index": item_index,
    }


REQUIRED_KEYS = (
    "batch_id",
    "track_order",
    "track_index",
    "item_index",
    "speaker_track_index",
    "speaker_score_db",
    "speaker_decision",
)


def test_single_track_rows_carry_provenance_and_nonzero_score():
    units = [
        _unit("你", 0, 10, track_index=2, score=-18.0),
        _unit("好", 10, 20, track_index=2, score=-16.0, decision="viterbi_exclusive"),
        _unit("世", 20, 30, track_index=2, score=-20.0),
        _unit("界", 30, 40, track_index=2, score=-17.5),
    ]
    rows, _ = v4.segment_canonical_units(units, "standard", None, 30.0)
    assert len(rows) == 1
    row = rows[0]
    for key in REQUIRED_KEYS:
        assert key in row, f"断句行缺少 {key}"
    assert row["batch_id"] == "doubao_track_2"
    assert row["track_order"] == 3
    assert row["item_index"] == 7
    assert row["track_index"] == 2
    assert row["speaker_track_index"] == 2
    # 中位数 [-20, -18, -17.5, -16] -> -17.75，不能再恒为 0。
    assert row["speaker_score_db"] == -17.75
    assert row["speaker_score_db"] != 0.0
    assert row["speaker_decision"] == "viterbi_exclusive"
    assert row["text"] == "你好世界"


def test_rows_split_by_track_keep_each_tracks_identity():
    units_a = [
        _unit(ch, index * 10, index * 10 + 10, track_index=0, score=-8.0,
              batch_id="doubao_track_0", track_order=1, item_index=0)
        for index, ch in enumerate("你好世界")
    ]
    units_b = [
        _unit(ch, 100 + index * 10, 100 + index * 10 + 10, track_index=1, score=-30.0,
              batch_id="doubao_track_1", track_order=2, item_index=4)
        for index, ch in enumerate("字幕生成")
    ]
    rows, _ = v4.segment_canonical_units(units_a + units_b, "standard", None, 30.0)
    assert len(rows) == 2
    first, second = rows
    assert first["track_index"] == 0
    assert first["batch_id"] == "doubao_track_0"
    assert first["track_order"] == 1
    assert first["item_index"] == 0
    assert first["speaker_score_db"] == -8.0
    assert second["track_index"] == 1
    assert second["batch_id"] == "doubao_track_1"
    assert second["track_order"] == 2
    assert second["item_index"] == 4
    assert second["speaker_score_db"] == -30.0
    assert "".join(row["text"] for row in rows) == "你好世界字幕生成"


def test_empty_group_helper_uses_safe_defaults():
    fields = v4._segment_row_source_fields([])
    assert fields["track_index"] == 0
    assert fields["speaker_score_db"] == 0.0
    assert fields["batch_id"] == ""
    assert fields["track_order"] == 0
    assert fields["item_index"] == 0
