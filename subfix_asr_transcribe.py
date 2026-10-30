#!/usr/bin/env python3
"""Local ASR / forced-alignment helper for SubFix.

Outputs small JSON payloads with segment-level start/end/text for the live
qwen/v5 transcription, generation and forced-alignment pipelines. Fixture
mode avoids heavyweight imports for tests.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import importlib.util
import json

# MARKER: record which file is actually loaded
try:
    with open(r"C:\Users\10840\subfix_py_marker.txt", "w", encoding="utf-8") as _mf:
        _mf.write("loaded from: " + str(__file__) + "\n")
except Exception:
    pass
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any

# Hugging Face 直连在部分网络环境不可达（运行时加载 Qwen3-ForcedAligner 会因此失败，
# 表现为识别进度卡住后报 “Qwen3-ASR 模型加载失败”）。
# 默认改用公共镜像 hf-mirror.com；用户显式设置过 HF_ENDPOINT 时尊重其配置。
# 注意：必须在首次导入 transformers/huggingface_hub 之前生效（此处均为懒加载，安全）。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


class _LazyGenerateV4:
    _module: Any = None

    def _load(self) -> Any:
        if self._module is not None:
            return self._module
        module_path = Path(__file__).resolve().with_name("subfix_generate_v4.py")
        if not module_path.is_file():
            raise RuntimeError(f"v4 生成模块缺失: {module_path.name}")
        spec = importlib.util.spec_from_file_location("subfix_generate_v4", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("v4 生成模块无法加载")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


generate_v4 = _LazyGenerateV4()


class _LazyGenerateV5:
    _module: Any = None

    def _load(self) -> Any:
        if self._module is not None:
            return self._module
        module_path = Path(__file__).resolve().with_name("subfix_generate_v5.py")
        if not module_path.is_file():
            raise RuntimeError(f"v5 生成模块缺失: {module_path.name}")
        spec = importlib.util.spec_from_file_location("subfix_generate_v5", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("v5 生成模块无法加载")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


generate_v5 = _LazyGenerateV5()


class _LazyGenerateTextnorm:
    _module: Any = None

    def _load(self) -> Any:
        if self._module is not None:
            return self._module
        module_path = Path(__file__).resolve().with_name("subfix_generate_textnorm.py")
        if not module_path.is_file():
            raise RuntimeError(f"数字与格式规范化模块缺失: {module_path.name}")
        spec = importlib.util.spec_from_file_location("subfix_generate_textnorm", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("数字与格式规范化模块无法加载")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


generate_textnorm = _LazyGenerateTextnorm()


DEFAULT_MODEL = "small"
HELPER_VERSION = "subfix-2026-07-13-candidate-arbitration-v5"
TRANSCRIBE_BACKENDS = (
    "mimo_asr",
    "qwen3_asr",
    "mlx_whisper",
    "openai_whisper",
    "doubao_asr",
    "doubao_asr_v2",
)
# 豆包后端均为付费、凭据门控路径，绝不进入 "auto" 回退链；默认行为仍是
# auto -> qwen3。只有显式 CLI/UI 选择才会调用任一豆包规格。
AUTO_TRANSCRIBE_BACKENDS = tuple(
    name for name in TRANSCRIBE_BACKENDS if name not in {"doubao_asr", "doubao_asr_v2"}
)
# NOTE: GENERATED_SUBTITLE_MAX_CHARS and friends below drive the rule-based
# splitter (split_generated_subtitle_text/_clause,
# generate_subtitle_rows_from_segments) used by fixture generation. The
# user-facing "字幕长度" (--max-chars) option only overrides the v4/v5 main
# segmentation path (subfix_generate_v4.segment_canonical_units /
# _segmentation_length_parameters); the rule-based splitter keeps its own
# fixed constants untouched.
GENERATED_SUBTITLE_MAX_CHARS = 18
GENERATED_SUBTITLE_PREFERRED_MIN_CHARS = 8
GENERATED_SUBTITLE_PREFERRED_MAX_CHARS = 14
GENERATED_SUBTITLE_MIN_TAIL_CHARS = 7
GENERATED_SUBTITLE_SEMANTIC_COMMA_MIN_CHARS = 4
GENERATED_SUBTITLE_COMMON_WORD_BOUNDARY_BIGRAMS = {
    "一个",
    "一些",
    "什么",
    "价格",
    "厉害",
    "可以",
    "嘲笑",
    "平台",
    "怎么",
    "意思",
    "硬件",
    "绝对",
    "视频",
    "这个",
    "这些",
    "那个",
    "那些",
    "产品",
    "把它",
    "块钱",
}
GENERATED_SUBTITLE_WORD_GAP_BREAK_SECONDS = 0.65
GENERATED_SUBTITLE_HARD_SILENCE_SECONDS = 0.45
GENERATED_SUBTITLE_VAD_ONLY_HARD_SILENCE_SECONDS = 0.80
GENERATED_SUBTITLE_WORD_GAP_CONSENSUS_SECONDS = 0.18
GENERATED_SUBTITLE_WORD_GAP_MIN_LEFT_CHARS = 2
GENERATED_SUBTITLE_WORD_GAP_MIN_RIGHT_CHARS = 2
GENERATED_SUBTITLE_LEARNED_MERGE_GAP_SECONDS = 0.45
SEGMENTATION_PROFILE_SCHEMA = "subfix_segmentation_profile_v2"
LEGACY_SEGMENTATION_PROFILE_SCHEMA = "subfix_segmentation_profile_v1"
SEGMENTATION_PROFILE_ENV = "SUBFIX_SEGMENTATION_PROFILE"
V5_WRITEBACK_ENV = "SUBFIX_V5_WRITEBACK"
GENERATED_SUBTITLE_MIN_DURATION_SECONDS = 0.55
GENERATED_SUBTITLE_SECONDS_PER_CHAR = 0.22
GENERATED_SUBTITLE_MAX_DURATION_SECONDS = 2.6
LIVE_SPEAKER_SWITCH_MARGIN_DB = 3.0
LIVE_SPEAKER_SWITCH_HOLD_SECONDS = 0.12
EXTERNAL_TRANSCRIBE_COMMAND_ENV = {
    "mimo_asr": "SUBFIX_MIMO_ASR_CMD",
}
# Volcano Engine (豆包/Doubao) 大模型录音文件极速版识别 backend. Its response
# carries word timestamps in milliseconds; normalize them to seconds so v4/v5
# can use them directly and reserve Qwen forced alignment for bad/missing data.
#
# Field names / endpoint below follow
# https://docs.volcengine.com/docs/6561/1631584 (fetched 2026-07-16). Marked
# "待校准" (to calibrate) where the public docs sample didn't fully pin the
# field down; verify against a real account response before relying on it.
DOUBAO_ASR_API_KEY_ENV = "SUBFIX_DOUBAO_API_KEY"
DOUBAO_ASR_RESOURCE_ID_ENV = "SUBFIX_DOUBAO_RESOURCE_ID"
DOUBAO_ASR_ENDPOINT_ENV = "SUBFIX_DOUBAO_ENDPOINT"
# Optional credentials file read as a fallback when the env vars are unset.
# It sits next to this helper (in the deployed plugin that is
# .subfix_support/doubao_credentials.json). Configuration accepts only
# "api_key". The real file is gitignored; a doubao_credentials.json.example
# template is tracked.
DOUBAO_ASR_CREDENTIALS_FILENAME = "doubao_credentials.json"
# "录音文件极速版识别" (flash/turbo tier) HTTP endpoint -- synchronous, no
# submit/query polling needed per the docs.
DOUBAO_ASR_DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
# Resource id required for the flash/turbo tier (different from the
# standard-tier "volc.bigasr.auc"); must be enabled for the account.
DOUBAO_ASR_DEFAULT_RESOURCE_ID = "volc.bigasr.auc_turbo"
DOUBAO_ASR_TIMEOUT_SECONDS = 30.0
DOUBAO_ASR_MAX_ATTEMPTS = 2
DOUBAO_ASR_RETRY_BACKOFF_SECONDS = 0.5
# 请求体 user.uid 是业务侧用户标识，绝不能放 API Key（会随请求体被网关日志
# 记录、计费侧可见）。SubFix 不区分终端用户，使用固定非敏感占位即可。
DOUBAO_ASR_CLIENT_UID = "subfix-client"
# 待校准: 文档 demo 中响应头 X-Api-Status-Code 的成功值；响应体 JSON 内未见到顶层
# code/message 字段（区别于旧版录音文件标准版接口）。
DOUBAO_ASR_SUCCESS_STATUS_CODE = "20000000"
DOUBAO_ASR_SILENT_AUDIO_STATUS_CODE = "20000003"
# 豆包录音文件识别模型 2.0 标准版使用异步 submit/query 协议。它与上方极速版
# 共用单 API Key，但不能复用同步 endpoint 或 resource id。
DOUBAO_ASR_V2_SUBMIT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
DOUBAO_ASR_V2_QUERY_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
DOUBAO_ASR_V2_RESOURCE_ID = "volc.seedasr.auc"
# 20000001/2 表示处理中/排队中，20000003 表示已完成但未检测到人声。
DOUBAO_ASR_V2_NON_ERROR_STATUS_CODES = frozenset(
    {DOUBAO_ASR_SUCCESS_STATUS_CODE, "20000001", "20000002", DOUBAO_ASR_SILENT_AUDIO_STATUS_CODE}
)
DOUBAO_ASR_V2_MAX_QUERY_ATTEMPTS = 60
DOUBAO_ASR_V2_QUERY_BACKOFF_SECONDS = 0.5
DOUBAO_ASR_V2_QUERY_MAX_BACKOFF_SECONDS = 2.0
QWEN3_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"
QWEN3_FORCED_ALIGNER_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B"
QWEN3_CPP_BIN_ENV = "SUBFIX_QWEN3_ASR_CPP_BIN"
QWEN3_CPP_ALIGNER_GGUF_ENV = "SUBFIX_QWEN3_ALIGNER_GGUF"
QWEN3_FORCED_ALIGNER_MODEL_NAMES = (
    "qwen3-forced-aligner-0.6b-f16.gguf",
    "qwen3-forced-aligner-0.6b-q8_0.gguf",
    "qwen3-forced-aligner-0.6b-q5_0.gguf",
    "qwen3-forced-aligner-0.6b-q4_k.gguf",
)
QWEN_ROW_REMAP_MIN_SCORE = 0.86
QWEN_ROW_REMAP_MAX_START_LOOKBACK = 2
QWEN_ROW_REMAP_MAX_START_LOOKAHEAD = 12
DEFAULT_FFMPEG_CANDIDATES = (
    str(Path(__file__).resolve().parent / "bin" / "ffmpeg"),
    str(Path(__file__).resolve().parent / "bin" / "ffmpeg.exe"),
    str(Path.home() / ".local" / "bin" / "ffmpeg"),
    str(Path.home() / ".local" / "bin" / "ffmpeg.exe"),
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "ffmpeg",
)
_QWEN3_ASR_MODEL_CACHE: dict[tuple[str, str, str, str | None], tuple[Any, str, str]] = {}
_QWEN3_FORCED_ALIGNER_CACHE: dict[tuple[str, str, str], Any] = {}
_OPENAI_WHISPER_MODEL_CACHE: dict[str, Any] = {}
HOTWORD_MAX_ENTRIES = 200
HOTWORD_CHINESE_DIGITS = str.maketrans("0123456789", "零一二三四五六七八九")


def load_hotword_entries(path: str | Path | None) -> list[dict[str, Any]]:
    """Load valid user hotwords without exposing malformed configuration to ASR."""
    if not path:
        return []
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return []

    entries: list[dict[str, Any]] = []
    seen_terms: set[str] = set()
    for raw_entry in payload["entries"]:
        if not isinstance(raw_entry, dict):
            continue
        raw_term = raw_entry.get("term")
        if not isinstance(raw_term, str):
            continue
        term = raw_term.strip()
        term_key = term.casefold()
        if not term or term_key in seen_terms:
            continue
        aliases: list[str] = []
        seen_aliases: set[str] = set()
        raw_aliases = raw_entry.get("aliases")
        if isinstance(raw_aliases, list):
            for raw_alias in raw_aliases:
                alias = str(raw_alias or "").strip()
                if alias and alias != term and alias not in seen_aliases:
                    aliases.append(alias)
                    seen_aliases.add(alias)
        entries.append({"term": term, "aliases": aliases})
        seen_terms.add(term_key)
    return entries


def build_hotword_context(entries: list[dict[str, Any]]) -> str:
    terms = [str(entry.get("term") or "").strip() for entry in entries[:HOTWORD_MAX_ENTRIES]]
    terms = [term for term in terms if term]
    return "请严格使用以下专有名词的标准写法；英文和数字不要改写为中文：" + "、".join(terms) if terms else ""


def implicit_hotword_aliases(term: str) -> list[str]:
    """Return non-UI aliases for ASR's common Arabic-to-Chinese digit conversion."""
    if not any(character.isdigit() for character in term):
        return []
    chinese_digit_alias = term.translate(HOTWORD_CHINESE_DIGITS)
    return [chinese_digit_alias] if chinese_digit_alias != term else []


def apply_hotword_replacements_to_units(
    units: list[dict[str, Any]], entries: list[dict[str, Any]]
) -> int:
    replacements: list[tuple[str, str]] = []
    seen_aliases: set[str] = set()
    for entry in entries:
        term = str(entry.get("term") or "").strip()
        for alias in [*(entry.get("aliases") or []), *implicit_hotword_aliases(term)]:
            alias_text = str(alias or "").strip()
            alias_key = alias_text.casefold()
            if term and alias_text and alias_key not in seen_aliases:
                replacements.append((alias_text, term))
                seen_aliases.add(alias_key)
    replacements.sort(key=lambda item: len(item[0]), reverse=True)

    changed_count = 0
    for unit in units:
        text = str(unit.get("text") or "")
        for alias, term in replacements:
            if re.search(r"[A-Za-z0-9]", alias):
                pattern = r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_])"
                text, replaced = re.subn(pattern, term, text, flags=re.IGNORECASE)
            else:
                replaced = text.count(alias)
                text = text.replace(alias, term)
            changed_count += replaced
        unit["text"] = text
    return changed_count


def v5_writeback_mode() -> str:
    mode = str(os.getenv(V5_WRITEBACK_ENV) or "live").strip().lower()
    return mode if mode in {"live", "shadow"} else "live"


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    payload.setdefault("helper_version", HELPER_VERSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_generate_writeback_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("ok") is False:
        return {"ok": False, "error": str(payload.get("error") or "ASR helper 执行失败")}
    return {
        "ok": True,
        "subtitle_rows": list(payload.get("subtitle_rows") or []),
    }


def sanitize_generate_diagnostic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    diagnostic_keys = {
        "requested_mode",
        "subtitle_mode",
        "live_engine",
        "source_batch_count",
        "batch_count",
        "batch_max_seconds",
        "successful_batch_count",
        "failed_batch_count",
        "generated_subtitle_count",
        "activity_track_count",
        "activity_item_count",
        "activity_skipped_item_count",
        "speech_island_count",
        "confirmed_silence_count",
        "vad_only_gap_count",
        "speaker_turn_count",
        "speaker_switch_count",
        "bleed_rejected_count",
        "ambiguous_window_count",
        "ambiguous_island_count",
        "overlap_suppressed_count",
        "duplicate_suppressed_count",
        "short_fragment_merged_count",
        "short_utterance_kept_count",
        "short_utterance_dropped_count",
        "speech_island_timing_anchor_count",
        "forced_silence_break_row_count",
        "learned_segmentation_row_count",
        "learned_text_correction_count",
        "legacy_passthrough_row_count",
        "live_fallback_no_word_timing",
        "live_fallback_no_activity",
        "qwen_batch_used",
        "hotword_enabled",
        "hotword_entry_count",
        "hotword_injected_count",
        "hotword_replacement_count",
        "hotword_context_supported",
        "segmentation_profile_used",
        "segmentation_profile_schema",
        "generate_engine",
        "track_composite_count",
        "context_window_count",
        "asr_empty_speech_window_count",
        "asr_tolerated_empty_speech_window_count",
        "asr_empty_speech_subwindow_count",
        "asr_single_retry_count",
        "asr_subwindow_retry_count",
        "asr_recovered_window_count",
        "asr_unrecovered_window_count",
        "forced_align_retry_count",
        "context_align_retry_count",
        "alignment_repaired_unit_count",
        "minimum_raw_alignment_coverage",
        "unaligned_rejected_count",
        "alignment_window_alternative_count",
        "alignment_window_uncovered_count",
        "canonical_unit_count",
        "forced_aligned_unit_coverage",
        "cross_mic_echo_region_count",
        "cross_mic_echo_suppressed_count",
        "cross_mic_echo_ambiguous_count",
        "cross_mic_echo_suppressed_unit_count",
        "cross_mic_duplicate_count",
        "short_speaker_flip_suppressed_count",
        "viterbi_source_switch_count",
        "ordinary_single_character_count",
        "ordinary_single_character_suppressed_count",
        "short_reply_suppressed_count",
        "single_unit_merged_count",
        "incomplete_clause_merged_count",
        "overlap_candidate_suppressed_count",
        "window_seam_suppressed_count",
        "cross_mic_boundary_serialized_count",
        "structural_duplicate_suppressed_count",
        "near_duplicate_suppressed_count",
        "orphan_segment_merged_count",
        "short_gap_filled_count",
        "text_conservation_failed_count",
        "hard_boundary_count",
        "overlap_disagreement_count",
        "local_retry_region_count",
        "local_retry_elapsed_seconds",
        "local_retry_selected_count",
        "local_retry_proposed_count",
        "local_retry_writeback_mode",
        "low_confidence_region_count",
        "local_retry_alignment_failed_count",
        "audio_refined_row_count",
        "audio_refinement_mode",
        "audio_refinement_suggested_row_count",
        "audio_refinement_suggested_energy_valley_count",
        "audio_refinement_suggested_silence_count",
        "energy_valley_boundary_count",
        "original_gap_preserved_count",
        "confirmed_silence_preserved_count",
        "overlong_tail_reclaimed_count",
        "tail_extended_row_count",
        "textnorm_changed_row_count",
        "profile_load_status",
        "failure_code",
        "asr_backend_used",
        "doubao_fallback_count",
        "doubao_native_timestamp_window_count",
        "doubao_native_timestamp_fallback_count",
        "adaptive_fast_unit_count",
        "adaptive_full_unit_count",
        "adaptive_full_region_count",
        "adaptive_selection_elapsed_seconds",
        "doubao_fallback_errors",
        "doubao_log_ids",
        "doubao_resource_ids",
        "doubao_status_codes",
    }
    row_keys = {
        "index",
        "start_frame",
        "end_frame",
        "text",
        "track_index",
        "speaker_track_index",
        "speaker_score_db",
        "speaker_dominance_db",
        "speaker_decision",
        "batch_id",
        "track_order",
        "item_index",
        "speech_island_id",
        "short_utterance_confident",
        "timing_anchor_source",
        "segmentation_decision",
        "forced_silence_break",
        "text_correction_count",
        "original_start_frame",
        "original_end_frame",
        "suggested_start_frame",
        "suggested_end_frame",
        "timing_decision",
    }
    raw_diagnostic = payload.get("diagnostic") if isinstance(payload.get("diagnostic"), dict) else {}
    diagnostic = {key: raw_diagnostic[key] for key in diagnostic_keys if key in raw_diagnostic}
    raw_stages = raw_diagnostic.get("stages") if isinstance(raw_diagnostic.get("stages"), dict) else {}
    stage_keys = {
        "track_index",
        "window_index",
        "start_frame",
        "end_frame",
        "text",
        "source_score",
        "speaker_score_db",
        "speaker_decision",
        "boundary_probability",
        "alignment_coverage",
        "raw_alignment_coverage",
        "alignment_repaired",
        "asr_recovery",
        "speech_seconds",
        "asr_empty_speech_subwindow_count",
        "asr_punctuation_strength",
        "independent_vad",
        "candidate_kind",
        "candidate_decision",
        "candidate_score",
        "retry_reason",
    }
    stages: dict[str, list[dict[str, Any]]] = {}
    for stage_name in ("window_asr", "aligned_units", "source_selection", "segmentation"):
        rows = raw_stages.get(stage_name) if isinstance(raw_stages.get(stage_name), list) else []
        stages[stage_name] = [
            {key: row[key] for key in stage_keys if key in row}
            for row in rows
            if isinstance(row, dict)
        ]
    if any(stages.values()):
        diagnostic["stages"] = stages
    subtitle_rows = [
        {key: row[key] for key in row_keys if key in row}
        for row in payload.get("subtitle_rows") or []
        if isinstance(row, dict)
    ]
    batch_summaries = []
    for batch in payload.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        batch_diagnostic = batch.get("diagnostic") if isinstance(batch.get("diagnostic"), dict) else {}
        batch_summaries.append(
            {
                "ok": batch.get("ok") is True,
                "track_index": int(batch.get("track_index") or 0),
                "diagnostic": {
                    key: batch_diagnostic[key]
                    for key in diagnostic_keys | {"segment_count", "generated_subtitle_count", "item_count", "window_count"}
                    if key in batch_diagnostic
                },
            }
        )
    diagnostic_schema = (
        generate_v5.DIAGNOSTIC_SCHEMA
        if str(raw_diagnostic.get("generate_engine") or "").lower() == "v5"
        else "subfix_generate_diagnostic_v2"
    )
    return {
        "schema_version": diagnostic_schema,
        "helper_version": HELPER_VERSION,
        "generated_at": time.time(),
        "ok": payload.get("ok") is True,
        "backend": str(payload.get("backend") or ""),
        "model": Path(str(payload.get("model") or "")).name if "/" in str(payload.get("model") or "") else str(payload.get("model") or ""),
        "subtitle_mode": str(raw_diagnostic.get("subtitle_mode") or "narration"),
        "diagnostic": diagnostic,
        "batches": batch_summaries,
        "subtitle_rows": subtitle_rows,
    }


def write_progress(path: Path | None, stage: str, message: str, **extra: Any) -> None:
    if not path:
        return
    payload = {
        "stage": stage,
        "message": message,
        "timestamp": time.time(),
        "helper_version": HELPER_VERSION,
    }
    payload.update(extra)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def generate_batch_progress_total(total_batches: int) -> int:
    return max(1, int(total_batches)) * 2 + 2


def v4_progress_point(start: int, end: int, completed: int, total: int) -> int:
    if total <= 0:
        return int(start)
    fraction = max(0.0, min(1.0, float(completed) / float(total)))
    return int(round(float(start) + (float(end) - float(start)) * fraction))


def generate_helper_completion_progress(diagnostic: dict[str, Any]) -> tuple[int, int]:
    if str(diagnostic.get("generate_engine") or "").lower() in {"v4", "v5"}:
        return 95, 100
    total = generate_batch_progress_total(int(diagnostic.get("batch_count") or 0))
    return total, total


def normalize_segments(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    segments = []
    for index, segment in enumerate(raw_payload.get("segments") or [], start=1):
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start)
        text = str(segment.get("text") or "").strip()
        if end > start and text:
            normalized = {"index": index, "start": start, "end": end, "text": text}
            if "ctc_confidence" in segment:
                normalized["ctc_confidence"] = float(segment.get("ctc_confidence") or 0.0)
            if "ctc_char_count" in segment:
                normalized["ctc_char_count"] = int(segment.get("ctc_char_count") or 0)
            words = []
            for word in segment.get("words") or []:
                word_start = float(word.get("start") or start)
                word_end = float(word.get("end") or word_start)
                word_text = str(word.get("word") or word.get("text") or "").strip()
                if word_end <= word_start:
                    word_end = word_start + 0.001
                if word_text:
                    words.append({"word": word_text, "start": word_start, "end": word_end})
            if words:
                normalized["words"] = words
            segments.append(normalized)
    return segments


def generated_subtitle_text_length(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def normalize_generated_subtitle_text(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    return value


def strip_generated_subtitle_punctuation(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"[，。！？、；：,.!?;:\"'“”‘’（）()《》【】\[\]{}<>…·/\\|]+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def generated_subtitle_effective_length(text: str) -> int:
    return len(normalize_ctc_text(text))


def generated_subtitle_right_side_until_hard_break(text: str) -> str:
    hard_breaks = "。！？!?；;"
    chars: list[str] = []
    for char in str(text or ""):
        chars.append(char)
        if char in hard_breaks:
            break
    return "".join(chars)


def generated_subtitle_starts_with_orphan_particle(text: str) -> bool:
    value = re.sub(r"^[\s，、,：:。！？!?；;]+", "", str(text or ""))
    return bool(value) and value[0] in "了的呢吧吗啊呀嘛啦"


def should_split_generated_subtitle_semantic_comma(left_text: str, right_text: str) -> bool:
    right_clause = generated_subtitle_right_side_until_hard_break(right_text)
    if generated_subtitle_starts_with_orphan_particle(right_clause):
        return False
    return (
        generated_subtitle_effective_length(left_text) >= GENERATED_SUBTITLE_SEMANTIC_COMMA_MIN_CHARS
        and generated_subtitle_effective_length(right_clause) >= GENERATED_SUBTITLE_SEMANTIC_COMMA_MIN_CHARS
    )


def rebalance_generated_subtitle_short_tail(
    chunks: list[str],
    min_tail_chars: int = GENERATED_SUBTITLE_MIN_TAIL_CHARS,
    max_chars: int = GENERATED_SUBTITLE_MAX_CHARS,
) -> list[str]:
    if len(chunks) < 2:
        return chunks

    output = chunks[:]
    if not strip_generated_subtitle_punctuation(output[-1]):
        return output

    while (
        generated_subtitle_text_length(output[-1]) < min_tail_chars
        and generated_subtitle_text_length(output[-2]) > min_tail_chars
        and generated_subtitle_text_length(output[-1]) < max_chars
    ):
        previous = output[-2].rstrip()
        if not previous:
            break
        output[-2] = previous[:-1].rstrip()
        output[-1] = (previous[-1] + output[-1]).strip()

    return [chunk for chunk in output if chunk]


def should_rebalance_generated_subtitle_row_tail(
    previous_text: str,
    current_text: str,
    min_tail_chars: int = GENERATED_SUBTITLE_MIN_TAIL_CHARS,
    max_chars: int = GENERATED_SUBTITLE_MAX_CHARS,
) -> bool:
    previous_length = generated_subtitle_text_length(previous_text)
    current_length = generated_subtitle_text_length(current_text)
    if not previous_text or not current_text or current_length >= min_tail_chars:
        return False
    return (
        previous_length > min_tail_chars
        and previous_text[-1] + current_text[0] in GENERATED_SUBTITLE_COMMON_WORD_BOUNDARY_BIGRAMS
    )


def rebalance_generated_subtitle_row_short_tails(
    rows: list[dict[str, Any]],
    min_tail_chars: int = GENERATED_SUBTITLE_MIN_TAIL_CHARS,
    max_chars: int = GENERATED_SUBTITLE_MAX_CHARS,
    max_gap_frames: int = 12,
) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return rows

    output = [dict(row) for row in rows]
    for index in range(1, len(output)):
        previous = output[index - 1]
        current = output[index]
        previous_text = str(previous.get("text") or "").rstrip()
        current_text = str(current.get("text") or "").strip()
        previous_length = generated_subtitle_text_length(previous_text)
        current_length = generated_subtitle_text_length(current_text)
        frame_gap = int(current.get("start_frame") or 0) - int(previous.get("end_frame") or 0)
        previous_speaker = previous.get("speaker_track_index")
        current_speaker = current.get("speaker_track_index")
        previous_island = str(previous.get("speech_island_id") or "")
        current_island = str(current.get("speech_island_id") or "")
        if (
            previous_speaker is not None
            and current_speaker is not None
            and int(previous_speaker) != int(current_speaker)
        ):
            continue
        if (previous_island or current_island) and previous_island != current_island:
            continue
        if (
            not should_rebalance_generated_subtitle_row_tail(
                previous_text,
                current_text,
                min_tail_chars=min_tail_chars,
                max_chars=max_chars,
            )
            or frame_gap > max_gap_frames
        ):
            continue

        moved_chars = 0
        original_previous_length = previous_length
        while current_length < min_tail_chars and previous_length > min_tail_chars:
            char = previous_text[-1]
            previous_text = previous_text[:-1].rstrip()
            current_text = (char + current_text).strip()
            previous_length = generated_subtitle_text_length(previous_text)
            current_length = generated_subtitle_text_length(current_text)
            moved_chars += 1

        if moved_chars <= 0 or not previous_text:
            continue

        previous["text"] = previous_text
        current["text"] = current_text

        previous_start = int(previous.get("start_frame") or 0)
        previous_end = int(previous.get("end_frame") or previous_start + 1)
        current_end = int(current.get("end_frame") or previous_end + 1)
        frames_per_char = max(1, int(round((previous_end - previous_start) / max(1, original_previous_length))))
        boundary = max(previous_start + 1, previous_end - frames_per_char * moved_chars)
        previous["end_frame"] = boundary
        current["start_frame"] = boundary
        if current_end <= boundary:
            current["end_frame"] = boundary + 1

    return output


def split_generated_subtitle_clause(text: str, max_chars: int = GENERATED_SUBTITLE_MAX_CHARS) -> list[str]:
    value = normalize_generated_subtitle_text(text)
    if not value:
        return []

    chunks: list[str] = []
    soft_breaks = "，、,：:"
    preferred_min = GENERATED_SUBTITLE_PREFERRED_MIN_CHARS
    preferred_max = min(GENERATED_SUBTITLE_PREFERRED_MAX_CHARS, max_chars)

    while generated_subtitle_text_length(value) > max_chars:
        break_at = 0
        upper = min(preferred_max, len(value))
        for index in range(upper, preferred_min - 1, -1):
            if value[index - 1] in soft_breaks:
                break_at = index
                break
        if break_at == 0:
            for index in range(min(max_chars, len(value)), preferred_min - 1, -1):
                if value[index - 1] in soft_breaks:
                    break_at = index
                    break
        if break_at == 0:
            for index in range(preferred_min - 1, 1, -1):
                if value[index - 1] in soft_breaks:
                    break_at = index
                    break
        if break_at == 0:
            break_at = min(max_chars, len(value))
        chunk = value[:break_at].strip()
        if chunk:
            chunks.append(chunk)
        value = value[break_at:].strip()

    if value:
        chunks.append(value)
    return rebalance_generated_subtitle_short_tail(chunks, max_chars=max_chars)


def split_generated_subtitle_text(text: str, max_chars: int = GENERATED_SUBTITLE_MAX_CHARS) -> list[str]:
    value = normalize_generated_subtitle_text(text)
    if not value:
        return []

    hard_breaks = "。！？!?；;"
    semantic_comma_breaks = "，、,"
    clauses: list[str] = []
    buffer: list[str] = []
    for index, char in enumerate(value):
        buffer.append(char)
        should_break = char in hard_breaks
        if not should_break and char in semantic_comma_breaks:
            should_break = should_split_generated_subtitle_semantic_comma("".join(buffer), value[index + 1 :])
        if should_break:
            clause = "".join(buffer).strip()
            if clause:
                clauses.append(clause)
            buffer = []
    tail = "".join(buffer).strip()
    if tail:
        clauses.append(tail)

    chunks: list[str] = []
    for clause in clauses:
        chunks.extend(split_generated_subtitle_clause(clause, max_chars=max_chars))
    return chunks


def seconds_to_timeline_frame(seconds: float, fps: float, timeline_start_frame: int) -> int:
    return int(timeline_start_frame + math.floor((float(seconds) * float(fps)) + 0.5))


def distribute_generated_subtitle_frames(
    segment_start_frame: int,
    segment_end_frame: int,
    chunks: list[str],
) -> list[tuple[int, int]]:
    if not chunks:
        return []

    start_frame = int(segment_start_frame)
    end_frame = max(start_frame + len(chunks), int(segment_end_frame))
    total_frames = max(len(chunks), end_frame - start_frame)
    weights = [max(1, generated_subtitle_text_length(chunk)) for chunk in chunks]
    total_weight = max(1, sum(weights))
    frames: list[tuple[int, int]] = []
    cursor = start_frame
    elapsed_weight = 0

    for index, weight in enumerate(weights):
        remaining_chunks = len(chunks) - index - 1
        elapsed_weight += weight
        if index == len(chunks) - 1:
            chunk_end = end_frame
        else:
            proportional = start_frame + int(math.floor((total_frames * elapsed_weight / total_weight) + 0.5))
            chunk_end = max(cursor + 1, proportional)
            chunk_end = min(chunk_end, end_frame - remaining_chunks)
        frames.append((cursor, max(cursor + 1, chunk_end)))
        cursor = frames[-1][1]

    return frames


def generated_subtitle_timed_units(words: list[dict[str, Any]]) -> list[dict[str, float]]:
    units: list[dict[str, float]] = []
    for word in words or []:
        word_text = str(word.get("word") or word.get("text") or "")
        normalized = normalize_ctc_text(word_text)
        if not normalized:
            continue
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") or start)
        if end <= start:
            end = start + 0.001
        duration = (end - start) / max(1, len(normalized))
        for index, _char in enumerate(normalized):
            unit_start = start + duration * index
            unit_end = start + duration * (index + 1)
            units.append({"start": unit_start, "end": max(unit_end, unit_start + 0.001)})
    return units


def generated_subtitle_forced_silence_boundaries(
    words: list[dict[str, Any]],
    speech_regions: list[dict[str, Any]] | None = None,
    min_silence_seconds: float = GENERATED_SUBTITLE_HARD_SILENCE_SECONDS,
) -> list[dict[str, Any]]:
    timed_units = generated_subtitle_timed_units(words)
    if len(timed_units) < 2:
        return []

    candidates: dict[int, dict[str, Any]] = {}
    for left_region, right_region in zip(speech_regions or [], (speech_regions or [])[1:]):
        silence_start = float(left_region.get("end") or 0.0)
        silence_end = float(right_region.get("start") or silence_start)
        silence_seconds = silence_end - silence_start
        if silence_seconds < min_silence_seconds:
            continue
        silence_midpoint = (silence_start + silence_end) * 0.5
        first_midpoint = (timed_units[0]["start"] + timed_units[0]["end"]) * 0.5
        last_midpoint = (timed_units[-1]["start"] + timed_units[-1]["end"]) * 0.5
        if silence_midpoint <= first_midpoint or silence_midpoint >= last_midpoint:
            continue
        offset = min(
            range(1, len(timed_units)),
            key=lambda index: abs(
                ((timed_units[index - 1]["end"] + timed_units[index]["start"]) * 0.5)
                - silence_midpoint
            ),
        )
        left_count = offset
        right_count = len(timed_units) - offset
        if left_count < 2 or right_count < 2:
            continue
        word_gap = float(timed_units[offset].get("start") or 0.0) - float(
            timed_units[offset - 1].get("end") or 0.0
        )
        if word_gap >= GENERATED_SUBTITLE_WORD_GAP_CONSENSUS_SECONDS:
            source = "vad_word_consensus"
        elif silence_seconds >= GENERATED_SUBTITLE_VAD_ONLY_HARD_SILENCE_SECONDS:
            source = "vad_long"
        else:
            continue
        candidates[offset] = {
            "offset": offset,
            "silence_start": silence_start,
            "silence_end": silence_end,
            "silence_seconds": silence_seconds,
            "word_gap_seconds": max(0.0, word_gap),
            "source": source,
        }

    for offset in range(1, len(timed_units)):
        silence_start = float(timed_units[offset - 1].get("end") or 0.0)
        silence_end = float(timed_units[offset].get("start") or silence_start)
        silence_seconds = silence_end - silence_start
        if (
            silence_seconds < GENERATED_SUBTITLE_WORD_GAP_BREAK_SECONDS
            or offset in candidates
            or offset < 2
            or len(timed_units) - offset < 2
        ):
            continue
        candidates[offset] = {
            "offset": offset,
            "silence_start": silence_start,
            "silence_end": silence_end,
            "silence_seconds": silence_seconds,
            "source": "word_timing",
        }
    return [candidates[offset] for offset in sorted(candidates)]


def split_generated_subtitle_text_at_normalized_offset(text: str, offset: int) -> tuple[str, str] | None:
    if offset <= 0:
        return None
    consumed = 0
    for index, char in enumerate(str(text or "")):
        consumed += len(normalize_ctc_text(char))
        if consumed >= offset:
            left = str(text or "")[: index + 1].strip()
            right = str(text or "")[index + 1 :].strip()
            if left and right:
                return left, right
            return None
    return None


def generated_subtitle_has_break_punctuation_at_normalized_offset(text: str, offset: int) -> bool:
    if offset <= 0:
        return False
    break_punctuation = "，、,：:。！？!?；;"
    value = str(text or "")
    consumed = 0
    for index, char in enumerate(value):
        consumed += len(normalize_ctc_text(char))
        if consumed >= offset:
            return index + 1 < len(value) and value[index + 1] in break_punctuation
    return False


def split_generated_subtitle_chunk_by_timed_gap(
    chunk: str,
    timed_units: list[dict[str, float]],
    min_gap_seconds: float = GENERATED_SUBTITLE_WORD_GAP_BREAK_SECONDS,
    min_left_chars: int = GENERATED_SUBTITLE_WORD_GAP_MIN_LEFT_CHARS,
    min_right_chars: int = GENERATED_SUBTITLE_WORD_GAP_MIN_RIGHT_CHARS,
) -> list[str]:
    unit_count = len(normalize_ctc_text(chunk))
    if unit_count < min_left_chars + min_right_chars or len(timed_units) < unit_count:
        return [chunk]

    best_offset = 0
    best_gap = 0.0
    for offset in range(min_left_chars, unit_count - min_right_chars + 1):
        right_count = unit_count - offset
        if right_count < GENERATED_SUBTITLE_WORD_GAP_MIN_RIGHT_CHARS + 2 and not generated_subtitle_has_break_punctuation_at_normalized_offset(chunk, offset):
            continue
        gap = float(timed_units[offset].get("start") or 0.0) - float(timed_units[offset - 1].get("end") or 0.0)
        if gap >= min_gap_seconds and gap > best_gap:
            best_gap = gap
            best_offset = offset
    if best_offset <= 0:
        return [chunk]

    split = split_generated_subtitle_text_at_normalized_offset(chunk, best_offset)
    if not split:
        return [chunk]
    left, right = split
    return [left, right]


def split_generated_subtitle_chunks_by_word_gaps(chunks: list[str], words: list[dict[str, Any]]) -> list[str]:
    timed_units = generated_subtitle_timed_units(words)
    if not timed_units:
        return chunks

    output: list[str] = []
    cursor = 0
    for chunk in chunks:
        unit_count = len(normalize_ctc_text(chunk))
        unit_slice = timed_units[cursor : cursor + unit_count]
        output.extend(split_generated_subtitle_chunk_by_timed_gap(chunk, unit_slice))
        cursor += unit_count
    return output


def _map_sequence_boundary(matcher: difflib.SequenceMatcher, source_position: int, reverse: bool = False) -> int | None:
    for source_start, target_start, size in matcher.get_matching_blocks():
        if reverse:
            source_start, target_start = target_start, source_start
        if source_start <= source_position <= source_start + size:
            return target_start + (source_position - source_start)
    return None


def _segmentation_boundary_features(text: str, position: int) -> list[str]:
    value = str(text or "")
    if position <= 0 or position >= len(value):
        return []
    features = [
        f"left1={value[position - 1:position]}",
        f"right1={value[position:position + 1]}",
        f"cross2={value[position - 1:position + 1]}",
    ]
    if position >= 2:
        features.append(f"left2={value[position - 2:position]}")
    if position + 2 <= len(value):
        features.append(f"right2={value[position:position + 2]}")
    if position >= 2 and position + 2 <= len(value):
        features.append(f"cross4={value[position - 2:position + 2]}")
    return features


def segmentation_profile_for_mode(
    profile: dict[str, Any] | None,
    subtitle_mode: str,
) -> dict[str, Any] | None:
    if not isinstance(profile, dict):
        return None
    mode = "live" if subtitle_mode == "live" else "narration"
    schema = str(profile.get("schema_version") or "")
    if schema == SEGMENTATION_PROFILE_SCHEMA:
        mode_profile = (profile.get("modes") or {}).get(mode)
        return mode_profile if isinstance(mode_profile, dict) else None
    if schema == LEGACY_SEGMENTATION_PROFILE_SCHEMA:
        return profile if mode == "narration" else None
    if "length_model" in profile and "feature_weights" in profile:
        return profile
    return None


def load_segmentation_profile(path: str | Path | None = None) -> dict[str, Any] | None:
    raw_path = path or os.getenv(SEGMENTATION_PROFILE_ENV)
    profile_path = Path(raw_path).expanduser() if raw_path else Path(__file__).resolve().with_name("segmentation_profile.json")
    if not profile_path.is_file():
        return None
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") not in {SEGMENTATION_PROFILE_SCHEMA, LEGACY_SEGMENTATION_PROFILE_SCHEMA}:
        return None
    payload["_path"] = str(profile_path)
    return payload


def load_segmentation_profile_v4(path: str | Path | None = None) -> dict[str, Any] | None:
    raw_path = path or os.getenv(SEGMENTATION_PROFILE_ENV)
    profile_path = Path(raw_path).expanduser() if raw_path else Path(__file__).resolve().with_name("segmentation_profile_v3.json")
    if not profile_path.is_file():
        return None
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != generate_v4.PROFILE_SCHEMA:
        return None
    payload["_path"] = str(profile_path)
    return payload


def load_segmentation_profile_v5(path: str | Path | None = None) -> tuple[dict[str, Any] | None, str]:
    raw_path = path or os.getenv(SEGMENTATION_PROFILE_ENV)
    profile_path = Path(raw_path).expanduser() if raw_path else Path(__file__).resolve().with_name("segmentation_profile_v4.json")
    if not profile_path.is_file():
        return None, "missing"
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, "invalid_json"
    except OSError:
        return None, "unreadable"
    if not isinstance(payload, dict):
        return None, "invalid_payload"
    if payload.get("schema_version") != generate_v5.PROFILE_SCHEMA:
        return None, "schema_mismatch"
    payload["_path"] = str(profile_path)
    return payload, "loaded"


def _mapped_example_boundaries(text: str, profile: dict[str, Any]) -> tuple[set[int], float]:
    best_boundaries: set[int] = set()
    best_score = 0.0
    for example in profile.get("examples") or []:
        source_text = str(example.get("source_text") or "")
        if not source_text:
            continue
        if text in source_text:
            offset = source_text.index(text)
            boundaries = {
                int(position) - offset
                for position in example.get("mapped_boundaries") or []
                if offset < int(position) < offset + len(text)
            }
            score = 1.0
        else:
            matcher = difflib.SequenceMatcher(None, source_text, text, autojunk=False)
            matched_chars = sum(size for _source, _target, size in matcher.get_matching_blocks())
            score = matched_chars / max(1, len(text))
            boundaries = set()
            for position in example.get("mapped_boundaries") or []:
                mapped = _map_sequence_boundary(matcher, int(position))
                if mapped is not None and 0 < mapped < len(text):
                    boundaries.add(mapped)
        if score > best_score:
            best_score = score
            best_boundaries = boundaries
    return best_boundaries, min(1.0, best_score)


def _normalized_break_punctuation_offsets(text: str) -> set[int]:
    offsets: set[int] = set()
    consumed = 0
    for char in str(text or ""):
        if char in "，、,：:。！？!?；;":
            if consumed > 0:
                offsets.add(consumed)
            continue
        consumed += len(normalize_ctc_text(char))
    return offsets


def _learned_boundary_score(
    text: str,
    position: int,
    profile: dict[str, Any],
    exact_boundaries: set[int],
    punctuation_offsets: set[int],
    timed_units: list[dict[str, float]],
) -> float:
    if position <= 0 or position >= len(text):
        return -1000.0
    if text[position - 1].isascii() and text[position - 1].isalnum() and text[position].isascii() and text[position].isalnum():
        return -1000.0

    score = -5.0
    feature_weights = profile.get("feature_weights") or {}
    feature_values = [float(feature_weights.get(feature) or 0.0) for feature in _segmentation_boundary_features(text, position)]
    if feature_values:
        score += sum(feature_values) / len(feature_values) * 0.8
    if position in exact_boundaries:
        score += 14.0
    if position in punctuation_offsets:
        score += 5.0
    if position < len(timed_units):
        gap = float(timed_units[position].get("start") or 0.0) - float(timed_units[position - 1].get("end") or 0.0)
        if gap >= 0.65:
            score += 8.0
        elif gap >= 0.25:
            score += 4.0
        elif gap >= 0.12:
            score += 1.5
    if text[position - 1:position + 1] in GENERATED_SUBTITLE_COMMON_WORD_BOUNDARY_BIGRAMS:
        score -= 7.0
    if generated_subtitle_starts_with_orphan_particle(text[position:]):
        score -= 5.0
    return score


def _learned_segment_length_score(length: int, length_model: dict[str, Any]) -> float:
    minimum = max(2, int(length_model.get("minimum") or 4))
    p25 = max(minimum, int(length_model.get("p25") or 7))
    median = max(p25, int(length_model.get("median") or 10))
    p75 = max(median, int(length_model.get("p75") or 14))
    p95 = max(p75, int(length_model.get("p95") or 20))
    if length < 2:
        return -1000.0
    score = -abs(length - median) * 0.22
    if p25 <= length <= p75:
        score += 0.25
    if length < minimum:
        score -= (minimum - length) * 3.0
    if length > p95:
        score -= (length - p95) * 1.2
    return score


def split_generated_subtitle_text_learned(
    text: str,
    words: list[dict[str, Any]],
    profile: dict[str, Any],
    speech_regions: list[dict[str, Any]] | None = None,
    subtitle_mode: str = "narration",
) -> tuple[list[str], dict[str, Any]]:
    normalized_text = normalize_ctc_text(text)
    mode_profile = segmentation_profile_for_mode(profile, subtitle_mode)
    if not normalized_text or not mode_profile:
        return split_generated_subtitle_text(text), {"used": False, "reason": "invalid_profile"}
    timed_units = generated_subtitle_timed_units(words)
    if len(timed_units) < len(normalized_text):
        return split_generated_subtitle_text(text), {"used": False, "reason": "missing_word_timing"}

    mapped_boundaries, matched_example_score = _mapped_example_boundaries(normalized_text, mode_profile)
    exact_boundaries = mapped_boundaries if matched_example_score >= 0.75 else set()
    forced_silence_boundaries = generated_subtitle_forced_silence_boundaries(words, speech_regions)
    forced_offsets = {int(boundary["offset"]) for boundary in forced_silence_boundaries}
    punctuation_offsets = _normalized_break_punctuation_offsets(text)
    length_model = mode_profile.get("length_model") or {}
    soft_limit = 14 if subtitle_mode == "live" else GENERATED_SUBTITLE_MAX_CHARS
    if (
        len(normalized_text) <= soft_limit
        and not exact_boundaries
        and not forced_offsets
        and not punctuation_offsets
    ):
        return [text], {
            "used": True,
            "reason": "below_soft_limit_without_boundary_evidence",
            "candidate_count": max(0, len(normalized_text) - 1),
            "matched_example_score": round(matched_example_score, 6),
            "matched_boundary_count": 0,
            "forced_silence_boundaries": [],
            "forced_silence_break_count": 0,
            "selected_boundaries": [],
            "score": 0.0,
        }
    hard_max = max(24, int(length_model.get("p95") or 20) + 8, int(length_model.get("maximum") or 18) + 3)
    positions = list(range(0, len(normalized_text) + 1))
    scores = [-math.inf] * len(positions)
    previous = [-1] * len(positions)
    scores[0] = 0.0

    for end in positions[1:]:
        for start in range(max(0, end - hard_max), end):
            if not math.isfinite(scores[start]):
                continue
            if any(start < forced_offset < end for forced_offset in forced_offsets):
                continue
            length = end - start
            segment_score = _learned_segment_length_score(length, length_model)
            if segment_score <= -999.0 and length == 1 and (
                start == 0 or end == len(normalized_text) or start in forced_offsets or end in forced_offsets
            ):
                segment_score = -4.0
            if segment_score <= -999.0:
                continue
            boundary_score = 0.0
            if end < len(normalized_text):
                boundary_score = _learned_boundary_score(
                    normalized_text,
                    end,
                    mode_profile,
                    exact_boundaries,
                    punctuation_offsets,
                    timed_units,
                )
                if boundary_score <= -999.0:
                    continue
            candidate_score = scores[start] + segment_score + boundary_score
            if candidate_score > scores[end]:
                scores[end] = candidate_score
                previous[end] = start

    if previous[-1] < 0:
        return split_generated_subtitle_text(text), {"used": False, "reason": "no_dp_path"}

    boundaries: list[int] = []
    cursor = len(normalized_text)
    while cursor > 0:
        start = previous[cursor]
        if start < 0:
            return split_generated_subtitle_text(text), {"used": False, "reason": "broken_dp_path"}
        if start > 0:
            boundaries.append(start)
        cursor = start
    boundaries.reverse()

    chunks: list[str] = []
    raw_tail = str(text or "")
    consumed = 0
    for boundary in [*boundaries, len(normalized_text)]:
        split_offset = boundary - consumed
        if boundary == len(normalized_text):
            chunk = raw_tail.strip()
            raw_tail = ""
        else:
            split = split_generated_subtitle_text_at_normalized_offset(raw_tail, split_offset)
            if not split:
                return split_generated_subtitle_text(text), {"used": False, "reason": "raw_text_mapping_failed"}
            chunk, raw_tail = split
        if chunk:
            chunks.append(chunk)
        consumed = boundary

    return chunks, {
        "used": True,
        "reason": "learned_dp",
        "candidate_count": max(0, len(normalized_text) - 1),
        "matched_example_score": round(matched_example_score, 6),
        "matched_boundary_count": len(exact_boundaries),
        "forced_silence_boundaries": forced_silence_boundaries,
        "forced_silence_break_count": len(forced_silence_boundaries),
        "selected_boundaries": boundaries,
        "score": round(scores[-1], 6),
    }


def apply_forced_silence_to_frame_ranges(
    frame_ranges: list[tuple[int, int]],
    chunks: list[str],
    forced_silence_boundaries: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
) -> tuple[list[tuple[int, int]], set[int]]:
    if len(frame_ranges) < 2 or not forced_silence_boundaries:
        return frame_ranges, set()
    forced_by_offset = {
        int(boundary.get("offset") or 0): boundary
        for boundary in forced_silence_boundaries
        if int(boundary.get("offset") or 0) > 0
    }
    adjusted = [[int(start), int(end)] for start, end in frame_ranges]
    adjusted_boundaries: set[int] = set()
    offset = 0
    for index, chunk in enumerate(chunks[:-1]):
        offset += len(normalize_ctc_text(chunk))
        boundary = forced_by_offset.get(offset)
        if not boundary:
            continue
        silence_end_frame = seconds_to_timeline_frame(
            float(boundary.get("silence_start") or 0.0),
            fps,
            timeline_start_frame,
        )
        next_speech_frame = seconds_to_timeline_frame(
            float(boundary.get("silence_end") or 0.0),
            fps,
            timeline_start_frame,
        )
        if next_speech_frame <= silence_end_frame:
            continue
        adjusted[index][1] = max(adjusted[index][0] + 1, silence_end_frame)
        adjusted[index + 1][0] = min(adjusted[index + 1][1] - 1, next_speech_frame)
        if adjusted[index + 1][0] > adjusted[index][1]:
            adjusted_boundaries.add(index)
    return [(start, end) for start, end in adjusted], adjusted_boundaries


def _same_generated_subtitle_speaker(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_speaker = left.get("speaker_track_index")
    right_speaker = right.get("speaker_track_index")
    return left_speaker == right_speaker or left_speaker is None or right_speaker is None


def _same_speech_island(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_island = str(left.get("speech_island_id") or "")
    right_island = str(right.get("speech_island_id") or "")
    return bool(left_island) and left_island == right_island


def attach_speech_islands_to_segments(
    segments: list[dict[str, Any]],
    speech_regions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    regions: list[tuple[int, float, float]] = []
    for index, region in enumerate(speech_regions or [], start=1):
        start = float(region.get("start") or 0.0)
        end = float(region.get("end") or start)
        if end > start:
            regions.append((index, start, end))
    if not regions:
        return [dict(segment) for segment in segments or []]

    attached: list[dict[str, Any]] = []
    for raw_segment in segments or []:
        segment = dict(raw_segment)
        if segment.get("speech_island_id"):
            attached.append(segment)
            continue
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or start)
        overlapping_regions = [
            region
            for region in regions
            if min(end, region[2]) - max(start, region[1]) > 0.0
        ]
        if len(overlapping_regions) == 1:
            region_index, region_start, region_end = overlapping_regions[0]
            segment["speech_island_id"] = f"shared:{region_index}:{region_start:.3f}-{region_end:.3f}"
            segment["speech_island_start"] = region_start
            segment["speech_island_end"] = region_end
        attached.append(segment)
    return attached


def attach_speech_islands_to_rows(
    rows: list[dict[str, Any]],
    speech_regions: list[dict[str, Any]] | None,
    fps: float,
    timeline_start_frame: int,
) -> list[dict[str, Any]]:
    rate = max(1.0, float(fps or 30.0))
    regions: list[tuple[int, int, int]] = []
    for index, region in enumerate(speech_regions or [], start=1):
        start_frame = seconds_to_timeline_frame(float(region.get("start") or 0.0), rate, timeline_start_frame)
        end_frame = seconds_to_timeline_frame(float(region.get("end") or 0.0), rate, timeline_start_frame)
        if end_frame > start_frame:
            regions.append((index, start_frame, end_frame))

    attached: list[dict[str, Any]] = []
    for raw_row in rows or []:
        row = dict(raw_row)
        if row.get("speech_island_id") and not str(row.get("speech_island_id")).startswith("shared:"):
            attached.append(row)
            continue
        row_start = int(row.get("start_frame") or 0)
        row_end = int(row.get("end_frame") or row_start + 1)
        best_region: tuple[int, int, int] | None = None
        best_overlap = 0
        for region in regions:
            overlap = max(0, min(row_end, region[2]) - max(row_start, region[1]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_region = region
        if best_region is not None and best_overlap > 0:
            region_index, region_start, region_end = best_region
            row["speech_island_id"] = f"shared:{region_index}:{region_start}-{region_end}"
            row["speech_island_start_frame"] = region_start
            row["speech_island_end_frame"] = region_end
        attached.append(row)
    return attached


def stabilize_generated_subtitle_rows(
    rows: list[dict[str, Any]],
    fps: float,
    subtitle_mode: str = "narration",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rate = max(1.0, float(fps or 30.0))
    minimum_frames = max(1, int(math.ceil(0.18 * rate)))
    maximum_anchor_frames = max(1, int(math.ceil(12.0 * rate / 30.0)))
    maximum_neighbor_gap = max(1, int(math.ceil(0.50 * rate)))
    soft_max_chars = 14 if subtitle_mode == "live" else GENERATED_SUBTITLE_MAX_CHARS
    output = sorted(
        [dict(row) for row in rows or []],
        key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)),
    )
    diagnostic = {
        "short_fragment_merged_count": 0,
        "short_utterance_kept_count": 0,
        "short_utterance_dropped_count": 0,
        "duplicate_suppressed_count": 0,
        "speech_island_timing_anchor_count": 0,
    }
    for row in output:
        row["_original_duration_frames"] = int(row.get("end_frame") or 0) - int(row.get("start_frame") or 0)

    island_rows: dict[str, list[dict[str, Any]]] = {}
    for row in output:
        island_id = str(row.get("speech_island_id") or "")
        if island_id:
            island_rows.setdefault(island_id, []).append(row)
    for grouped_rows in island_rows.values():
        first = grouped_rows[0]
        last = grouped_rows[-1]
        island_start = int(first.get("speech_island_start_frame") or first.get("start_frame") or 0)
        island_end = int(last.get("speech_island_end_frame") or last.get("end_frame") or 0)
        current_start = int(first.get("start_frame") or 0)
        current_end = int(last.get("end_frame") or current_start + 1)
        if 0 <= current_start - island_start <= maximum_anchor_frames:
            first["start_frame"] = island_start
            first["timing_anchor_source"] = "speech_island"
            diagnostic["speech_island_timing_anchor_count"] += 1
        if 0 <= island_end - current_end <= maximum_anchor_frames:
            last["end_frame"] = island_end
            last["timing_anchor_source"] = "speech_island"
            diagnostic["speech_island_timing_anchor_count"] += 1

    deduped: list[dict[str, Any]] = []
    for index, row in enumerate(output):
        text = normalize_ctc_text(row.get("text") or "")
        previous = deduped[-1] if deduped else None
        next_row = output[index + 1] if index + 1 < len(output) else None
        is_short = 0 < len(text) <= 2
        duplicate = False
        if is_short and previous and _same_generated_subtitle_speaker(previous, row):
            gap = int(row.get("start_frame") or 0) - int(previous.get("end_frame") or 0)
            previous_text = normalize_ctc_text(previous.get("text") or "")
            previous_island = str(previous.get("speech_island_id") or "")
            row_island = str(row.get("speech_island_id") or "")
            same_island = not previous_island and not row_island or previous_island == row_island
            duplicate = same_island and gap <= maximum_neighbor_gap and bool(previous_text) and previous_text.endswith(text)
        if is_short and not duplicate and next_row and _same_generated_subtitle_speaker(row, next_row):
            gap = int(next_row.get("start_frame") or 0) - int(row.get("end_frame") or 0)
            next_text = normalize_ctc_text(next_row.get("text") or "")
            row_island = str(row.get("speech_island_id") or "")
            next_island = str(next_row.get("speech_island_id") or "")
            same_island = not row_island and not next_island or row_island == next_island
            duplicate = same_island and gap <= maximum_neighbor_gap and bool(next_text) and next_text.startswith(text)
        if duplicate:
            diagnostic["duplicate_suppressed_count"] += 1
            continue
        deduped.append(row)

    stabilized: list[dict[str, Any]] = []
    index = 0
    while index < len(deduped):
        row = deduped[index]
        start_frame = int(row.get("start_frame") or 0)
        end_frame = int(row.get("end_frame") or start_frame + 1)
        duration = end_frame - start_frame
        text = normalize_ctc_text(row.get("text") or "")
        island_start = int(row.get("speech_island_start_frame") or start_frame)
        island_end = int(row.get("speech_island_end_frame") or end_frame)
        has_short_evidence = bool(row.get("short_utterance_confident")) or (
            float(row.get("speaker_dominance_db") or 0.0) >= LIVE_SPEAKER_SWITCH_MARGIN_DB
            and int(row.get("speaker_hold_frames") or 0) >= max(1, int(math.ceil(LIVE_SPEAKER_SWITCH_HOLD_SECONDS * rate)))
        )
        requires_short_evidence = subtitle_mode == "live" and 0 < len(text) <= 2
        if duration >= minimum_frames and (not requires_short_evidence or has_short_evidence):
            if (
                int(row.get("_original_duration_frames") or duration) < minimum_frames
                and len(text) <= 2
                and has_short_evidence
            ):
                diagnostic["short_utterance_kept_count"] += 1
            stabilized.append(row)
            index += 1
            continue

        confident_short = has_short_evidence and island_end - island_start >= minimum_frames
        if confident_short:
            previous_end = int(stabilized[-1].get("end_frame") or island_start) if stabilized else island_start
            next_start = int(deduped[index + 1].get("start_frame") or island_end) if index + 1 < len(deduped) else island_end
            lower_bound = max(island_start, previous_end)
            upper_bound = min(island_end, next_start)
            needed = max(0, minimum_frames - duration)
            expand_before = min((needed + 1) // 2, maximum_anchor_frames, max(0, start_frame - lower_bound))
            expand_after = min(needed - expand_before, maximum_anchor_frames, max(0, upper_bound - end_frame))
            remaining = needed - expand_before - expand_after
            if remaining > 0:
                expand_before += min(
                    remaining,
                    maximum_anchor_frames - expand_before,
                    max(0, start_frame - lower_bound - expand_before),
                )
            new_start = start_frame - expand_before
            new_end = end_frame + expand_after
            if new_end - new_start >= minimum_frames:
                row["start_frame"] = new_start
                row["end_frame"] = new_end
                row["timing_anchor_source"] = "speech_island"
                diagnostic["short_utterance_kept_count"] += 1
                stabilized.append(row)
                index += 1
                continue

        next_row = deduped[index + 1] if index + 1 < len(deduped) else None
        if (
            next_row
            and _same_generated_subtitle_speaker(row, next_row)
            and _same_speech_island(row, next_row)
            and generated_subtitle_effective_length(str(row.get("text") or "") + str(next_row.get("text") or "")) <= soft_max_chars
        ):
            merged = dict(next_row)
            merged["text"] = str(row.get("text") or "") + str(next_row.get("text") or "")
            merged["start_frame"] = start_frame
            merged["end_frame"] = max(end_frame, int(next_row.get("end_frame") or end_frame))
            diagnostic["short_fragment_merged_count"] += 1
            deduped[index + 1] = merged
            index += 1
            continue

        previous = stabilized[-1] if stabilized else None
        if (
            previous
            and _same_generated_subtitle_speaker(previous, row)
            and _same_speech_island(previous, row)
            and generated_subtitle_effective_length(str(previous.get("text") or "") + str(row.get("text") or "")) <= soft_max_chars
        ):
            previous["text"] = str(previous.get("text") or "") + str(row.get("text") or "")
            previous["end_frame"] = max(int(previous.get("end_frame") or 0), end_frame)
            diagnostic["short_fragment_merged_count"] += 1
        else:
            diagnostic["short_utterance_dropped_count"] += 1
        index += 1

    for row_index, row in enumerate(stabilized, start=1):
        row["index"] = row_index
        row.pop("_original_duration_frames", None)
    return stabilized, diagnostic


def merge_generated_subtitle_segments_for_learning(
    segments: list[dict[str, Any]],
    max_gap_seconds: float = GENERATED_SUBTITLE_LEARNED_MERGE_GAP_SECONDS,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for segment in segments or []:
        current = dict(segment)
        if not merged:
            merged.append(current)
            continue
        previous = merged[-1]
        gap = float(current.get("start") or 0.0) - float(previous.get("end") or 0.0)
        previous_speaker = previous.get("speaker_track_index")
        current_speaker = current.get("speaker_track_index")
        same_speaker = previous_speaker == current_speaker or previous_speaker is None or current_speaker is None
        previous_island = str(previous.get("speech_island_id") or "")
        current_island = str(current.get("speech_island_id") or "")
        same_island = not previous_island and not current_island or previous_island == current_island
        if (
            gap <= max_gap_seconds
            and gap >= -0.05
            and same_speaker
            and same_island
            and previous.get("words")
            and current.get("words")
        ):
            previous_text = str(previous.get("text") or "")
            current_text = str(current.get("text") or "")
            separator = " " if previous_text[-1:].isascii() and current_text[:1].isascii() else ""
            previous["text"] = previous_text + separator + current_text
            previous["end"] = max(float(previous.get("end") or 0.0), float(current.get("end") or 0.0))
            previous["words"] = [*(previous.get("words") or []), *(current.get("words") or [])]
            continue
        merged.append(current)
    return merged


def _replace_normalized_text_span(text: str, start: int, end: int, replacement: str) -> str | None:
    if start < 0 or end <= start:
        return None
    consumed = 0
    raw_start: int | None = None
    raw_end: int | None = None
    for index, char in enumerate(str(text or "")):
        normalized_length = len(normalize_ctc_text(char))
        if raw_start is None and consumed == start:
            raw_start = index
        consumed += normalized_length
        if consumed == end:
            raw_end = index + 1
            break
        if consumed > end:
            return None
    if raw_start is None or raw_end is None:
        return None
    return text[:raw_start] + replacement + text[raw_end:]


def apply_segmentation_text_corrections_to_chunks(
    chunks: list[str],
    profile: dict[str, Any],
) -> tuple[list[str], list[int]]:
    output = list(chunks)
    correction_counts = [0] * len(output)
    for correction in profile.get("text_corrections") or []:
        source = normalize_ctc_text(correction.get("source") or "")
        replacement = str(correction.get("replacement") or "")
        left_context = normalize_ctc_text(correction.get("left_context") or "")
        right_context = normalize_ctc_text(correction.get("right_context") or "")
        if not source or not replacement:
            continue

        normalized_chunks = [normalize_ctc_text(chunk) for chunk in output]
        full_text = "".join(normalized_chunks)
        search_start = 0
        while True:
            position = full_text.find(source, search_start)
            if position < 0:
                break
            source_end = position + len(source)
            visible_left = full_text[max(0, position - len(left_context)):position]
            visible_right = full_text[source_end:source_end + len(right_context)]
            left_matches = not left_context or (visible_left and left_context.endswith(visible_left))
            right_matches = not right_context or (visible_right and right_context.startswith(visible_right))
            full_context_on_one_side = visible_left == left_context or visible_right == right_context
            if (
                not left_matches
                or not right_matches
                or not full_context_on_one_side
                or len(visible_left) + len(visible_right) < 4
            ):
                search_start = position + 1
                continue

            chunk_start = 0
            applied = False
            for chunk_index, normalized_chunk in enumerate(normalized_chunks):
                chunk_end = chunk_start + len(normalized_chunk)
                if chunk_start <= position and source_end <= chunk_end:
                    corrected = _replace_normalized_text_span(
                        output[chunk_index],
                        position - chunk_start,
                        source_end - chunk_start,
                        replacement,
                    )
                    if corrected is not None:
                        output[chunk_index] = corrected
                        correction_counts[chunk_index] += 1
                        applied = True
                    break
                chunk_start = chunk_end
            if not applied:
                search_start = position + 1
                continue
            break
    return output, correction_counts


def distribute_generated_subtitle_frames_from_words(
    words: list[dict[str, Any]],
    chunks: list[str],
    fps: float,
    timeline_start_frame: int,
) -> list[tuple[int, int]] | None:
    timed_units = generated_subtitle_timed_units(words)
    chunk_unit_counts = [len(normalize_ctc_text(chunk)) for chunk in chunks]
    if not timed_units or any(count <= 0 for count in chunk_unit_counts):
        return None
    required_units = sum(chunk_unit_counts)
    if required_units <= 0 or len(timed_units) < required_units:
        return None

    rate = float(fps or 30.0)
    frames: list[tuple[int, int]] = []
    cursor = 0
    for index, count in enumerate(chunk_unit_counts):
        remaining_units = sum(chunk_unit_counts[index + 1 :])
        take = min(count, len(timed_units) - cursor - remaining_units)
        if take <= 0:
            return None
        unit_slice = timed_units[cursor : cursor + take]
        cursor += take
        start_frame = seconds_to_timeline_frame(unit_slice[0]["start"], rate, int(timeline_start_frame))
        end_frame = seconds_to_timeline_frame(unit_slice[-1]["end"], rate, int(timeline_start_frame))
        if frames and start_frame < frames[-1][1]:
            start_frame = frames[-1][1]
        frames.append((start_frame, max(start_frame + 1, end_frame)))

    return frames


def clamp_generated_subtitle_frame_range(
    start_frame: int,
    end_frame: int,
    text: str,
    fps: float,
) -> tuple[int, int]:
    rate = max(1.0, float(fps or 30.0))
    char_count = max(1, generated_subtitle_text_length(text))
    max_duration_seconds = min(
        GENERATED_SUBTITLE_MAX_DURATION_SECONDS,
        max(GENERATED_SUBTITLE_MIN_DURATION_SECONDS, char_count * GENERATED_SUBTITLE_SECONDS_PER_CHAR),
    )
    max_frames = max(1, int(math.ceil(max_duration_seconds * rate)))
    return start_frame, min(max(start_frame + 1, end_frame), start_frame + max_frames)


def generate_subtitle_rows_from_segments(
    segments: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int = 0,
    max_chars: int = GENERATED_SUBTITLE_MAX_CHARS,
    segmentation_profile: dict[str, Any] | None = None,
    speech_regions: list[dict[str, Any]] | None = None,
    subtitle_mode: str = "narration",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rate = float(fps or 30.0)
    mode_profile = segmentation_profile_for_mode(segmentation_profile, subtitle_mode)
    learned_mode = bool(mode_profile)
    island_segments = (
        attach_speech_islands_to_segments(segments, speech_regions)
        if subtitle_mode == "narration"
        else [dict(segment) for segment in segments or []]
    )
    source_segments = (
        merge_generated_subtitle_segments_for_learning(island_segments)
        if learned_mode
        else island_segments
    )
    for source_segment_index, segment in enumerate(source_segments, start=1):
        text = str(segment.get("text") or "").strip()
        start_seconds = float(segment.get("start") or 0.0)
        end_seconds = float(segment.get("end") or start_seconds)
        if not text or end_seconds <= start_seconds:
            continue
        segmentation_diagnostic: dict[str, Any] = {"used": False, "reason": "legacy_rules"}
        if learned_mode:
            chunks, segmentation_diagnostic = split_generated_subtitle_text_learned(
                text,
                segment.get("words") or [],
                mode_profile or {},
                speech_regions=speech_regions,
                subtitle_mode=subtitle_mode,
            )
        else:
            chunks = split_generated_subtitle_text(text, max_chars=max_chars)
            chunks = split_generated_subtitle_chunks_by_word_gaps(chunks, segment.get("words") or [])
        chunks = [
            chunk
            for chunk in chunks
            if strip_generated_subtitle_punctuation(chunk) and len(normalize_ctc_text(chunk)) > 0
        ]
        if not chunks:
            continue
        segment_start_frame = seconds_to_timeline_frame(start_seconds, rate, int(timeline_start_frame))
        segment_end_frame = seconds_to_timeline_frame(end_seconds, rate, int(timeline_start_frame))
        frame_ranges = distribute_generated_subtitle_frames_from_words(
            segment.get("words") or [],
            chunks,
            rate,
            int(timeline_start_frame),
        )
        used_word_timing = frame_ranges is not None
        if frame_ranges is None:
            frame_ranges = distribute_generated_subtitle_frames(segment_start_frame, segment_end_frame, chunks)
        frame_ranges, forced_silence_row_boundaries = apply_forced_silence_to_frame_ranges(
            frame_ranges,
            chunks,
            segmentation_diagnostic.get("forced_silence_boundaries") or [],
            rate,
            int(timeline_start_frame),
        )
        if learned_mode:
            output_chunks, text_correction_counts = apply_segmentation_text_corrections_to_chunks(
                chunks,
                mode_profile or {},
            )
        else:
            output_chunks = chunks
            text_correction_counts = [0] * len(chunks)
        # zip(..., strict=True) 仅 Python 3.10+ 支持；显式等长校验兼容 3.8/3.9。
        if len(chunks) != len(output_chunks) or len(chunks) != len(text_correction_counts) or len(chunks) != len(frame_ranges):
            raise ValueError(
                "ASR 文本校正结果长度不一致: chunks=%d output=%d corrections=%d ranges=%d"
                % (len(chunks), len(output_chunks), len(text_correction_counts), len(frame_ranges))
            )
        for chunk_index, (chunk, output_chunk, correction_count, (start_frame, end_frame)) in enumerate(zip(
            chunks,
            output_chunks,
            text_correction_counts,
            frame_ranges,
        )):
            output_text = strip_generated_subtitle_punctuation(output_chunk)
            if not output_text:
                continue
            if used_word_timing:
                timing_text = strip_generated_subtitle_punctuation(chunk)
                start_frame, end_frame = clamp_generated_subtitle_frame_range(start_frame, end_frame, timing_text, rate)
            row = {
                "index": len(rows) + 1,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "text": output_text,
                "source_segment_index": source_segment_index,
            }
            if segmentation_diagnostic.get("used"):
                row["segmentation_decision"] = "learned_dp"
                row["segmentation_score"] = segmentation_diagnostic.get("score")
                row["segmentation_example_score"] = segmentation_diagnostic.get("matched_example_score")
                row["text_correction_count"] = correction_count
                row["forced_silence_break"] = (
                    chunk_index in forced_silence_row_boundaries
                    or (chunk_index - 1) in forced_silence_row_boundaries
                )
            for field in (
                "speaker_track_index",
                "speaker_score_db",
                "speaker_dominance_db",
                "speaker_hold_frames",
                "speaker_decision",
                "speech_island_id",
                "short_utterance_confident",
            ):
                if field in segment:
                    row[field] = segment[field]
            if segment.get("speech_island_start") is not None:
                row["speech_island_start_frame"] = seconds_to_timeline_frame(
                    float(segment.get("speech_island_start") or 0.0),
                    rate,
                    int(timeline_start_frame),
                )
            if segment.get("speech_island_end") is not None:
                row["speech_island_end_frame"] = seconds_to_timeline_frame(
                    float(segment.get("speech_island_end") or 0.0),
                    rate,
                    int(timeline_start_frame),
                )
            rows.append(row)
    if subtitle_mode == "narration":
        rows = attach_speech_islands_to_rows(rows, speech_regions, rate, int(timeline_start_frame))
    if not learned_mode:
        rows = rebalance_generated_subtitle_row_short_tails(rows, max_chars=max_chars)
    if subtitle_mode == "narration" and any(row.get("speech_island_id") for row in rows):
        rows, _diagnostic = stabilize_generated_subtitle_rows(rows, rate, subtitle_mode="narration")
    return rows


def milliseconds_to_srt_time(milliseconds: int) -> str:
    value = max(0, int(milliseconds))
    hours = value // 3_600_000
    value %= 3_600_000
    minutes = value // 60_000
    value %= 60_000
    seconds = value // 1000
    millis = value % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def write_subtitle_rows_to_srt(path: Path, rows: list[dict[str, Any]], fps: float, base_frame: int = 0) -> int:
    rate = float(fps or 30.0)
    sorted_rows = sorted(rows or [], key=lambda row: int(row.get("start_frame") or 0))
    lines: list[str] = []
    written = 0
    for row in sorted_rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start_frame = int(row.get("start_frame") or 0)
        end_frame = int(row.get("end_frame") or start_frame + 1)
        if end_frame <= start_frame:
            end_frame = start_frame + 1
        start_ms = int(math.floor(((start_frame - base_frame) / rate) * 1000 + 0.5))
        end_ms = int(math.floor(((end_frame - base_frame) / rate) * 1000 + 0.5))
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        written += 1
        lines.extend(
            [
                str(written),
                f"{milliseconds_to_srt_time(start_ms)} --> {milliseconds_to_srt_time(end_ms)}",
                text,
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return written


def audio_duration_seconds(audio_path: Path) -> float:
    """Return a WAV file's duration in seconds.

    A readable but zero-frame WAV legitimately reports 0.0 (silent/empty
    cut). Any file ``wave`` cannot open is a real pipeline failure here --
    every caller passes audio this tool itself produced with ffmpeg -- so
    such cases raise instead of silently returning 0.0 and causing
    downstream zero-length subtitles or a bogus "audio too long" pass.
    """
    try:
        with wave.open(str(audio_path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 1
            return frames / float(rate)
    except (wave.Error, EOFError, OSError) as exc:
        raise RuntimeError(f"无法读取音频时长（文件缺失或已损坏）: {audio_path}") from exc


def qwen3_language_name(language: str | None) -> str | None:
    value = str(language or "").strip().lower().replace("_", "-")
    if not value or value == "auto":
        return None
    mapping = {
        "zh": "Chinese",
        "zh-cn": "Chinese",
        "chinese": "Chinese",
        "cmn": "Chinese",
        "yue": "Cantonese",
        "cantonese": "Cantonese",
        "en": "English",
        "en-us": "English",
        "en-gb": "English",
        "english": "English",
    }
    return mapping.get(value, language)


def qwen3_torch_dtype(torch_module: Any) -> Any:
    dtype_name = str(os.getenv("SUBFIX_QWEN3_ASR_DTYPE") or "bfloat16").strip()
    if dtype_name == "auto":
        return None
    return getattr(torch_module, dtype_name, None)


def qwen3_timestamp_value(item: Any, *names: str) -> Any:
    if isinstance(item, dict):
        for name in names:
            if name in item:
                return item[name]
        return None
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return None


def qwen3_timestamp_segments(value: Any) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
            return
        text = str(qwen3_timestamp_value(node, "text", "word") or "").strip()
        start = qwen3_timestamp_value(node, "start_time", "start", "begin")
        end = qwen3_timestamp_value(node, "end_time", "end", "finish")
        if text and start is not None and end is not None:
            try:
                start_float = float(start)
                end_float = float(end)
            except (TypeError, ValueError):
                return
            if end_float > start_float:
                segments.append({"start": start_float, "end": end_float, "text": text})

    visit(value)
    return segments


def qwen3_timestamp_words(value: Any) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
            return
        for container_key in ("items", "words", "tokens", "segments"):
            items = qwen3_timestamp_value(node, container_key)
            if items is not None and items is not node:
                visit(items)
                return
        text = str(qwen3_timestamp_value(node, "text", "word") or "").strip()
        start = qwen3_timestamp_value(node, "start_time", "start", "begin")
        end = qwen3_timestamp_value(node, "end_time", "end", "finish")
        if text and start is not None and end is not None:
            try:
                start_float = float(start)
                end_float = float(end)
            except (TypeError, ValueError):
                return
            if end_float <= start_float:
                end_float = start_float + 0.001
            words.append({"word": text, "start": start_float, "end": end_float})

    visit(value)
    return words


def normalize_rows_payload(rows: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows or [], start=1):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start_frame = int(float(row.get("start_frame") or 0))
        end_frame = int(float(row.get("end_frame") or start_frame + 1))
        normalized.append(
            {
                "index": int(row.get("index") or index),
                "text": text,
                "start_frame": start_frame,
                "end_frame": max(start_frame + 1, end_frame),
            }
        )
    return normalized


def load_rows(rows_json: str | None) -> list[dict[str, Any]]:
    if not rows_json:
        return []
    payload = json.loads(Path(rows_json).read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    return normalize_rows_payload(rows)


def load_batch_plan(batch_plan_json: str | None) -> list[dict[str, Any]]:
    if not batch_plan_json:
        return []
    payload = json.loads(Path(batch_plan_json).read_text(encoding="utf-8"))
    raw_batches = payload.get("batches") if isinstance(payload, dict) else payload
    batches: list[dict[str, Any]] = []
    for index, batch in enumerate(raw_batches or [], start=1):
        rows = normalize_rows_payload(batch.get("rows") or [])
        if not rows:
            continue
        batches.append(
            {
                "batch_id": str(batch.get("batch_id") or index),
                "audio": str(batch.get("audio") or batch.get("audio_path") or ""),
                "source_start": float(batch.get("source_start") or 0.0),
                "source_end": float(batch["source_end"]) if batch.get("source_end") is not None else None,
                "timeline_start_frame": int(float(batch.get("timeline_start_frame") or 0)),
                "timeline_end_frame": int(float(batch["timeline_end_frame"])) if batch.get("timeline_end_frame") is not None else None,
                "audio_channel_index": int(batch["audio_channel_index"]) if int(batch.get("audio_channel_index") or 0) > 0 else None,
                "fps": float(batch.get("fps") or 30.0),
                "rows": rows,
            }
        )
    return batches


def load_generate_subtitles_batch_plan(batch_plan_json: str | None) -> list[dict[str, Any]]:
    if not batch_plan_json:
        return []
    payload = json.loads(Path(batch_plan_json).read_text(encoding="utf-8"))
    raw_batches = payload.get("batches") if isinstance(payload, dict) else payload
    batches: list[dict[str, Any]] = []
    for index, batch in enumerate(raw_batches or [], start=1):
        if not isinstance(batch, dict):
            continue
        source_start = float(batch.get("source_start") or 0.0)
        source_end = batch.get("source_end")
        source_end_float = float(source_end) if source_end is not None else None
        if source_end_float is not None and source_end_float <= source_start:
            continue
        batches.append(
            {
                "batch_id": str(batch.get("batch_id") or index),
                "audio": str(batch.get("audio") or batch.get("audio_path") or ""),
                "source_start": source_start,
                "source_end": source_end_float,
                "timeline_start_frame": int(float(batch.get("timeline_start_frame") or 0)),
                "timeline_end_frame": int(float(batch["timeline_end_frame"])) if batch.get("timeline_end_frame") is not None else None,
                "fps": float(batch.get("fps") or 30.0),
                "audio_channel_index": int(batch["audio_channel_index"]) if batch.get("audio_channel_index") else None,
                "track_order": int(float(batch.get("track_order") or batch.get("track_index") or 0)),
                "track_index": int(float(batch.get("track_index") or 0)),
                "track_name": str(batch.get("track_name") or ""),
                "item_index": int(float(batch.get("item_index") or 0)),
            }
        )
    return batches


def load_transcribe_windows(windows_json: str | None) -> list[dict[str, Any]]:
    if not windows_json:
        return []
    payload = json.loads(Path(windows_json).read_text(encoding="utf-8"))
    raw_windows = payload.get("windows") if isinstance(payload, dict) else payload
    windows: list[dict[str, Any]] = []
    for index, window in enumerate(raw_windows or [], start=1):
        if not isinstance(window, dict):
            continue
        source_start = float(window.get("source_start") or 0.0)
        source_end = window.get("source_end")
        source_end_float = float(source_end) if source_end is not None else None
        if source_end_float is not None and source_end_float <= source_start:
            continue
        windows.append(
            {
                "window_id": str(window.get("window_id") or index),
                "source_start": source_start,
                "source_end": source_end_float,
                "row_label": str(window.get("row_label") or ""),
                "review_type": str(window.get("review_type") or ""),
            }
        )
    return windows


def build_alignment_text(rows: list[dict[str, Any]]) -> str:
    return "\n".join(str(row.get("text") or "").strip() for row in rows if str(row.get("text") or "").strip())


def normalize_ctc_text(text: str) -> str:
    value = str(text or "").lower()
    value = re.sub(r"[\s\-_—–]+", "", value)
    value = re.sub(r"[，。！？、；：,.!?;:\"'“”‘’（）()《》【】\[\]{}<>…·/\\|]+", "", value)
    return value


def ctc_units_for_rows(rows: list[dict[str, Any]]) -> list[list[str]]:
    units: list[list[str]] = []
    for row in rows:
        row_units = list(normalize_ctc_text(str(row.get("text") or "")))
        if not row_units:
            raise RuntimeError(f"CTC 对齐文本为空: 字幕 #{row.get('index', len(units) + 1)}")
        units.append(row_units)
    return units


def alignment_unit_count(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def split_aligned_segments_by_rows(rows: list[dict[str, Any]], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timed_words: list[dict[str, Any]] = []
    for segment in segments:
        for word in segment.get("words") or []:
            word_text = str(word.get("word") or word.get("text") or "").strip()
            word_start = float(word.get("start") or 0)
            word_end = float(word.get("end") or word_start)
            units = alignment_unit_count(word_text)
            if units > 0 and word_end > word_start:
                timed_words.append({"word": word_text, "start": word_start, "end": word_end, "units": units})

    if not timed_words:
        raise RuntimeError(f"stable-ts 分段数量不匹配: 字幕 {len(rows)} 条，对齐结果 {len(segments)} 段，且无 word 时间戳可重切")

    row_units = [max(1, alignment_unit_count(str(row.get("text") or ""))) for row in rows]
    total_row_units = sum(row_units)
    total_word_units = sum(word["units"] for word in timed_words)
    if total_row_units <= 0 or total_word_units <= 0:
        raise RuntimeError("stable-ts word 时间戳不可用于重切字幕行")

    unit_ratio = total_word_units / total_row_units
    split_segments: list[dict[str, Any]] = []
    word_index = 0
    consumed_target_units = 0
    consumed_word_units = 0

    if len(rows) != len(row_units):
        raise ValueError("split_segments 行数不一致: rows=%d row_units=%d" % (len(rows), len(row_units)))
    for row, units in zip(rows, row_units):
        start_index = min(word_index, len(timed_words) - 1)
        consumed_target_units += units
        target_word_units = max(consumed_word_units + 1, int(round(consumed_target_units * unit_ratio)))

        end_index = start_index
        while end_index < len(timed_words) - 1 and consumed_word_units + timed_words[end_index]["units"] < target_word_units:
            consumed_word_units += timed_words[end_index]["units"]
            end_index += 1

        if end_index == len(timed_words) - 1:
            consumed_word_units = total_word_units
        else:
            consumed_word_units += timed_words[end_index]["units"]

        start_word = timed_words[start_index]
        end_word = timed_words[end_index]
        split_segments.append(
            {
                "index": row.get("index"),
                "start": start_word["start"],
                "end": end_word["end"],
                "text": row.get("text") or "",
            }
        )
        word_index = min(end_index + 1, len(timed_words) - 1)

    return split_segments


def apply_alignment_to_rows(
    rows: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
    allow_non_monotonic: bool = False,
) -> list[dict[str, Any]]:
    if len(segments) != len(rows):
        segments = split_aligned_segments_by_rows(rows, segments)

    if len(rows) != len(segments):
        raise ValueError("对齐段数与字幕行数不一致: rows=%d segments=%d" % (len(rows), len(segments)))
    aligned: list[dict[str, Any]] = []
    previous_start: int | None = None
    for row, segment in zip(rows, segments):
        segment_start = float(segment.get("start") or 0)
        segment_end = float(segment.get("end") or segment_start)
        if segment_end <= segment_start:
            raise RuntimeError("stable-ts 返回空时间段")
        start_frame = int(round(timeline_start_frame + segment_start * fps))
        original_duration = max(1, int(row["end_frame"]) - int(row["start_frame"]))
        non_monotonic_candidate = previous_start is not None and start_frame < previous_start
        if non_monotonic_candidate and not allow_non_monotonic:
            raise RuntimeError("stable-ts 返回非单调时间")
        if not non_monotonic_candidate:
            previous_start = start_frame
        aligned_row = {
            "index": row["index"],
            "text": row["text"],
            "start": round(segment_start, 3),
            "end": round(segment_end, 3),
            "start_frame": start_frame,
            "end_frame": start_frame + original_duration,
            "original_start_frame": row["start_frame"],
            "original_end_frame": row["end_frame"],
        }
        if non_monotonic_candidate:
            aligned_row["non_monotonic_candidate"] = True
        if "ctc_start_frame" in segment:
            aligned_row["ctc_start_frame"] = int(segment["ctc_start_frame"])
        if "ctc_end_frame" in segment:
            aligned_row["ctc_end_frame"] = int(segment["ctc_end_frame"])
        for key in (
            "ctc_confidence",
            "ctc_char_count",
            "alignment_mode",
            "row_remap_score",
            "row_remap_decision",
            "remap_text_candidate",
            "row_remap_target",
        ):
            if key in segment:
                aligned_row[key] = segment[key]
        aligned.append(aligned_row)
    pad_aligned_rows_display(aligned, fps, timeline_start_frame)
    return aligned


def pad_aligned_rows_display(
    aligned_rows: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
    lead_frames: int | None = None,
    tail_frames: int | None = None,
) -> list[dict[str, Any]]:
    """调整字幕“显示”时机，使其更贴合听感而非严格卡在语音边界上：

      * 在对应音频开始前的 lead_frames（默认 5）帧出现；
      * 在对应音频结束后的 tail_frames（默认 5）帧消失；
      * 若下一句紧随而来（扩展后会与下一句重叠），则在下一句开始前 1 帧切掉，
        避免出现两句字幕重叠。

    仅修改最终展示用的 start_frame/end_frame（及其对应的 start/end 秒数），
    不触碰 alignment 内部使用的 ctc_* 字段。
    """
    rate = max(1.0, float(fps or 30.0))
    if lead_frames is None:
        lead_frames = int(os.getenv("SUBFIX_DISPLAY_LEAD_FRAMES", "5") or 5)
    if tail_frames is None:
        tail_frames = int(os.getenv("SUBFIX_DISPLAY_TAIL_FRAMES", "5") or 5)
    tl = int(timeline_start_frame or 0)
    lead_frames = max(0, int(lead_frames))
    tail_frames = max(0, int(tail_frames))

    for row in aligned_rows:
        try:
            sf = int(round(float(row.get("start_frame") or 0)))
            ef = int(round(float(row.get("end_frame") or sf + 1)))
        except (TypeError, ValueError):
            continue
        new_sf = max(tl, sf - lead_frames)
        new_ef = ef + tail_frames
        row["start_frame"] = new_sf
        row["end_frame"] = new_ef
        new_start_sec = (new_sf - tl) / rate
        new_end_sec = (new_ef - tl) / rate
        row["start"] = round(new_start_sec, 3)
        row["end"] = round(max(new_start_sec + 0.001, new_end_sec), 3)

    n = len(aligned_rows)
    for i in range(n - 1):
        try:
            cur_end = int(round(float(aligned_rows[i].get("end_frame") or 0)))
            nxt_start = int(round(float(aligned_rows[i + 1].get("start_frame") or 0)))
        except (TypeError, ValueError):
            continue
        if cur_end >= nxt_start:
            sf_i = int(round(float(aligned_rows[i].get("start_frame") or 0)))
            # 至少保留 1 帧时长，避免 end_frame==start_frame 被下游当作无效行丢弃。
            cur_end = max(nxt_start - 1, sf_i + 1)
            aligned_rows[i]["end_frame"] = cur_end
            aligned_rows[i]["end"] = round((cur_end - tl) / rate, 3)
    return aligned_rows


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def decode_pcm_samples(raw: bytes, sample_width: int, channels: int) -> list[float]:
    if sample_width != 2:
        raise RuntimeError(f"unsupported wav sample width: {sample_width}")
    step = sample_width * channels
    samples = []
    for offset in range(0, len(raw) - step + 1, step):
        channel_values = [
            int.from_bytes(raw[offset + channel * sample_width : offset + (channel + 1) * sample_width], "little", signed=True)
            for channel in range(channels)
        ]
        samples.append(sum(channel_values) / (len(channel_values) * 32768.0))
    return samples


def qwen_timestamp_items_to_char_units(timestamp_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for item in timestamp_items or []:
        char_text = normalize_ctc_text(str(item.get("word") or item.get("text") or ""))
        if not char_text:
            continue
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or start)
        raw_score = item.get("score")
        if raw_score is None:
            raw_score = item.get("confidence")
        score = float(raw_score) if raw_score is not None else 1.0
        for char in char_text:
            units.append({"char": char, "start": start, "end": end, "score": score})
    return units


def row_remap_length_tolerance(target_length: int) -> int:
    return max(3, int(math.ceil(max(1, target_length) * 0.25)))


def best_local_row_span(
    target_text: str,
    units: list[dict[str, Any]],
    cursor: int,
) -> dict[str, Any] | None:
    target_length = len(target_text)
    if target_length <= 0 or not units:
        return None
    length_tolerance = row_remap_length_tolerance(target_length)
    start_min = max(0, cursor - QWEN_ROW_REMAP_MAX_START_LOOKBACK)
    start_max = min(len(units) - 1, cursor + QWEN_ROW_REMAP_MAX_START_LOOKAHEAD)
    min_span_length = max(1, target_length - length_tolerance)
    max_span_length = max(min_span_length, target_length + length_tolerance)
    best: dict[str, Any] | None = None

    for start_index in range(start_min, start_max + 1):
        for span_length in range(min_span_length, max_span_length + 1):
            end_index = start_index + span_length
            if end_index > len(units):
                break
            candidate_text = "".join(str(unit.get("char") or "") for unit in units[start_index:end_index])
            if not candidate_text:
                continue
            score = difflib.SequenceMatcher(None, target_text, candidate_text).ratio()
            cursor_distance = abs(start_index - cursor)
            adjusted_score = score - (cursor_distance * 0.001)
            if (
                best is None
                or adjusted_score > best["adjusted_score"]
                or (
                    adjusted_score == best["adjusted_score"]
                    and abs(len(candidate_text) - target_length) < best["length_diff"]
                )
                or (
                    adjusted_score == best["adjusted_score"]
                    and abs(len(candidate_text) - target_length) == best["length_diff"]
                    and cursor_distance < best["cursor_distance"]
                )
            ):
                best = {
                    "start_index": start_index,
                    "end_index": end_index,
                    "text": candidate_text,
                    "score": score,
                    "adjusted_score": adjusted_score,
                    "cursor_distance": cursor_distance,
                    "length_diff": abs(len(candidate_text) - target_length),
                }
    return best


def preserved_row_segment(
    row: dict[str, Any],
    fps: float,
    timeline_start_frame: int,
    decision: str,
    score: float,
    candidate_text: str,
    target_text: str,
) -> dict[str, Any]:
    start_frame = int(round(float(row.get("start_frame") or 0)))
    end_frame = int(round(float(row.get("end_frame") or start_frame + 1)))
    start = max(0.0, (float(start_frame) - float(timeline_start_frame or 0)) / max(float(fps), 1.0))
    end = max(start + 0.001, (float(end_frame) - float(timeline_start_frame or 0)) / max(float(fps), 1.0))
    return {
        "index": row.get("index"),
        "start": round(start, 3),
        "end": round(end, 3),
        "text": row.get("text") or "",
        "ctc_start_frame": start_frame,
        "ctc_end_frame": max(end_frame, start_frame + 1),
        "ctc_confidence": round(max(0.0, min(1.0, float(score or 0.0))), 4),
        "ctc_char_count": len(target_text),
        "row_remap_score": round(max(0.0, min(1.0, float(score or 0.0))), 4),
        "row_remap_decision": decision,
        "remap_text_candidate": candidate_text,
        "row_remap_target": target_text,
        "alignment_mode": "qwen3_forced_aligner",
    }


def repair_rejected_row_timings(
    segments: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
    collapse_timestamp: float | None = None,
    speech_end: float | None = None,
    speech_onset: float | None = None,
    speech_regions: list[dict[str, float]] | None = None,
    first_rejected_start: float | None = None,
) -> None:
    """把被拒（回退到“名义占位槽”）的行重排到相邻已对齐行之间的空隙里。

    原实现直接沿用输入行的名义帧位（总时长 / 行数），会产生两类问题：
      * 首行被拒时停在时间轴 0 附近，与第 2 行之间留下大段空白（开头对不上）；
      * 被拒行按行号插到已对齐行中间，造成时间倒挂（起止时间小于前一行）。
    这里改为：按“连续被拒行段”处理，用整体语速估算时长，把该段紧贴
    下一已对齐行的起点放入空隙，从而消除大段空白与倒挂。
    对于尾部被拒段，若已知真实语音终点 speech_end，则用它作为右边界，
    确保尾部字幕能延伸到语音结束，而非仅凭语速估算提前收尾。
    对于开头被拒段，若已知真实语音起点 speech_onset，则用它作为左锚点
    并左对齐排布，避免首行被塞到第二行前面而晚于实际语音数秒。
    """
    n = len(segments)
    if n == 0:
        return
    accepted = [s.get("row_remap_decision") == "accepted_local_match" for s in segments]
    if all(accepted) or not any(accepted):
        return

    def fnum(index: int, key: str) -> float:
        try:
            return float(segments[index].get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    ok_index = [i for i in range(n) if accepted[i]]
    total_chars = sum(max(1, int(segments[i].get("ctc_char_count") or 1)) for i in ok_index)
    total_speech = sum(max(0.0, fnum(i, "end") - fnum(i, "start")) for i in ok_index)
    # 语速估算：优先用 accepted 行的时间跨度（首行起点到末行终点），避免各行时长
    # 之和因间隙/重叠产生偏差；跨度无效时回退到时和，仍无效则用 4.0。
    rate = 4.0
    if total_chars > 0:
        first_ok = ok_index[0]
        last_ok = ok_index[-1]
        span = max(0.0, fnum(last_ok, "end") - fnum(first_ok, "start"))
        span_rate = (total_chars / span) if span > 1e-6 else 0.0
        sum_rate = (total_chars / total_speech) if total_speech > 1e-6 else 0.0
        candidates = [r for r in (span_rate, sum_rate) if 1.0 <= r <= 20.0]
        if candidates:
            rate = sorted(candidates)[len(candidates) // 2]
        elif 1.0 <= sum_rate <= 20.0:
            rate = sum_rate
        elif 1.0 <= span_rate <= 20.0:
            rate = span_rate

    gap = 0.08
    min_dur = 0.6
    i = 0
    while i < n:
        if accepted[i]:
            i += 1
            continue
        run_start = i
        while i < n and not accepted[i]:
            i += 1
        run_end = i - 1
        run = list(range(run_start, run_end + 1))
        chars = [max(1, int(segments[k].get("ctc_char_count") or 1)) for k in run]
        chars_sum = float(sum(chars))

        is_leading = run_start == 0
        is_tail = run_end + 1 >= n
        left = fnum(run_start - 1, "end") if run_start > 0 else 0.0
        prev_end = left if run_start > 0 else 0.0
        # 开头被拒段：若已知真实语音起点，用它作为左锚点，避免首行被塞到
        # 第二行前面而晚于实际语音数秒。
        if is_leading and speech_onset is not None and speech_onset > left:
            left = speech_onset
        # 尾部被拒段：用坍缩点作为左锚点。
        # ASR 坍缩点是强制对齐器停止分配逐字时间戳的位置，代表被拒段语音的实际起点。
        # 首行被拒行的首字 ASR 时间戳（first_rejected_start）在坍缩区附近已退化
        # （单字时长异常偏大），不可靠，仅在无坍缩点时作为回退。
        if is_tail and collapse_timestamp is not None and collapse_timestamp > left:
            if collapse_timestamp - left <= 3.0:
                left = collapse_timestamp
            else:
                left = left + 0.25
        elif is_tail and first_rejected_start is not None and first_rejected_start > left:
            left = first_rejected_start
        right = fnum(run_end + 1, "start") if not is_tail else left + chars_sum / rate + gap
        # 尾部被拒段：用真实语音终点作为右边界
        if is_tail and speech_end is not None:
            right = speech_end

        est = chars_sum / rate
        window = right - left - gap
        # 尾部被拒段：按 VAD 语音区分布文字，跳过静音间隙。
        # 均匀铺满整个窗口会把字幕放到静音区、压缩语音区字幕，导致全部不对齐。
        use_vad_distribution = False
        if is_tail and window > 0.0 and speech_regions:
            tail_regions = []
            for region in speech_regions:
                rs = max(left, float(region["start"]))
                re = min(right, float(region["end"]))
                if re - rs > 0.05:
                    tail_regions.append((rs, re))
            # 合并间隙 < 0.5s 的相邻语音区，减少 VAD 误判（短暂音量下降）导致的虚假间隙
            merged: list[tuple[float, float]] = []
            for rs, re in sorted(tail_regions):
                if merged and rs - merged[-1][1] <= 0.5:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], re))
                else:
                    merged.append((rs, re))
            total_speech = sum(re - rs for rs, re in merged)
            if total_speech > 1.0 and len(merged) >= 1:
                use_vad_distribution = True

        if use_vad_distribution:
            total = total_speech
        elif window <= 0.0:
            total = max(min_dur * len(run), est)
        elif est > window:
            total = window
        elif is_tail and window > 0.0:
            total = window
        else:
            total = max(est, min(min_dur * len(run), window))

        # DEBUG: log repair tail run
        if is_tail:
            try:
                with open(os.path.expanduser("~\\subfix_remap_debug.log"), "a", encoding="utf-8") as _dlf:
                    _dlf.write(f"[repair_tail] run={run_start}-{run_end} chars_sum={chars_sum:.0f} "
                               f"rate={rate:.3f} est={est:.3f} left={left:.3f} right={right:.3f} "
                               f"window={window:.3f} total={total:.3f} vad={'yes' if use_vad_distribution else 'no'} "
                               f"regions={len(merged) if use_vad_distribution else 0} "
                               f"first_rej_start={first_rejected_start} collapse={collapse_timestamp}\n")
                    if use_vad_distribution:
                        for _ri, (_rs, _re) in enumerate(merged):
                            _dlf.write(f"  [vad_region {_ri}] {_rs:.3f}-{_re:.3f} dur={_re-_rs:.3f}\n")
            except Exception:
                pass

        # 分布被拒行
        if use_vad_distribution:
            # 阶段1：把行按字数比例分配到各 VAD 语音区
            region_rows: list[list[int]] = [[] for _ in merged]  # 每个区放哪些 run 索引
            run_idx = 0
            chars_remaining = chars_sum
            for region_idx in range(len(merged)):
                rs, re = merged[region_idx]
                region_dur = re - rs
                region_chars = chars_sum * (region_dur / total_speech)
                region_chars_consumed = 0.0
                # 最后一个区：把剩余所有行都放进去，避免末尾留白
                if region_idx == len(merged) - 1:
                    while run_idx < len(run):
                        region_rows[region_idx].append(run_idx)
                        run_idx += 1
                    break
                while run_idx < len(run) and region_chars_consumed < region_chars:
                    region_rows[region_idx].append(run_idx)
                    region_chars_consumed += chars[run_idx]
                    chars_remaining -= chars[run_idx]
                    run_idx += 1

            # 阶段2：每个区内按实际字数均匀铺满整个区域时长
            for region_idx in range(len(merged)):
                rs, re = merged[region_idx]
                region_dur = re - rs
                idxs = region_rows[region_idx]
                if not idxs:
                    continue
                actual_chars = sum(chars[i] for i in idxs)
                region_cursor = rs
                for pos, idx in enumerate(idxs):
                    k = run[idx]
                    c = chars[idx]
                    # 按实际字数比例分配时长，确保填满整个区域
                    duration = region_dur * (c / max(actual_chars, 0.001))
                    seg_start = max(region_cursor, rs)
                    # 最后一行延伸到区域末尾，不留白
                    if pos == len(idxs) - 1:
                        seg_end = re
                    else:
                        seg_end = min(max(seg_start + 0.05, seg_start + duration), re)
                    segment = segments[k]
                    segment["start"] = round(seg_start, 3)
                    segment["end"] = round(seg_end, 3)
                    start_frame = int(round(timeline_start_frame + seg_start * fps))
                    segment["ctc_start_frame"] = start_frame
                    segment["ctc_end_frame"] = max(int(round(timeline_start_frame + seg_end * fps)), start_frame + 1)
                    region_cursor = seg_end
        else:
            # 原有均匀分布逻辑（开头段/中间段/无语音区信息的尾部）
            cursor = left
            for k, c in zip(run, chars):
                duration = total * (c / chars_sum)
                seg_start = cursor
                seg_end = max(cursor + 0.05, cursor + duration)
                segment = segments[k]
                segment["start"] = round(seg_start, 3)
                segment["end"] = round(seg_end, 3)
                start_frame = int(round(timeline_start_frame + seg_start * fps))
                segment["ctc_start_frame"] = start_frame
                segment["ctc_end_frame"] = max(int(round(timeline_start_frame + seg_end * fps)), start_frame + 1)
                cursor = seg_end


def qwen3_remap_timestamp_items_to_rows(
    rows: list[dict[str, Any]],
    timestamp_items: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
    min_score: float = QWEN_ROW_REMAP_MIN_SCORE,
    speech_end: float | None = None,
    speech_onset: float | None = None,
    speech_regions: list[dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    units = qwen_timestamp_items_to_char_units(timestamp_items)
    row_units = ctc_units_for_rows(rows)
    segments: list[dict[str, Any]] = []
    # 检测 ASR 时间戳坍缩点：数组末尾连续退化的 unit 视为强制对齐失败。
    # 退化判定（三者任一即算退化，且必须从末尾连续）：
    #   1) 严格零时长（end - start <= 0.001）；
    #   2) 与后一个 unit 共用同一终点（时间戳“卡住”不再前进）；
    #   3) 时长远小于全段中位时长（压缩型退化，并非真正的零时长）。
    # 中间短暂的零时长 unit 是正常停顿，不算坍缩。span 延伸进尾部坍缩区的行一律拒绝，
    # 交由 repair_rejected_row_timings 按语速重排（或尾部切片重对齐）。
    collapse_idx = None
    if units:
        _durs = [max(0.0, float(u.get("end", 0)) - float(u.get("start", 0))) for u in units]
        _median_dur = percentile(_durs, 0.5) if _durs else 0.0
        _short_thresh = max(0.001, 0.2 * _median_dur) if _median_dur > 0 else 0.001
        _tail_run = 0
        for _i in range(len(units) - 1, -1, -1):
            _u = units[_i]
            _dur = _durs[_i]
            _degenerate = _dur <= 0.001
            if not _degenerate and _i < len(units) - 1:
                _prev = units[_i + 1]
                if abs(float(_u.get("end", 0)) - float(_prev.get("end", 0))) <= 0.001:
                    _degenerate = True
            if not _degenerate and _median_dur > 0 and _dur < _short_thresh:
                _degenerate = True
            if _degenerate:
                _tail_run += 1
            else:
                break
        if _tail_run >= 3:
            collapse_idx = len(units) - _tail_run
    # DEBUG: dump last 40 units timestamps once
    try:
        with open(r"C:\Users\10840\subfix_remap_debug.log", "a", encoding="utf-8") as _f:
            _f.write("=== units tail (last 40 of %d), collapse_idx=%s ===\n" % (len(units), collapse_idx))
            for _i in range(max(0, len(units) - 40), len(units)):
                _u = units[_i]
                _f.write("  [%d] %.3f-%.3f %s\n" % (_i, float(_u.get("start", 0)), float(_u.get("end", 0)), str(_u.get("text", ""))[:10]))
    except Exception:
        pass
    cursor = 0
    first_rejected_start: float | None = None

    if len(rows) != len(row_units):
        raise ValueError("Qwen 重映射行单元数不一致: rows=%d row_units=%d" % (len(rows), len(row_units)))
    for row, target_units in zip(rows, row_units):
        target_text = "".join(target_units)
        if not target_text:
            segments.append(
                preserved_row_segment(row, fps, timeline_start_frame, "rejected_empty_text", 0.0, "", target_text)
            )
            continue
        if not units or cursor >= len(units):
            # 音频时间戳已耗尽，后续文稿行无法对齐，标记为拒绝，由 repair_rejected_row_timings
            # 按语速估算并排到最后一条已对齐行之后，避免在尾部几个 unit 上反复假匹配导致全部挤在同一帧。
            segments.append(
                preserved_row_segment(row, fps, timeline_start_frame, "rejected_no_timestamp", 0.0, "", target_text)
            )
            continue

        best = best_local_row_span(target_text, units, cursor)
        candidate_text = str(best.get("text") or "") if best else ""
        score = float(best.get("score") or 0.0) if best else 0.0
        length_diff = int(best["length_diff"]) if best and "length_diff" in best else len(target_text)
        end_index = int(best["end_index"]) if best else 0
        # DEBUG: dump per-row remap trace + matched unit timestamps to hardcoded path
        try:
            with open(r"C:\Users\10840\subfix_remap_debug.log", "a", encoding="utf-8") as _f:
                _si = int(best.get("start_index", -1)) if best else -1
                _u0 = units[_si] if best and 0 <= _si < len(units) else {}
                _u1 = units[end_index - 1] if best and 0 < end_index <= len(units) else {}
                _f.write("row=%s cursor=%s len_units=%s start=%s end=%s score=%.4f diff=%s u0=(%.3f,%.3f) uN=(%.3f,%.3f) text=%s\n" % (
                    row.get("index"), cursor, len(units), _si, end_index, score, length_diff,
                    float(_u0.get("start", 0)), float(_u0.get("end", 0)),
                    float(_u1.get("start", 0)), float(_u1.get("end", 0)),
                    target_text[:20]))
        except Exception:
            pass
        # 必须向前推进（end_index > cursor）才接受；否则会在已消费的尾部 unit 上反复匹配，
        # 导致多行拿到相同时间戳而挤在一起。
        # 同时要求匹配 span 的实际时长 >= 50ms 且未延伸进 ASR 时间戳坍缩区：当 ASR 强制对齐
        # 在音频尾部退化（大量 unit 共享同一时间戳、零时长）时，该行会被拒绝，交由
        # repair_rejected_row_timings 按整体语速估算并排到上一已对齐行之后。
        span_dur = 0.0
        if best:
            _si2 = int(best["start_index"])
            _ei2 = int(best["end_index"])
            if 0 <= _si2 < len(units) and 0 < _ei2 <= len(units) and _ei2 > _si2:
                span_dur = float(units[_ei2 - 1].get("end", 0)) - float(units[_si2].get("start", 0))
        in_collapse = collapse_idx is not None and best and int(best["end_index"]) > collapse_idx
        accepted = (bool(best) and score >= min_score
                    and length_diff <= row_remap_length_tolerance(len(target_text))
                    and end_index > cursor
                    and span_dur >= 0.05
                    and not in_collapse)
        if not accepted:
            # 记录首个被拒行的 ASR 起点（若有匹配），供 repair 用作尾部左锚点。
            # 坍缩点不是被拒文字的实际起点，首行被拒行的首字时间戳才是。
            if first_rejected_start is None and best is not None:
                _si = int(best.get("start_index", -1))
                if 0 <= _si < len(units):
                    first_rejected_start = float(units[_si].get("start", 0))
            segments.append(
                preserved_row_segment(row, fps, timeline_start_frame, "rejected_low_score", score, candidate_text, target_text)
            )
            # Advance by the target length so one bad row does not poison later rows.
            cursor = min(len(units), cursor + len(target_text))
            continue

        start_index = int(best["start_index"])
        end_index = int(best["end_index"])
        row_chars = units[start_index:end_index]
        start = float(row_chars[0]["start"])
        end = max(float(row_chars[-1]["end"]), start + 0.001)
        ctc_start_frame = int(round(float(timeline_start_frame or 0) + start * float(fps)))
        ctc_end_frame = max(
            int(round(float(timeline_start_frame or 0) + end * float(fps))),
            ctc_start_frame + 1,
        )
        segments.append(
            {
                "index": row.get("index"),
                "start": round(start, 3),
                "end": round(end, 3),
                "text": row.get("text") or "",
                "ctc_start_frame": ctc_start_frame,
                "ctc_end_frame": ctc_end_frame,
                "ctc_confidence": round(score, 4),
                "ctc_char_count": len(target_text),
                "row_remap_score": round(score, 4),
                "row_remap_decision": "accepted_local_match",
                "remap_text_candidate": candidate_text,
                "row_remap_target": target_text,
                "alignment_mode": "qwen3_forced_aligner",
            }
        )
        cursor = end_index
    collapse_ts = float(units[collapse_idx].get("start", 0)) if collapse_idx is not None and 0 <= collapse_idx < len(units) else None
    repair_rejected_row_timings(segments, fps, timeline_start_frame, collapse_timestamp=collapse_ts, speech_end=speech_end, speech_onset=speech_onset, speech_regions=speech_regions, first_rejected_start=first_rejected_start)
    return segments


def detect_speech_regions(
    wav_path: Path,
    window_seconds: float = 0.01,
    merge_gap_seconds: float = 0.08,
    min_region_seconds: float = 0.08,
    hangover_seconds: float = 0.12,
) -> tuple[list[dict[str, float]], list[float], dict[str, Any]]:
    with wave.open(str(wav_path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())

    samples = decode_pcm_samples(raw, sample_width, channels)
    window_size = max(1, int(sample_rate * window_seconds))
    windows: list[dict[str, float]] = []
    for start in range(0, len(samples), window_size):
        chunk = samples[start : start + window_size]
        if not chunk:
            continue
        rms = math.sqrt(sum(sample * sample for sample in chunk) / len(chunk))
        peak = max(abs(sample) for sample in chunk)
        windows.append({"start": start / sample_rate, "end": min(len(samples), start + len(chunk)) / sample_rate, "rms": rms, "peak": peak})

    rms_values = [window["rms"] for window in windows]
    peak_values = [window["peak"] for window in windows]
    noise_floor = percentile(rms_values, 0.20)
    peak_floor = percentile(peak_values, 0.20)
    rms_on_threshold = max(0.0005, noise_floor * 3.0)
    rms_off_threshold = max(0.0003, noise_floor * 2.0)
    peak_on_threshold = max(0.0015, peak_floor * 3.0)
    peak_off_threshold = max(0.0008, peak_floor * 2.0)

    # 语音占比过高（>80%）时，20 百分位噪声底会落在语音区间内，导致阈值虚高、
    # 检测不到任何语音区。此时改用最大 RMS/peak 的一小部分作为兜底阈值。
    max_rms = max(rms_values) if rms_values else 0.0
    max_peak = max(peak_values) if peak_values else 0.0
    if max_rms > 0.0 and rms_on_threshold > 0.4 * max_rms:
        fallback = max(0.0005, 0.04 * max_rms)
        rms_on_threshold = fallback
        rms_off_threshold = max(0.0003, fallback * 0.66)
    if max_peak > 0.0 and peak_on_threshold > 0.4 * max_peak:
        fallback = max(0.0015, 0.04 * max_peak)
        peak_on_threshold = fallback
        peak_off_threshold = max(0.0008, fallback * 0.66)

    regions: list[dict[str, float]] = []
    active_start: float | None = None
    last_active_end: float | None = None
    inactive_seconds = 0.0
    for window in windows:
        starts_speech = window["rms"] >= rms_on_threshold or window["peak"] >= peak_on_threshold
        continues_speech = window["rms"] >= rms_off_threshold or window["peak"] >= peak_off_threshold
        if active_start is None and starts_speech:
            active_start = window["start"]
            last_active_end = window["end"]
            inactive_seconds = 0.0
        elif active_start is not None and continues_speech:
            last_active_end = window["end"]
            inactive_seconds = 0.0
        elif active_start is not None:
            inactive_seconds += max(0.0, float(window["end"]) - float(window["start"]))
            if inactive_seconds < hangover_seconds:
                continue
            if last_active_end is not None:
                regions.append({"start": active_start, "end": last_active_end})
            active_start = None
            last_active_end = None
            inactive_seconds = 0.0
    if active_start is not None and last_active_end is not None:
        regions.append({"start": active_start, "end": last_active_end})

    merged: list[dict[str, float]] = []
    for region in regions:
        if region["end"] - region["start"] < min_region_seconds:
            continue
        if merged and region["start"] - merged[-1]["end"] <= merge_gap_seconds:
            merged[-1]["end"] = max(merged[-1]["end"], region["end"])
        else:
            merged.append({"start": round(region["start"], 3), "end": round(region["end"], 3)})

    onsets = [region["start"] for region in merged]
    diagnostic = {
        "vad_mode": "hysteresis",
        "speech_region_count": len(merged),
        "speech_onset_count": len(onsets),
        "rms_threshold": round(rms_on_threshold, 6),
        "peak_threshold": round(peak_on_threshold, 6),
        "rms_on_threshold": round(rms_on_threshold, 6),
        "rms_off_threshold": round(rms_off_threshold, 6),
        "peak_on_threshold": round(peak_on_threshold, 6),
        "peak_off_threshold": round(peak_off_threshold, 6),
        "hangover_seconds": hangover_seconds,
        "sample_rate": sample_rate,
        "window_seconds": window_seconds,
    }
    return merged, onsets, diagnostic


def is_executable_file(path: str) -> bool:
    return bool(path) and Path(path).is_file() and os.access(path, os.X_OK)


def ffmpeg_candidates() -> list[str]:
    override = os.environ.get("SUBFIX_FFMPEG_CANDIDATES")
    if override is not None:
        return [part for part in override.split(os.pathsep) if part]
    return list(DEFAULT_FFMPEG_CANDIDATES)


def ensure_ffmpeg_path_env() -> None:
    candidate_dirs = [str(Path(candidate).parent) for candidate in ffmpeg_candidates()]
    existing_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    merged: list[str] = []
    for part in candidate_dirs + existing_parts:
        if part and part not in merged:
            merged.append(part)
    os.environ["PATH"] = os.pathsep.join(merged)


def resolve_ffmpeg(requested: str | None) -> str:
    ensure_ffmpeg_path_env()
    checked: list[str] = []
    if requested and requested != "ffmpeg":
        checked.append(requested)
        if is_executable_file(requested):
            return requested

    for candidate in ffmpeg_candidates():
        checked.append(candidate)
        if is_executable_file(candidate):
            return candidate

    path_match = shutil.which(requested or "ffmpeg") or shutil.which("ffmpeg")
    if path_match:
        return path_match

    checked_text = ", ".join(checked) if checked else "无固定候选路径"
    raise RuntimeError(
        "未找到 ffmpeg；已检查: "
        + checked_text
        + "；请重新安装/更新 SubFix 以恢复内置 ffmpeg，或运行 which ffmpeg 确认系统路径"
    )


def cut_audio(
    ffmpeg: str,
    audio: Path,
    output: Path,
    source_start: float,
    source_end: float | None,
    audio_channel_index: int | None = None,
) -> None:
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-ss", f"{source_start:.3f}"]
    if source_end is not None and source_end > source_start:
        cmd.extend(["-to", f"{source_end:.3f}"])
    cmd.extend(["-i", str(audio)])
    if audio_channel_index is not None and audio_channel_index > 0:
        cmd.extend(["-filter:a", f"pan=mono|c0=c{audio_channel_index - 1}"])
    cmd.extend(["-ac", "1", "-ar", "16000", "-vn", str(output)])
    # P7：切片超时兜底。正常 ffmpeg 切片远快于实时；按区间长度给 120-600 秒上限，
    # 防止源文件损坏/网络盘假死时 ffmpeg 永久挂住、整条生成流程卡死。
    if source_end is not None and source_end > source_start:
        duration = float(source_end) - float(source_start)
    else:
        duration = 60.0
    timeout_seconds = max(120, min(600, int(60 + 10 * duration)))
    # 必须显式按 UTF-8 解码：默认 locale 码页（GBK）遇到非 GBK 字节会让 subprocess 的
    # 读取线程直接崩溃（UnicodeDecodeError），父进程随之永久等待 → 进度卡死。
    try:
        subprocess.run(
            cmd,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        if source_end is not None:
            range_text = f"{float(source_start):.3f}s-{float(source_end):.3f}s"
        else:
            range_text = f"从 {float(source_start):.3f}s 起"
        raise RuntimeError(
            f"音频切片超时（已等待 {timeout_seconds} 秒）：源文件 {audio.name}（{range_text}）；"
            "可能是素材损坏或磁盘/网络盘繁忙，请检查该素材后重试"
        ) from exc


def transcribe_mlx_whisper(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    try:
        import mlx_whisper  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local setup
        raise RuntimeError("ASR 环境未安装，请先运行 setup_asr_env.sh 安装 mlx-whisper") from exc

    kwargs: dict[str, Any] = {"path_or_hf_repo": model, "word_timestamps": True}
    if language and language != "auto":
        kwargs["language"] = language
    try:
        return mlx_whisper.transcribe(str(audio_path), **kwargs)
    except TypeError:
        kwargs.pop("path_or_hf_repo", None)
        kwargs["model"] = model
        return mlx_whisper.transcribe(str(audio_path), **kwargs)


def transcribe_openai_whisper(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    try:
        import whisper  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local setup
        raise RuntimeError("OpenAI Whisper 环境未安装，请先运行 setup_asr_env.sh 安装 stable-ts/openai-whisper") from exc

    try:
        model_name = model or DEFAULT_MODEL
        whisper_model = _OPENAI_WHISPER_MODEL_CACHE.get(model_name)
        if whisper_model is None:
            whisper_model = whisper.load_model(model_name)
            _OPENAI_WHISPER_MODEL_CACHE[model_name] = whisper_model
        kwargs: dict[str, Any] = {
            "word_timestamps": True,
            "fp16": False,
            "temperature": 0,
            "condition_on_previous_text": False,
            "no_speech_threshold": 0.45,
            "logprob_threshold": -0.8,
            "compression_ratio_threshold": 2.4,
            "hallucination_silence_threshold": 1.0,
        }
        if language and language != "auto":
            kwargs["language"] = language
        try:
            return whisper_model.transcribe(str(audio_path), **kwargs)
        except TypeError as exc:
            if "hallucination_silence_threshold" not in str(exc):
                raise
            kwargs.pop("hallucination_silence_threshold", None)
            return whisper_model.transcribe(str(audio_path), **kwargs)
    except Exception as exc:  # pragma: no cover - depends on local setup/model cache
        raise RuntimeError(f"OpenAI Whisper 转写失败: {exc}") from exc


def _local_forced_aligner_model() -> str | None:
    """安装阶段已预下载的强制对齐器本地目录；存在则完全离线加载，不再访问 Hugging Face。"""
    data_root = os.getenv("LOCALAPPDATA") or os.path.join(
        str(Path.home()), "Library", "Application Support"
    )
    candidate = Path(data_root) / "SubFix" / "models" / "qwen3-forced-aligner-0.6b"
    try:
        if (candidate / "config.json").is_file():
            return str(candidate)
    except OSError:
        return None
    return None


def load_qwen3_asr_model(model: str) -> tuple[Any, str, str, str | None]:
    try:
        import torch  # type: ignore
        from qwen_asr import Qwen3ASRModel  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional local setup
        raise RuntimeError("Qwen3-ASR 环境未安装，请安装 qwen-asr 或继续使用 Whisper fallback") from exc

    model_name = os.getenv("SUBFIX_QWEN3_ASR_MODEL") or QWEN3_ASR_MODEL
    aligner_name = os.getenv("SUBFIX_QWEN3_ALIGNER_MODEL") or _local_forced_aligner_model() or QWEN3_FORCED_ALIGNER_MODEL
    device_map = os.getenv("SUBFIX_QWEN3_ASR_DEVICE_MAP") or "auto"
    dtype = qwen3_torch_dtype(torch)

    init_kwargs: dict[str, Any] = {
        "device_map": device_map,
        "max_inference_batch_size": int(os.getenv("SUBFIX_QWEN3_ASR_MAX_BATCH") or "8"),
        "max_new_tokens": int(os.getenv("SUBFIX_QWEN3_ASR_MAX_NEW_TOKENS") or "512"),
        "forced_aligner": aligner_name,
        "forced_aligner_kwargs": {"device_map": device_map},
    }
    if dtype is not None:
        init_kwargs["dtype"] = dtype
        init_kwargs["forced_aligner_kwargs"]["dtype"] = dtype

    cache_key = (model_name, aligner_name, device_map, str(dtype))
    try:
        cached_model = _QWEN3_ASR_MODEL_CACHE.get(cache_key)
        if cached_model is None:
            qwen_model = Qwen3ASRModel.from_pretrained(model_name, **init_kwargs)
            _QWEN3_ASR_MODEL_CACHE[cache_key] = (qwen_model, aligner_name, device_map)
        else:
            qwen_model, aligner_name, device_map = cached_model
    except Exception as exc:  # pragma: no cover - depends on optional local setup/model cache
        raise RuntimeError(f"Qwen3-ASR 模型加载失败: {exc}") from exc
    return qwen_model, model_name, aligner_name, device_map


def load_qwen3_forced_aligner() -> Any:
    try:
        import torch  # type: ignore
        from qwen_asr import Qwen3ForcedAligner  # type: ignore
    except Exception as exc:  # pragma: no cover - optional local runtime
        raise RuntimeError("Qwen3-ForcedAligner 环境未安装，请运行 setup_asr_env.sh") from exc

    model_name = os.getenv("SUBFIX_QWEN3_ALIGNER_MODEL") or _local_forced_aligner_model() or QWEN3_FORCED_ALIGNER_MODEL
    device_map = os.getenv("SUBFIX_QWEN3_ASR_DEVICE_MAP") or "auto"
    dtype = qwen3_torch_dtype(torch)
    cache_key = (model_name, device_map, str(dtype))
    cached = _QWEN3_FORCED_ALIGNER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    kwargs: dict[str, Any] = {"device_map": device_map}
    if dtype is not None:
        kwargs["dtype"] = dtype
    try:
        aligner = Qwen3ForcedAligner.from_pretrained(model_name, **kwargs)
    except Exception as exc:  # pragma: no cover - optional local runtime/model cache
        raise RuntimeError(f"Qwen3-ForcedAligner 模型加载失败: {exc}") from exc
    _QWEN3_FORCED_ALIGNER_CACHE[cache_key] = aligner
    return aligner


def qwen3_force_align_items(audio_path: Path, text: str, language: str) -> list[dict[str, Any]]:
    aligner = load_qwen3_forced_aligner()
    try:
        results = aligner.align(audio=str(audio_path), text=str(text), language=str(language))
    except Exception as exc:  # pragma: no cover - optional local runtime/model execution
        raise RuntimeError(f"Qwen3-ForcedAligner 对齐失败: {exc}") from exc
    first = results[0] if isinstance(results, list) and results else results
    items = qwen3_timestamp_value(first, "items") or first or []
    normalized: list[dict[str, Any]] = []
    for item in items:
        unit_text = str(qwen3_timestamp_value(item, "text", "word") or "")
        start = qwen3_timestamp_value(item, "start_time", "start")
        end = qwen3_timestamp_value(item, "end_time", "end")
        # 对齐器可能对个别字符给出 start == end 的零长度时间戳：旧逻辑用 “>” 会把
        # 整条丢掉，造成候选文本缺字（如目标“聚焦大模型”只匹配到“聚大模型”），
        # 行相似度被压低后误判为 rejected_low_score。这里放行零长度时间戳。
        if unit_text and start is not None and end is not None and float(end) >= float(start):
            normalized.append({"text": unit_text, "start": float(start), "end": float(end)})
    return normalized


def qwen3_result_to_payload(
    result: Any,
    audio_path: Path,
    requested_model: str,
    model_name: str,
    aligner_name: str,
    device_map: str,
    hotword_context_status: str = "not_requested",
) -> dict[str, Any]:
    text = str(qwen3_timestamp_value(result, "text") or "").strip()
    language_name = qwen3_timestamp_value(result, "language")
    timestamp_payload = qwen3_timestamp_value(result, "time_stamps", "timestamps", "segments")
    words = qwen3_timestamp_words(timestamp_payload)
    segments = qwen3_timestamp_segments(timestamp_payload)
    if text and words:
        segments = [{"start": words[0]["start"], "end": words[-1]["end"], "text": text, "words": words}]
    if not segments and text:
        duration = audio_duration_seconds(audio_path)
        segments = [{"start": 0.0, "end": max(0.01, duration), "text": text}]

    return {
        "backend": "qwen3_asr",
        "model": model_name,
        "language": language_name,
        "segments": segments,
        "text": text,
        "diagnostic": {
            "forced_aligner": aligner_name,
            "device_map": device_map,
            "requested_model": requested_model,
            "forced_align_item_count": len(words),
            "uses_word_timing": bool(words),
            "hotword_context_status": hotword_context_status,
        },
    }


def _transcribe_qwen3_model(
    qwen_model: Any, audio: str | list[str], language: str | None, context: str | None
) -> tuple[Any, str]:
    kwargs: dict[str, Any] = {
        "audio": audio,
        "language": qwen3_language_name(language),
        "return_time_stamps": True,
    }
    if not context:
        return qwen_model.transcribe(**kwargs), "not_requested"
    kwargs["context"] = context
    try:
        return qwen_model.transcribe(**kwargs), "used"
    except TypeError as exc:
        if "context" not in str(exc).lower():
            raise
        kwargs.pop("context", None)
        return qwen_model.transcribe(**kwargs), "unsupported"


def transcribe_qwen3_asr(
    audio_path: Path, model: str, language: str | None, context: str | None = None
) -> dict[str, Any]:
    qwen_model, model_name, aligner_name, device_map = load_qwen3_asr_model(model)
    try:
        results, hotword_context_status = _transcribe_qwen3_model(
            qwen_model, str(audio_path), language, context
        )
    except Exception as exc:  # pragma: no cover - depends on optional local setup/model cache
        raise RuntimeError(f"Qwen3-ASR 转写失败: {exc}") from exc
    first_result = results[0] if isinstance(results, list) and results else results
    return qwen3_result_to_payload(
        first_result, audio_path, model, model_name, aligner_name, device_map, hotword_context_status
    )


def transcribe_qwen3_asr_batch(
    audio_paths: list[Path], model: str, language: str | None, context: str | None = None
) -> list[dict[str, Any]]:
    if not audio_paths:
        return []
    qwen_model, model_name, aligner_name, device_map = load_qwen3_asr_model(model)
    try:
        results, hotword_context_status = _transcribe_qwen3_model(
            qwen_model, [str(path) for path in audio_paths], language, context
        )
    except Exception as exc:  # pragma: no cover - depends on optional local setup/model cache
        raise RuntimeError(f"Qwen3-ASR 批量转写失败: {exc}") from exc
    if not isinstance(results, list):
        results = [results]
    if len(results) != len(audio_paths):
        raise RuntimeError(f"Qwen3-ASR 批量结果数量不匹配: audio={len(audio_paths)} result={len(results)}")
    return [
        qwen3_result_to_payload(
            result, audio_path, model, model_name, aligner_name, device_map, hotword_context_status
        )
        for result, audio_path in zip(results, audio_paths)
    ]


def _doubao_credentials_path() -> Path:
    """Path to the optional doubao_credentials.json next to this helper."""
    return Path(__file__).with_name(DOUBAO_ASR_CREDENTIALS_FILENAME)


def _load_doubao_credentials_file() -> dict[str, Any]:
    """Read the single API Key from doubao_credentials.json if present.

    Returns an empty dict when the file is missing, unreadable, or not a JSON
    object -- callers fall back to raising a clear "no credentials" error.
    Never raises on a malformed/absent file so a stray file can't crash ASR.
    """
    try:
        raw = _doubao_credentials_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    api_key = str(data.get("api_key") or "").strip()
    return {"api_key": api_key} if api_key else {}


def _doubao_asr_request_once(
    endpoint: str,
    headers: dict[str, str],
    body_bytes: bytes,
    timeout_seconds: float,
) -> tuple[str, str, str]:
    """Issue a single HTTP POST and return (status_code, response_text, log_id).

    Uses the stdlib urllib (no new dependency) rather than the `requests`
    package, since `requests` is not otherwise imported by this module.

    log_id is Volcano's per-request trace id (X-Tt-Logid), returned so it can
    be recorded in the generate diagnostic -- with it the user can look up in
    the console / a support ticket whether a given call was actually billed
    and against which quota/tier.
    """
    request = urllib.request.Request(endpoint, data=body_bytes, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        # 待校准: 状态码文档记录在响应头 X-Api-Status-Code 中；如实际账号返回改为响应体
        # 顶层 code 字段，需要在此处同步兼容。
        status_code = response.headers.get("X-Api-Status-Code") or ""
        response_text = response.read().decode("utf-8", errors="replace")
        # 火山每个请求的唯一追踪 ID，用于账务/工单精确核对本次调用是否计费。
        log_id = response.headers.get("X-Tt-Logid") or response.headers.get("X-Api-Request-Id") or ""
    return status_code, response_text, log_id


def _doubao_asr_timestamp_seconds(value: Any) -> float | None:
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _doubao_asr_timestamp_payload(result: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(result, dict):
        return [], []
    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    for utterance in result.get("utterances") or []:
        if not isinstance(utterance, dict):
            continue
        start = _doubao_asr_timestamp_seconds(utterance.get("start_time"))
        end = _doubao_asr_timestamp_seconds(utterance.get("end_time"))
        if start is None or end is None or end <= start:
            continue
        segment_words: list[dict[str, Any]] = []
        for item in utterance.get("words") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("word") or "").strip()
            word_start = _doubao_asr_timestamp_seconds(item.get("start_time"))
            word_end = _doubao_asr_timestamp_seconds(item.get("end_time"))
            if text and word_start is not None and word_end is not None and word_end > word_start:
                word = {"word": text, "start": word_start, "end": word_end}
                segment_words.append(word)
                words.append(word)
        segments.append(
            {
                "start": start,
                "end": end,
                "text": str(utterance.get("text") or "").strip(),
                "words": segment_words,
            }
        )
    return segments, words


def transcribe_doubao_asr(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    """Transcribe via Volcano Engine (豆包) 录音文件极速版识别 HTTP API.

    Word timestamps are normalized from the API's milliseconds to seconds.
    The v4/v5 pipeline validates them before use and uses a deterministic local
    time mapping when they are missing or unreliable. Selecting 豆包 never
    requires the Qwen runtime.

    Configuration uses one API Key only. Legacy App ID / Access Token files
    are intentionally ignored so an empty API Key can never spend cloud quota.
    Optional (env only): SUBFIX_DOUBAO_RESOURCE_ID, SUBFIX_DOUBAO_ENDPOINT.

    Raises RuntimeError with a clear message on missing credentials, HTTP
    errors, timeouts, or malformed responses. Callers surface these failures
    to Resolve so the user can retry or explicitly switch to local Qwen.
    """
    api_key = str(os.getenv(DOUBAO_ASR_API_KEY_ENV) or "").strip()
    if not api_key:
        creds = _load_doubao_credentials_file()
        api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError(
            "豆包 ASR 未配置密钥：请设置环境变量 "
            f"{DOUBAO_ASR_API_KEY_ENV}，"
            f"或在 {_doubao_credentials_path()} 写入 "
            '{"api_key": "..."} 后重试'
        )
    resource_id = os.getenv(DOUBAO_ASR_RESOURCE_ID_ENV) or DOUBAO_ASR_DEFAULT_RESOURCE_ID
    endpoint = os.getenv(DOUBAO_ASR_ENDPOINT_ENV) or DOUBAO_ASR_DEFAULT_ENDPOINT

    audio_bytes = Path(audio_path).read_bytes()
    if not audio_bytes:
        raise RuntimeError(f"豆包 ASR 音频文件为空: {audio_path}")

    # 待校准: 请求体字段仅确认 user.uid / audio.data(或 audio.url) / request.model_name；
    # 文档 demo 未展示 audio.format/audio.codec 字段，暂不填写，若真实响应要求
    # 音频格式声明，需要在这里补充（cut_audio 输出固定为 16k/mono/wav）。
    body = {
        "user": {"uid": DOUBAO_ASR_CLIENT_UID},
        "audio": {"data": base64.b64encode(audio_bytes).decode("ascii")},
        "request": {"model_name": "bigmodel", "show_utterances": True},
    }
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    headers["X-Api-Key"] = api_key
    body_bytes = json.dumps(body).encode("utf-8")

    last_error: Exception | None = None
    for attempt in range(1, DOUBAO_ASR_MAX_ATTEMPTS + 1):
        try:
            status_code, response_text, log_id = _doubao_asr_request_once(
                endpoint, headers, body_bytes, DOUBAO_ASR_TIMEOUT_SECONDS
            )
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            last_error = RuntimeError(f"豆包 ASR HTTP {exc.code}: {exc.reason}; {error_body[:500]}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = RuntimeError(f"豆包 ASR 请求失败（网络/超时）: {exc}")
        else:
            try:
                response_payload = json.loads(response_text) if response_text else {}
            except json.JSONDecodeError as exc:
                # 网关/代理偶发返回 HTML 纯文本而非 API JSON：当作可重试错误，
                # 与错误状态码走同一条退避重试路径（两次尝试后再抛出）。
                last_error = RuntimeError(
                    f"豆包 ASR 响应不是合法 JSON（status={status_code or '-'}, log_id={log_id or '-'}）: "
                    f"{exc}; 响应片段: {response_text[:200]}"
                )
            else:
                if not isinstance(response_payload, dict):
                    last_error = RuntimeError("豆包 ASR 响应 JSON 必须是对象")
                else:
                    silent_audio = status_code == DOUBAO_ASR_SILENT_AUDIO_STATUS_CODE
                    if status_code and status_code != DOUBAO_ASR_SUCCESS_STATUS_CODE and not silent_audio:
                        message = str(response_payload.get("message") or response_text[:300])
                        last_error = RuntimeError(f"豆包 ASR 返回错误状态 {status_code}: {message}")
                    else:
                        result = {} if silent_audio else response_payload.get("result")
                        text = str((result or {}).get("text") or "").strip()
                        segments, words = _doubao_asr_timestamp_payload(result)
                        return {
                            "backend": "doubao_asr",
                            "model": model,
                            "language": language,
                            "segments": segments,
                            "words": words,
                            "text": text,
                            "diagnostic": {
                                "resource_id": resource_id,
                                "endpoint": endpoint,
                                "status_code": status_code,
                                "log_id": log_id,
                                "attempt": attempt,
                                "silent_audio": silent_audio,
                            },
                        }
        if attempt < DOUBAO_ASR_MAX_ATTEMPTS:
            time.sleep(DOUBAO_ASR_RETRY_BACKOFF_SECONDS * attempt)

    raise RuntimeError(str(last_error) if last_error else "豆包 ASR 请求失败: 未知错误")


def _doubao_asr_api_key() -> str:
    api_key = str(os.getenv(DOUBAO_ASR_API_KEY_ENV) or "").strip()
    if not api_key:
        api_key = str(_load_doubao_credentials_file().get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError(
            "豆包 ASR 未配置密钥：请设置环境变量 "
            f"{DOUBAO_ASR_API_KEY_ENV}，"
            f"或在 {_doubao_credentials_path()} 写入 "
            '{"api_key": "..."} 后重试'
        )
    return api_key


def _doubao_asr_v2_response_fields(response_payload: dict[str, Any], status_code: str) -> tuple[str, dict[str, Any]]:
    """Accept both the raw HTTP body and the documentation's body envelope."""
    body = response_payload.get("body")
    if not isinstance(body, dict):
        body = response_payload
    headers = response_payload.get("headers")
    if isinstance(headers, dict):
        status_code = status_code or str(headers.get("X-Api-Status-Code") or "")
    status_code = status_code or str(body.get("X-Api-Status-Code") or "")
    return status_code, body


def _doubao_asr_v2_post(
    endpoint: str,
    headers: dict[str, str],
    body: dict[str, Any],
    stage: str,
) -> tuple[str, dict[str, Any], str]:
    try:
        status_code, response_text, log_id = _doubao_asr_request_once(
            endpoint,
            headers,
            json.dumps(body).encode("utf-8"),
            DOUBAO_ASR_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            error_payload = json.loads(error_body) if error_body else {}
        except json.JSONDecodeError:
            error_payload = {}
        error_header = error_payload.get("header") if isinstance(error_payload, dict) else None
        if not isinstance(error_header, dict):
            error_header = {}
        if exc.code == 403 and str(error_header.get("code") or "") == "45000030":
            request_id = str(error_header.get("reqid") or "").strip()
            request_hint = f"；请求 ID：{request_id}" if request_id else ""
            raise RuntimeError(
                f"豆包标准版{stage}未获授权：当前 API Key 所属项目未开通"
                f"录音文件识别模型 2.0（{DOUBAO_ASR_V2_RESOURCE_ID}）。"
                f"请在同一项目开通服务，并重新复制该项目的 API Key{request_hint}"
            ) from exc
        raise RuntimeError(f"豆包标准版{stage} HTTP {exc.code}: {exc.reason}; {error_body[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"豆包标准版{stage}失败（网络/超时）: {exc}") from exc
    try:
        response_payload = json.loads(response_text) if response_text else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"豆包标准版{stage}响应不是合法 JSON: {exc}") from exc
    if not isinstance(response_payload, dict):
        raise RuntimeError(f"豆包标准版{stage}响应 JSON 必须是对象")
    return (*_doubao_asr_v2_response_fields(response_payload, status_code), log_id)


def transcribe_doubao_asr_v2(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    """Transcribe through 豆包录音文件识别模型 2.0 标准版's async API."""
    api_key = _doubao_asr_api_key()
    audio_bytes = Path(audio_path).read_bytes()
    if not audio_bytes:
        raise RuntimeError(f"豆包 ASR 音频文件为空: {audio_path}")

    submit_request_id = str(uuid.uuid4())
    submit_headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": DOUBAO_ASR_V2_RESOURCE_ID,
        "X-Api-Request-Id": submit_request_id,
        "X-Api-Sequence": "-1",
    }
    submit_body = {
        "audio": {
            "data": base64.b64encode(audio_bytes).decode("ascii"),
            "format": "wav",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "show_utterances": True,
            "enable_punc": True,
            "enable_itn": False,
            "enable_ddc": False,
            "enable_speaker_info": False,
            "enable_channel_split": False,
        },
    }
    submit_status, submit_payload, submit_log_id = _doubao_asr_v2_post(
        DOUBAO_ASR_V2_SUBMIT_ENDPOINT,
        submit_headers,
        submit_body,
        "任务提交",
    )
    if submit_status and submit_status != DOUBAO_ASR_SUCCESS_STATUS_CODE:
        message = str(submit_payload.get("X-Api-Message") or submit_payload.get("message") or "")
        raise RuntimeError(f"豆包标准版任务提交返回错误状态 {submit_status}: {message[:300]}")
    task_id = str(submit_payload.get("task_id") or submit_request_id).strip()
    if not task_id:
        raise RuntimeError("豆包标准版任务提交未返回 task_id")

    query_headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": DOUBAO_ASR_V2_RESOURCE_ID,
        "X-Api-Request-Id": task_id,
    }
    query_log_ids: list[str] = []
    query_status_codes: list[str] = []
    for attempt in range(1, DOUBAO_ASR_V2_MAX_QUERY_ATTEMPTS + 1):
        query_status, query_payload, query_log_id = _doubao_asr_v2_post(
            DOUBAO_ASR_V2_QUERY_ENDPOINT,
            query_headers,
            {},
            "结果查询",
        )
        if query_log_id:
            query_log_ids.append(query_log_id)
        if query_status:
            query_status_codes.append(query_status)
        if query_status and query_status not in DOUBAO_ASR_V2_NON_ERROR_STATUS_CODES:
            message = str(query_payload.get("X-Api-Message") or query_payload.get("message") or "")
            raise RuntimeError(f"豆包标准版结果查询返回错误状态 {query_status}: {message[:300]}")
        result = query_payload.get("result")
        if query_status == DOUBAO_ASR_SILENT_AUDIO_STATUS_CODE and not isinstance(result, dict):
            result = {}
        # Processing/queued responses may include an empty result object; only terminal states may consume it.
        if query_status in {
            DOUBAO_ASR_SUCCESS_STATUS_CODE,
            DOUBAO_ASR_SILENT_AUDIO_STATUS_CODE,
        } and isinstance(result, dict):
            segments, words = _doubao_asr_timestamp_payload(result)
            return {
                "backend": "doubao_asr_v2",
                "model": model,
                "language": language,
                "segments": segments,
                "words": words,
                "text": str(result.get("text") or "").strip(),
                "diagnostic": {
                    "resource_id": DOUBAO_ASR_V2_RESOURCE_ID,
                    "submit_endpoint": DOUBAO_ASR_V2_SUBMIT_ENDPOINT,
                    "query_endpoint": DOUBAO_ASR_V2_QUERY_ENDPOINT,
                    "task_id": task_id,
                    "status_code": query_status or submit_status,
                    "log_id": query_log_id or submit_log_id,
                    "submit_log_id": submit_log_id,
                    "query_log_ids": query_log_ids,
                    "query_status_codes": query_status_codes,
                    "attempt": attempt,
                },
            }
        if attempt < DOUBAO_ASR_V2_MAX_QUERY_ATTEMPTS:
            time.sleep(
                min(
                    DOUBAO_ASR_V2_QUERY_MAX_BACKOFF_SECONDS,
                    DOUBAO_ASR_V2_QUERY_BACKOFF_SECONDS * attempt,
                )
            )
    raise RuntimeError(f"豆包标准版等待结果超时（task_id={task_id}）")


def transcribe_v4_window_batch(
    window_audio_paths: list[Path],
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve per-window ASR text payloads for the v4/v5 main pipeline.

    This is the single dispatch point that keeps
    run_generate_subtitles_batch_plan_v4's first-step transcription
    switchable by backend while leaving segmentation, profiles, and
    SUBFIX_V5_WRITEBACK untouched:
      - default / any backend other than the explicit 豆包 backends: calls
        transcribe_qwen3_asr_batch exactly as before (zero behavior change).
      - `doubao_asr` / `doubao_asr_v2`: transcribe each window individually
        via the explicitly selected Volcano tier. A per-window failure is
        surfaced to Resolve with the affected filename and original cause;
        valid word timestamps are consumed by the existing alignment validator,
        otherwise it falls back to Qwen.

    Returns (payloads, diagnostic) where diagnostic carries
    asr_backend_used / doubao_fallback_count (+ error detail) for the
    generate-batch diagnostic dict (see sanitize_generate_diagnostic_payload
    for the whitelist these keys must be added to).
    """
    backend = str(getattr(args, "backend", "auto") or "auto")
    if backend not in {"doubao_asr", "doubao_asr_v2"}:
        hotword_context = str(getattr(args, "hotword_context", "") or "")
        payloads = (
            transcribe_qwen3_asr_batch(
                window_audio_paths, args.model, args.language, context=hotword_context
            )
            if hotword_context
            else transcribe_qwen3_asr_batch(window_audio_paths, args.model, args.language)
        )
        diagnostic = {
            "asr_backend_used": "qwen3_asr",
            "doubao_fallback_count": 0,
        }
        if hotword_context:
            context_statuses = {
                str((payload.get("diagnostic") or {}).get("hotword_context_status") or "not_requested")
                for payload in payloads
            }
            diagnostic["hotword_context_supported"] = "unsupported" not in context_statuses
        return payloads, diagnostic

    payloads = []
    doubao_log_ids: list[str] = []
    doubao_resource_ids: list[str] = []
    doubao_status_codes: list[str] = []
    for window_audio_path in window_audio_paths:
        try:
            transcribe = transcribe_doubao_asr_v2 if backend == "doubao_asr_v2" else transcribe_doubao_asr
            payload = transcribe(window_audio_path, args.model, args.language)
            payloads.append(payload)
            # 收集火山返回的追踪/计费线索，便于在诊断里核对每次调用是否真计费。
            call_diag = payload.get("diagnostic") or {}
            if call_diag.get("log_id"):
                doubao_log_ids.append(str(call_diag["log_id"]))
            if call_diag.get("resource_id"):
                doubao_resource_ids.append(str(call_diag["resource_id"]))
            if call_diag.get("status_code"):
                doubao_status_codes.append(str(call_diag["status_code"]))
        except Exception as exc:
            raise RuntimeError(
                f"豆包 ASR 失败（{Path(window_audio_path).name}）: {exc}"
            ) from exc
    diagnostic: dict[str, Any] = {
        "asr_backend_used": backend,
        "doubao_fallback_count": 0,
    }
    if doubao_log_ids:
        diagnostic["doubao_log_ids"] = doubao_log_ids
    if doubao_resource_ids:
        # 去重保序：同一批次通常同一个 resource_id。
        diagnostic["doubao_resource_ids"] = list(dict.fromkeys(doubao_resource_ids))
    if doubao_status_codes:
        diagnostic["doubao_status_codes"] = list(dict.fromkeys(doubao_status_codes))
    return payloads, diagnostic


def _split_external_command_template(command_template: str) -> list[str]:
    """Split an opt-in external ASR command template into argv tokens.

    POSIX systems use :func:`shlex.split` (single/double quote aware). On
    Windows, ``shlex.quote`` output (POSIX single quotes) is meaningless and
    ``shell=True`` invokes cmd.exe quoting rules, so we tokenize with
    ``posix=False`` -- which keeps Windows backslashes literal -- and strip one
    pair of enclosing quotes per token. The resulting argv is executed with
    ``shell=False`` so substituted paths never need quoting and can no longer
    alter the command structure.
    """
    if os.name == "nt":
        tokens: list[str] = []
        for token in shlex.split(command_template, posix=False):
            if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
                token = token[1:-1]
            tokens.append(token)
        return tokens
    return shlex.split(command_template, posix=True)


def _external_command_argv(command_template: str, values: dict[str, str]) -> list[str]:
    try:
        tokens = _split_external_command_template(command_template)
        argv = [token.format_map(values) for token in tokens]
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(
            "外部 ASR 命令模板无效：仅支持 {audio}/{output}/{model}/{language} 占位符"
        ) from exc
    if not argv or any(not token for token in argv):
        raise RuntimeError("外部 ASR 命令模板不能为空")
    return argv


def transcribe_external_backend(audio_path: Path, model: str, language: str | None, backend: str) -> dict[str, Any]:
    env_name = EXTERNAL_TRANSCRIBE_COMMAND_ENV.get(backend)
    command_template = os.getenv(env_name or "")
    if not command_template:
        raise RuntimeError(f"{backend} 未配置本地命令环境变量 {env_name}")

    with tempfile.TemporaryDirectory(prefix=f"subfix_{backend}_") as tmp_dir:
        output_path = Path(tmp_dir) / "transcribe.json"
        # argv 数组 + shell=False：路径不经过 shell 解释，Windows 下也不会因
        # POSIX 单引号规则失效（原先 shlex.quote + shell=True 在 Windows 必坏）。
        argv = _external_command_argv(
            command_template,
            {
                "audio": str(audio_path),
                "output": str(output_path),
                "model": str(model),
                "language": str(language or "auto"),
            },
        )
        result = subprocess.run(argv, shell=False, text=True, encoding="utf-8", errors="replace", capture_output=True)
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"{backend} 执行失败: {error_text}")
        if output_path.exists():
            payload_text = output_path.read_text(encoding="utf-8")
        else:
            payload_text = result.stdout or ""
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{backend} 输出不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{backend} 输出 JSON 必须是对象")
        if payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error") or f"{backend} 返回失败"))
        return payload


def transcribe_with_backend(
    audio_path: Path,
    model: str,
    language: str | None,
    backend: str = "auto",
    context: str | None = None,
) -> dict[str, Any]:
    requested_backend = backend or "auto"
    # "auto" never silently tries either paid/credential-gated 豆包 backend; it must
    # be requested explicitly. See AUTO_TRANSCRIBE_BACKENDS definition.
    candidates = list(AUTO_TRANSCRIBE_BACKENDS) if requested_backend == "auto" else [requested_backend]
    fallback_errors: list[str] = []

    for candidate in candidates:
        try:
            if candidate == "mlx_whisper":
                payload = transcribe_mlx_whisper(audio_path, model, language)
            elif candidate == "openai_whisper":
                payload = transcribe_openai_whisper(audio_path, model, language)
            elif candidate == "qwen3_asr":
                payload = (
                    transcribe_qwen3_asr(audio_path, model, language, context=context)
                    if context
                    else transcribe_qwen3_asr(audio_path, model, language)
                )
            elif candidate == "doubao_asr":
                payload = transcribe_doubao_asr(audio_path, model, language)
            elif candidate == "doubao_asr_v2":
                payload = transcribe_doubao_asr_v2(audio_path, model, language)
            elif candidate in EXTERNAL_TRANSCRIBE_COMMAND_ENV:
                payload = transcribe_external_backend(audio_path, model, language, candidate)
            else:
                raise RuntimeError(f"未知 ASR backend: {candidate}")
        except Exception as exc:
            fallback_errors.append(f"{candidate}: {exc}")
            continue

        payload = dict(payload)
        payload["backend"] = candidate
        payload["model"] = str(payload.get("model") or model)
        if fallback_errors:
            payload["fallback_errors"] = fallback_errors
        return payload

    raise RuntimeError("ASR backend 全部不可用: " + "；".join(fallback_errors))


DOUBAO_ASR_BACKENDS = frozenset({"doubao_asr", "doubao_asr_v2"})


def transcribe_v4_retry_audio(
    audio_path: Path,
    model: str,
    language: str | None,
    *,
    backend: str,
    hotword_context: str = "",
) -> dict[str, Any]:
    """Retry a v4/v5 window with the backend explicitly selected by the user."""
    if backend == "doubao_asr":
        return transcribe_doubao_asr(audio_path, model, language)
    if backend == "doubao_asr_v2":
        return transcribe_doubao_asr_v2(audio_path, model, language)
    return (
        transcribe_qwen3_asr(audio_path, model, language, context=hotword_context)
        if hotword_context
        else transcribe_qwen3_asr(audio_path, model, language)
    )


def align_v4_retry_audio(
    audio_path: Path,
    text: str,
    language: str,
    *,
    backend: str,
) -> list[dict[str, Any]]:
    """Provide fallback timestamps without crossing ASR backend boundaries."""
    if backend not in DOUBAO_ASR_BACKENDS:
        return qwen3_force_align_items(audio_path, text, language)
    clean_text = str(text or "").strip()
    if not clean_text:
        return []
    return [
        {
            "text": clean_text,
            "start": 0.0,
            "end": max(0.001, audio_duration_seconds(audio_path)),
        }
    ]


def qwen_generate_batch_size() -> int:
    raw_value = os.getenv("SUBFIX_QWEN_GENERATE_BATCH_SIZE")
    if raw_value:
        try:
            return max(1, int(raw_value))
        except ValueError:
            pass
    return 8


def recover_v4_asr_window(
    *,
    window: dict[str, Any],
    payload: dict[str, Any],
    track_audio_path: Path,
    track_start_frame: int,
    retry_dir: Path,
    model: str,
    language: str | None,
    transcribe_fn: Any = None,
    detect_speech_fn: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover a speech-bearing v4 window that ASR returned as punctuation only."""
    diagnostic = {
        "asr_recovery": "not_needed",
        "asr_single_retry_count": 0,
        "asr_subwindow_retry_count": 0,
        "asr_empty_speech_subwindow_count": 0,
        "speech_seconds": 0.0,
    }
    if generate_v4.normalize_text(payload.get("text")):
        return dict(payload), diagnostic

    transcribe_fn = transcribe_fn or transcribe_qwen3_asr
    detect_speech_fn = detect_speech_fn or detect_speech_regions
    # Cloud ASR may validly suppress noise that local VAD classifies as speech;
    # keep Qwen fail-fast while allowing explicit Doubao backends to continue.
    is_doubao_backend = str(payload.get("backend") or "") in DOUBAO_ASR_BACKENDS
    window_audio_path = Path(str(window.get("audio_path") or ""))
    speech_regions, _onsets, _speech_diagnostic = detect_speech_fn(window_audio_path)
    speech_seconds = sum(
        max(0.0, float(region.get("end") or 0.0) - float(region.get("start") or 0.0))
        for region in speech_regions or []
    )
    diagnostic["speech_seconds"] = round(speech_seconds, 3)
    if speech_seconds < 0.30:
        diagnostic["asr_recovery"] = "accepted_silence"
        return dict(payload), diagnostic

    diagnostic["asr_single_retry_count"] = 1
    serial_payload = dict(transcribe_fn(window_audio_path, model, language) or {})
    if generate_v4.normalize_text(serial_payload.get("text")):
        diagnostic["asr_recovery"] = "single_window"
        return serial_payload, diagnostic

    fps = float(window.get("fps") or 30.0)
    retry_windows = generate_v4.build_context_windows(
        int(window.get("start_frame") or 0),
        int(window.get("end_frame") or 0),
        fps,
        window_seconds=24.0,
        stride_seconds=20.0,
    )
    if len(retry_windows) < 2:
        if is_doubao_backend:
            diagnostic["asr_recovery"] = "accepted_doubao_empty_speech"
            return dict(serial_payload or payload), diagnostic
        raise generate_v4.V4AlignmentError("v4 有人声窗口转写为空，已终止写回")

    retry_dir = Path(retry_dir)
    retry_dir.mkdir(parents=True, exist_ok=True)
    texts: list[str] = []
    for retry_window in retry_windows:
        retry_path = retry_dir / (
            f"track_{int(window.get('track_index') or 0):03d}_"
            f"window_{int(window.get('window_index') or 0):04d}_"
            f"retry_{int(retry_window.get('window_index') or 0):04d}.wav"
        )
        generate_v4.write_context_window_audio(
            Path(track_audio_path),
            retry_path,
            int(track_start_frame),
            int(retry_window["start_frame"]),
            int(retry_window["end_frame"]),
            fps,
        )
        diagnostic["asr_subwindow_retry_count"] += 1
        retry_payload = dict(transcribe_fn(retry_path, model, language) or {})
        retry_text = str(retry_payload.get("text") or "")
        if not generate_v4.normalize_text(retry_text):
            retry_regions, _retry_onsets, _retry_diagnostic = detect_speech_fn(retry_path)
            retry_speech_seconds = sum(
                max(0.0, float(region.get("end") or 0.0) - float(region.get("start") or 0.0))
                for region in retry_regions or []
            )
            if retry_speech_seconds >= 0.30:
                diagnostic["asr_empty_speech_subwindow_count"] += 1
                if not is_doubao_backend:
                    raise generate_v4.V4AlignmentError("v4 有人声子窗口转写为空，已终止写回")
            continue
        texts.append(retry_text)

    merged_text = ""
    for text in texts:
        merged_text = generate_v4.merge_asr_transcripts(merged_text, text)
    if not generate_v4.normalize_text(merged_text):
        if is_doubao_backend:
            diagnostic["asr_recovery"] = "accepted_doubao_empty_speech"
            return dict(serial_payload or payload), diagnostic
        raise generate_v4.V4AlignmentError("v4 有人声窗口转写为空，已终止写回")

    recovered = dict(serial_payload or payload)
    recovered["text"] = merged_text
    recovered["segments"] = []
    diagnostic["asr_recovery"] = "short_subwindows"
    return recovered, diagnostic


def transcribe(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    return transcribe_mlx_whisper(audio_path, model, language)


def run_transcribe_windows(
    audio_path: Path,
    windows: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
) -> dict[str, Any]:
    if not windows:
        raise RuntimeError("没有可转写的局部窗口")
    ffmpeg_path = resolve_ffmpeg(args.ffmpeg)
    output_windows: list[dict[str, Any]] = []
    diagnostic: dict[str, Any] = {
        "mode": args.mode,
        "ffmpeg": ffmpeg_path,
        "window_count": len(windows),
        "requested_backend": args.backend,
    }
    backend = ""
    model = ""
    fallback_errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="subfix_asr_windows_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        for index, window in enumerate(windows, start=1):
            window_id = str(window.get("window_id") or index)
            source_start = float(window.get("source_start") or 0.0)
            source_end = window.get("source_end")
            source_end_float = float(source_end) if source_end is not None else None
            audio_channel_index = int(window.get("audio_channel_index")) if window.get("audio_channel_index") else None
            label = str(window.get("row_label") or window_id)
            write_progress(
                progress_path,
                "transcribe_window",
                f"局部复核 {index}/{len(windows)}｜字幕 {label}",
                batch_index=index,
                total_batches=len(windows),
            )
            cut_path = tmp_path / f"window_{index:04d}.wav"
            try:
                cut_audio(ffmpeg_path, audio_path, cut_path, source_start, source_end_float, audio_channel_index)
                speech_regions, speech_onsets, onset_diagnostic = detect_speech_regions(cut_path)
                raw_payload = transcribe_with_backend(cut_path, args.model, args.language, args.backend)
                raw_segments = normalize_segments(raw_payload)
                window_backend = str(raw_payload.get("backend") or "")
                window_model = str(raw_payload.get("model") or args.model)
                backend = backend or window_backend
                model = model or window_model
                if raw_payload.get("fallback_errors"):
                    fallback_errors = list(raw_payload.get("fallback_errors") or [])
                output_windows.append(
                    {
                        "ok": True,
                        "window_id": window_id,
                        "review_type": window.get("review_type") or "",
                        "row_label": label,
                        "source_start": source_start,
                        "source_end": source_end_float,
                        "backend": window_backend,
                        "model": window_model,
                        "segments": raw_segments,
                        "speech_onsets": speech_onsets,
                        "speech_regions": speech_regions,
                        "text": str(raw_payload.get("text") or "").strip(),
                        "diagnostic": {
                            "cut_audio_bytes": cut_path.stat().st_size if cut_path.exists() else 0,
                            "raw_segment_count": len(raw_payload.get("segments") or []),
                            "segment_count": len(raw_segments),
                            "text_length": len(str(raw_payload.get("text") or "").strip()),
                            **onset_diagnostic,
                        },
                    }
                )
            except Exception as exc:
                output_windows.append(
                    {
                        "ok": False,
                        "window_id": window_id,
                        "review_type": window.get("review_type") or "",
                        "row_label": label,
                        "source_start": source_start,
                        "source_end": source_end_float,
                        "error": str(exc),
                        "segments": [],
                    }
                )

    if fallback_errors:
        diagnostic["fallback_errors"] = fallback_errors
    return {
        "backend": backend,
        "model": model or args.model,
        "segments": [],
        "text": "",
        "windows": output_windows,
        "diagnostic": diagnostic,
    }


def select_adaptive_subtitle_candidates(
    units: list[dict[str, Any]],
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    started_at = time.monotonic()
    safe_regions, risky_regions = generate_v5.partition_adaptive_regions(units, fps)

    def overlapping(region: dict[str, Any]) -> list[dict[str, Any]]:
        start = int(region["start_frame"])
        end = int(region["end_frame"])
        return [
            dict(unit)
            for unit in units
            if int(unit.get("end_frame") or 0) > start and int(unit.get("start_frame") or 0) < end
        ]

    selected: list[dict[str, Any]] = []
    stage_diagnostic: dict[str, int] = {}

    def add_diagnostic(values: dict[str, Any]) -> None:
        for key, value in values.items():
            if isinstance(value, (int, float)):
                stage_diagnostic[key] = stage_diagnostic.get(key, 0) + int(value)

    for region in safe_regions:
        safe_units, overlap_diagnostic = generate_v4.dedupe_overlap_units(overlapping(region))
        selected.extend(safe_units)
        add_diagnostic(overlap_diagnostic)

    full_unit_count = 0
    for region in risky_regions:
        risky_units = overlapping(region)
        full_unit_count += len(risky_units)
        echo_filtered, echo_diagnostic = generate_v4.suppress_cross_mic_echo_regions(risky_units, fps)
        exclusive_units, exclusive_diagnostic = generate_v4.build_exclusive_unit_stream(echo_filtered, fps)
        filtered_units, near_duplicate_diagnostic = generate_v4.suppress_near_duplicate_units(exclusive_units, fps)
        selected.extend(filtered_units)
        add_diagnostic(echo_diagnostic)
        add_diagnostic(exclusive_diagnostic)
        add_diagnostic(near_duplicate_diagnostic)

    selected, final_overlap_diagnostic = generate_v4.dedupe_overlap_units(selected)
    add_diagnostic(final_overlap_diagnostic)
    selected.sort(
        key=lambda unit: (
            int(unit.get("start_frame") or 0),
            int(unit.get("end_frame") or 0),
            int(unit.get("track_index") or 0),
        )
    )
    return selected, {
        **stage_diagnostic,
        "adaptive_fast_unit_count": sum(len(overlapping(region)) for region in safe_regions),
        "adaptive_full_unit_count": full_unit_count,
        "adaptive_full_region_count": len(risky_regions),
        "adaptive_selection_elapsed_seconds": round(time.monotonic() - started_at, 6),
    }


def run_generate_subtitles_batch_plan_v4(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
    fixture_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_engine = str(getattr(args, "generate_engine", "v4") or "v4").lower()
    v5_mode = requested_engine == "v5"
    engine_label = "v5" if v5_mode else "v4"
    # Progress label for the per-window transcription step. It reflects the
    # requested ASR backend (识别模型) so the progress window doesn't always
    # read "Qwen" even when the user picked 豆包. Actual dispatch happens in
    # transcribe_v4_window_batch; recovery and alignment keep using the same
    # explicitly selected backend.
    backend_label = str(getattr(args, "backend", "auto") or "auto")
    asr_label = (
        "豆包 2.0" if backend_label == "doubao_asr_v2"
        else "豆包" if backend_label == "doubao_asr"
        else "Qwen"
    )
    if not batches:
        raise RuntimeError(f"缺少 generate_subtitles {engine_label} batch plan")
    if fixture_payload is not None:
        raise RuntimeError(f"{engine_label} fixture 必须提供独立 aligned-unit 测试入口")
    subtitle_mode = str(getattr(args, "subtitle_mode", "narration") or "narration")
    if subtitle_mode not in {"narration", "live"}:
        subtitle_mode = "narration"
    configured_hotword_entries = load_hotword_entries(getattr(args, "hotwords_json", None))
    hotword_entries = configured_hotword_entries[:HOTWORD_MAX_ENTRIES]
    hotword_context = build_hotword_context(hotword_entries)
    args.hotword_context = hotword_context
    uses_doubao_backend = backend_label in DOUBAO_ASR_BACKENDS
    retry_transcribe_fn = lambda audio_path, model_name, language_name: transcribe_v4_retry_audio(
        audio_path,
        model_name,
        language_name,
        backend=backend_label,
        hotword_context=hotword_context,
    )
    retry_align_fn = lambda audio_path, text, language: align_v4_retry_audio(
        audio_path,
        text,
        language,
        backend=backend_label,
    )
    ffmpeg_path = resolve_ffmpeg(args.ffmpeg)
    diagnostic: dict[str, Any] = {
        "mode": "generate_subtitles_batch",
        "requested_mode": getattr(args, "mode", "generate_subtitles_batch"),
        "generate_engine": engine_label,
        "subtitle_mode": subtitle_mode,
        "source_batch_count": len(batches),
        "batch_count": len(batches),
        "successful_batch_count": len(batches),
        "failed_batch_count": 0,
        "forced_align_retry_count": 0,
        "context_align_retry_count": 0,
        "alignment_repaired_unit_count": 0,
        "minimum_raw_alignment_coverage": 1.0,
        "unaligned_rejected_count": 0,
        "alignment_window_alternative_count": 0,
        "alignment_window_uncovered_count": 0,
        "overlap_unit_suppressed_count": 0,
        "window_seam_suppressed_count": 0,
        "cross_mic_boundary_serialized_count": 0,
        "cross_mic_echo_region_count": 0,
        "cross_mic_echo_suppressed_count": 0,
        "cross_mic_echo_ambiguous_count": 0,
        "cross_mic_echo_suppressed_unit_count": 0,
        "cross_mic_duplicate_count": 0,
        "near_duplicate_suppressed_count": 0,
        "short_speaker_flip_suppressed_count": 0,
        "asr_empty_speech_window_count": 0,
        "asr_tolerated_empty_speech_window_count": 0,
        "asr_empty_speech_subwindow_count": 0,
        "asr_single_retry_count": 0,
        "asr_subwindow_retry_count": 0,
        "asr_recovered_window_count": 0,
        "asr_unrecovered_window_count": 0,
        "local_retry_elapsed_seconds": 0.0,
        "overlong_tail_reclaimed_count": 0,
        "tail_extended_row_count": 0,
        "textnorm_changed_row_count": 0,
        "asr_backend_used": "qwen3_asr",
        "doubao_fallback_count": 0,
        "doubao_native_timestamp_window_count": 0,
        "doubao_native_timestamp_fallback_count": 0,
        "hotword_enabled": bool(getattr(args, "hotwords_json", None)),
        "hotword_entry_count": len(configured_hotword_entries),
        "hotword_injected_count": len(hotword_entries),
        "hotword_replacement_count": 0,
        "hotword_context_supported": True,
        "adaptive_fast_unit_count": 0,
        "adaptive_full_unit_count": 0,
        "adaptive_full_region_count": 0,
        "adaptive_selection_elapsed_seconds": 0.0,
    }
    profile: dict[str, Any] | None = None
    active_mode_profile: dict[str, Any] | None = None
    v5_writeback = v5_writeback_mode() if v5_mode else "shadow"
    if v5_mode:
        profile, profile_status = load_segmentation_profile_v5(
            getattr(args, "segmentation_profile", None)
        )
        diagnostic["profile_load_status"] = profile_status
        if profile_status != "loaded":
            raise RuntimeError(f"v5 分句配置不可用（{profile_status}），请重新安装最新版 SubFix")
        active_mode_profile = generate_v5.require_profile_mode(profile, subtitle_mode)
    if v5_mode:
        diagnostic["v5_writeback_mode"] = v5_writeback
    output_batches: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="subfix_generate_v4_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        work_items: list[dict[str, Any]] = []
        for batch_index, batch in enumerate(batches, start=1):
            audio_path = Path(str(batch.get("audio") or ""))
            if not audio_path.is_file():
                raise RuntimeError(f"audio file not found: {audio_path}")
            batch_fps = float(batch.get("fps") or args.fps or 30.0)
            cut_path = tmp_path / f"item_{batch_index:04d}.wav"
            write_progress(
                progress_path,
                "prepare_subtitle_batch",
                f"{engine_label} 准备连续音轨 {batch_index}/{len(batches)}",
                batch_index=batch_index,
                total_batches=len(batches),
                progress_index=v4_progress_point(0, 25, batch_index - 1, len(batches)),
                progress_total=100,
            )
            cut_audio(
                ffmpeg_path,
                audio_path,
                cut_path,
                float(batch.get("source_start") or 0.0),
                float(batch["source_end"]) if batch.get("source_end") is not None else None,
                int(batch["audio_channel_index"]) if batch.get("audio_channel_index") else None,
            )
            work_items.append(
                {
                    "cut_path": cut_path,
                    "timeline_start_frame": int(batch.get("timeline_start_frame") or 0),
                    "timeline_end_frame": int(batch["timeline_end_frame"]) if batch.get("timeline_end_frame") is not None else None,
                    "fps": batch_fps,
                    "track_index": int(batch.get("track_index") or 0),
                    "batch_id": str(batch.get("batch_id") or batch_index),
                    # Lua 仲裁按 track_order/track_index/item_index 三元组归并，
                    # 缺省会导致 v4 合轨后的字幕行无法对应回 Resolve 条目。
                    "track_order": int(batch.get("track_order") or 0),
                    "item_index": int(batch.get("item_index") or 0),
                }
            )

        prepared_tracks, prepare_diagnostic = generate_v4.prepare_track_context_windows(
            work_items,
            tmp_path / "tracks",
        )
        diagnostic.update(prepare_diagnostic)
        windows = [
            {
                **window,
                "track_index": int(track["track_index"]),
                "fps": float(track["fps"]),
                # 行级 batch_id 与最终 output_batches 的轨道批次 id 保持一致，
                # Lua merge 时同一行/批次能直接对上。
                "batch_id": f"{engine_label}_track_{int(track['track_index'])}",
                "track_order": int(track.get("track_order") or 0),
                "item_index": int(track.get("item_index") or 0),
            }
            for track in prepared_tracks
            for window in track.get("windows") or []
        ]
        if not windows:
            raise RuntimeError(f"{engine_label} 未生成连续音轨窗口")
        write_progress(
            progress_path,
            "generate_subtitle_batch",
            f"{engine_label} {asr_label} 转写 1/{len(windows)}",
            batch_index=0,
            total_batches=len(windows),
            progress_index=25,
            progress_total=100,
        )
        raw_payloads: list[dict[str, Any]] = []
        batch_size = qwen_generate_batch_size()
        for offset in range(0, len(windows), batch_size):
            chunk = windows[offset:offset + batch_size]
            write_progress(
                progress_path,
                "generate_subtitle_batch",
                f"{engine_label} {asr_label} 转写 {offset + 1}/{len(windows)}",
                batch_index=offset,
                total_batches=len(windows),
                progress_index=v4_progress_point(25, 55, offset, len(windows)),
                progress_total=100,
            )
            chunk_payloads, chunk_asr_diagnostic = transcribe_v4_window_batch(
                [Path(window["audio_path"]) for window in chunk],
                args,
            )
            raw_payloads.extend(chunk_payloads)
            diagnostic["asr_backend_used"] = chunk_asr_diagnostic.get(
                "asr_backend_used", diagnostic["asr_backend_used"]
            )
            diagnostic["doubao_fallback_count"] += int(
                chunk_asr_diagnostic.get("doubao_fallback_count") or 0
            )
            if chunk_asr_diagnostic.get("hotword_context_supported") is False:
                diagnostic["hotword_context_supported"] = False
            if chunk_asr_diagnostic.get("doubao_fallback_errors"):
                diagnostic.setdefault("doubao_fallback_errors", []).extend(
                    chunk_asr_diagnostic["doubao_fallback_errors"]
                )
            # 汇总豆包每次调用的追踪/计费线索，供账务核对（log_id 可去火山精确定位）。
            for _key in ("doubao_log_ids", "doubao_resource_ids", "doubao_status_codes"):
                if chunk_asr_diagnostic.get(_key):
                    diagnostic.setdefault(_key, []).extend(chunk_asr_diagnostic[_key])
            completed_windows = min(len(windows), offset + len(chunk))
            write_progress(
                progress_path,
                "generate_subtitle_batch",
                f"{engine_label} {asr_label} 转写 {completed_windows}/{len(windows)}",
                batch_index=completed_windows,
                total_batches=len(windows),
                progress_index=v4_progress_point(25, 55, completed_windows, len(windows)),
                progress_total=100,
            )
        if len(raw_payloads) != len(windows):
            raise RuntimeError(f"{engine_label} {asr_label} 窗口数量不匹配: {len(raw_payloads)} != {len(windows)}")
        track_by_index = {int(track["track_index"]): track for track in prepared_tracks}
        recovered_payloads: list[dict[str, Any]] = []
        recovery_diagnostics: list[dict[str, Any]] = []
        for recovery_index, (window, raw_payload) in enumerate(zip(windows, raw_payloads), start=1):
            write_progress(
                progress_path,
                "validate_subtitle_batch",
                f"{engine_label} 转写校验 {recovery_index}/{len(windows)}",
                batch_index=recovery_index,
                total_batches=len(windows),
                progress_index=v4_progress_point(55, 60, recovery_index - 1, len(windows)),
                progress_total=100,
            )
            track = track_by_index[int(window["track_index"])]
            try:
                recovered_payload, recovery_diagnostic = recover_v4_asr_window(
                    window=window,
                    payload=raw_payload,
                    track_audio_path=Path(track["audio_path"]),
                    track_start_frame=int(track["timeline_start_frame"]),
                    retry_dir=tmp_path / "asr_recovery",
                    model=args.model,
                    language=args.language,
                    transcribe_fn=retry_transcribe_fn,
                )
            except generate_v4.V4AlignmentError:
                diagnostic["asr_unrecovered_window_count"] += 1
                raise
            recovery = str(recovery_diagnostic.get("asr_recovery") or "not_needed")
            if recovery == "accepted_doubao_empty_speech":
                diagnostic["asr_empty_speech_window_count"] += 1
                diagnostic["asr_tolerated_empty_speech_window_count"] += 1
            elif recovery not in {"not_needed", "accepted_silence"}:
                diagnostic["asr_empty_speech_window_count"] += 1
                diagnostic["asr_recovered_window_count"] += 1
            diagnostic["asr_empty_speech_subwindow_count"] += int(
                recovery_diagnostic.get("asr_empty_speech_subwindow_count") or 0
            )
            diagnostic["asr_single_retry_count"] += int(
                recovery_diagnostic.get("asr_single_retry_count") or 0
            )
            diagnostic["asr_subwindow_retry_count"] += int(
                recovery_diagnostic.get("asr_subwindow_retry_count") or 0
            )
            recovered_payloads.append(recovered_payload)
            recovery_diagnostics.append(recovery_diagnostic)
        raw_payloads = recovered_payloads
        retry_regions: list[dict[str, Any]] = []
        if v5_mode and v5_writeback == "live":
            retry_regions, retry_detection = generate_v5.detect_ambiguous_regions(
                windows,
                raw_payloads,
                float(args.fps or 30.0),
            )
            diagnostic.update(retry_detection)
            for retry_index, region in enumerate(retry_regions, start=1):
                region["retry_region_index"] = retry_index
                track_index = int(region.get("track_index") or 0)
                track = track_by_index.get(track_index)
                if track is None:
                    continue
                write_progress(
                    progress_path,
                    "repair_overlap_batch",
                    f"{engine_label} 正在修复相邻窗口差异 {retry_index}/{len(retry_regions)}",
                    batch_index=retry_index - 1,
                    total_batches=len(retry_regions),
                    progress_index=v4_progress_point(60, 65, retry_index - 1, len(retry_regions)),
                    progress_total=100,
                )
                local_retry_started_at = time.monotonic()
                retry_path = tmp_path / "asr_local_retry" / f"track_{track_index:03d}_retry_{retry_index:04d}.wav"
                generate_v4.write_context_window_audio(
                    Path(track["audio_path"]),
                    retry_path,
                    int(track["timeline_start_frame"]),
                    int(region["start_frame"]),
                    int(region["end_frame"]),
                    float(track["fps"]),
                )
                retry_payload = retry_transcribe_fn(retry_path, args.model, args.language)
                diagnostic["local_retry_elapsed_seconds"] += time.monotonic() - local_retry_started_at
                write_progress(
                    progress_path,
                    "repair_overlap_batch",
                    f"{engine_label} 正在修复相邻窗口差异 {retry_index}/{len(retry_regions)}",
                    batch_index=retry_index,
                    total_batches=len(retry_regions),
                    progress_index=v4_progress_point(60, 65, retry_index, len(retry_regions)),
                    progress_total=100,
                )
                if not generate_v4.normalize_text(retry_payload.get("text")):
                    diagnostic["low_confidence_region_count"] = int(diagnostic.get("low_confidence_region_count") or 0) + 1
                    continue
                retry_window = {
                    "track_index": track_index,
                    "window_index": 100000 + retry_index,
                    "start_frame": int(region["start_frame"]),
                    "end_frame": int(region["end_frame"]),
                    "left_context_frames": 0,
                    "right_context_frames": 0,
                    "fps": float(track["fps"]),
                    "audio_path": str(retry_path),
                    "candidate_kind": "local_retry",
                    "retry_region_index": retry_index,
                    "retry_reason": str(region.get("reason") or "ambiguous_region"),
                    # 重试窗口继承所属轨道的来源元数据，保证重试产出的 unit
                    # 与主窗口一样能回填到断句行。
                    "batch_id": f"{engine_label}_track_{track_index}",
                    "track_order": int(track.get("track_order") or 0),
                    "item_index": int(track.get("item_index") or 0),
                }
                windows.append(retry_window)
                raw_payloads.append(retry_payload)
                try:
                    retry_duration: float | None = audio_duration_seconds(retry_path)
                except RuntimeError:
                    # 诊断字段绝不能反噬主流程：读取失败时记 None 而非伪装成 0 秒。
                    retry_duration = None
                recovery_diagnostics.append(
                    {
                        "asr_recovery": "local_retry",
                        "speech_seconds": retry_duration,
                    }
                )
        if hotword_context:
            diagnostic["hotword_context_supported"] = not any(
                str((payload.get("diagnostic") or {}).get("hotword_context_status") or "")
                == "unsupported"
                for payload in raw_payloads
            )
        diagnostic["stages"] = {
            "window_asr": [
                {
                    "track_index": int(window["track_index"]),
                    "window_index": int(window.get("window_index") or 0),
                    "start_frame": int(window["start_frame"]),
                    "end_frame": int(window["end_frame"]),
                    "text": str(raw_payload.get("text") or ""),
                    "asr_recovery": str(recovery_diagnostic.get("asr_recovery") or "not_needed"),
                    "speech_seconds": float(recovery_diagnostic.get("speech_seconds") or 0.0),
                    "asr_empty_speech_subwindow_count": int(
                        recovery_diagnostic.get("asr_empty_speech_subwindow_count") or 0
                    ),
                    "candidate_kind": str(window.get("candidate_kind") or "primary"),
                    "retry_reason": str(window.get("retry_reason") or ""),
                }
                for window, raw_payload, recovery_diagnostic in zip(
                    windows,
                    raw_payloads,
                    recovery_diagnostics,
                )
            ],
            "aligned_units": [],
            "source_selection": [],
            "segmentation": [],
        }

        units_by_track: dict[int, list[dict[str, Any]]] = {}
        failed_alignment_windows: list[dict[str, Any]] = []
        successful_alignment_windows: list[dict[str, Any]] = []
        payload_by_window = {
            (int(window["track_index"]), int(window.get("window_index") or 0)): payload
            for window, payload in zip(windows, raw_payloads)
        }
        align_language = qwen3_language_name(args.language) or "Chinese"
        backend = "qwen3_asr"
        model = args.model
        alignment_retry_regions: list[dict[str, Any]] = []
        for align_index, (window, raw_payload) in enumerate(zip(windows, raw_payloads), start=1):
            has_doubao_words = (
                str(raw_payload.get("backend") or "") in DOUBAO_ASR_BACKENDS
                and any(bool(segment.get("words")) for segment in raw_payload.get("segments") or [] if isinstance(segment, dict))
            )
            write_progress(
                progress_path,
                "align_subtitle_batch",
                f"{engine_label} {'豆包时间轴整理' if uses_doubao_backend else 'Forced Alignment'} {align_index}/{len(windows)}",
                batch_index=align_index,
                total_batches=len(windows),
                progress_index=v4_progress_point(65, 85, align_index - 1, len(windows)),
                progress_total=100,
            )
            track_index = int(window["track_index"])
            track = track_by_index[track_index]
            track_windows = [
                candidate
                for candidate in windows
                if int(candidate.get("track_index") or 0) == track_index
            ]
            local_index = next(
                (index for index, candidate in enumerate(track_windows) if int(candidate.get("window_index") or 0) == int(window.get("window_index") or 0)),
                -1,
            )
            track_payloads = {
                int(candidate.get("window_index") or 0): payload_by_window.get(
                    (track_index, int(candidate.get("window_index") or 0)),
                    {},
                )
                for candidate in track_windows
            }
            adjacent_window = None if window.get("candidate_kind") == "local_retry" else generate_v4.choose_alignment_context_window(
                track_windows,
                local_index,
                track_payloads,
            )
            adjacent_payload = (
                payload_by_window.get((track_index, int(adjacent_window.get("window_index") or 0)))
                if adjacent_window is not None
                else None
            )
            merged_audio_path = tmp_path / (
                f"align_context_track_{track_index:03d}_window_{int(window.get('window_index') or 0):04d}.wav"
            )
            try:
                aligned_units, align_diagnostic = generate_v4.require_aligned_window_with_context(
                    track_audio_path=Path(track["audio_path"]),
                    track_start_frame=int(track["timeline_start_frame"]),
                    window=window,
                    payload=raw_payload,
                    adjacent_window=adjacent_window,
                    adjacent_payload=adjacent_payload,
                    merged_audio_path=merged_audio_path,
                    align_fn=retry_align_fn,
                    language=align_language,
                    fps=float(window["fps"]),
                )
            except generate_v4.V4AlignmentError as exc:
                diagnostic["unaligned_rejected_count"] += 1
                if v5_mode and not generate_v5.alignment_failure_is_fatal(window):
                    diagnostic["local_retry_alignment_failed_count"] = int(
                        diagnostic.get("local_retry_alignment_failed_count") or 0
                    ) + 1
                    diagnostic["low_confidence_region_count"] = int(
                        diagnostic.get("low_confidence_region_count") or 0
                    ) + 1
                    continue
                failed_alignment_windows.append(
                    {
                        "track_index": track_index,
                        "start_frame": int(window["start_frame"]),
                        "end_frame": int(window["end_frame"]),
                        "error": str(exc),
                    }
                )
                continue
            diagnostic["forced_align_retry_count"] += int(align_diagnostic.get("forced_align_retry_count") or 0)
            native_timestamp_fallback_count = int(align_diagnostic.get("native_timestamp_fallback_count") or 0)
            diagnostic["doubao_native_timestamp_fallback_count"] += native_timestamp_fallback_count
            if has_doubao_words and native_timestamp_fallback_count == 0:
                diagnostic["doubao_native_timestamp_window_count"] += 1
            diagnostic["alignment_repaired_unit_count"] += int(
                align_diagnostic.get("alignment_repaired_unit_count") or 0
            )
            diagnostic["minimum_raw_alignment_coverage"] = min(
                float(diagnostic.get("minimum_raw_alignment_coverage") or 1.0),
                float(align_diagnostic.get("raw_alignment_coverage") or 1.0),
            )
            context_retry = int(align_diagnostic.get("context_align_retry_count") or 0)
            diagnostic["context_align_retry_count"] += context_retry
            score_audio_path = merged_audio_path if context_retry else Path(window["audio_path"])
            score_start_frame = (
                min(int(window["start_frame"]), int(adjacent_window["start_frame"]))
                if context_retry and adjacent_window is not None
                else int(window["start_frame"])
            )
            aligned_units = generate_v4.score_aligned_units_from_audio(
                score_audio_path,
                aligned_units,
                score_start_frame,
                float(window["fps"]),
                float(align_diagnostic.get("alignment_coverage") or 0.0),
            )
            speech_regions, _speech_onsets, _vad_diagnostic = detect_speech_regions(score_audio_path)
            aligned_units = generate_v4.annotate_independent_vad(
                aligned_units,
                speech_regions,
                score_start_frame,
                float(window["fps"]),
            )
            raw_coverage = float(align_diagnostic.get("raw_alignment_coverage") or 0.0)
            repaired_count = int(align_diagnostic.get("alignment_repaired_unit_count") or 0)
            for unit in aligned_units:
                unit["track_index"] = track_index
                unit["window_index"] = int(window.get("window_index") or 0)
                unit["candidate_kind"] = str(window.get("candidate_kind") or "primary")
                unit["retry_region_index"] = int(window.get("retry_region_index") or 0)
                unit["retry_reason"] = str(window.get("retry_reason") or "")
                unit["raw_alignment_coverage"] = raw_coverage
                unit["alignment_repaired_unit_count"] = repaired_count
                unit["unit_count"] = len(aligned_units)
                # 来源批次/轨道/条目随 unit 透传，供断句行回填 Lua 仲裁字段。
                unit["batch_id"] = str(window.get("batch_id") or "")
                unit["track_order"] = int(window.get("track_order") or 0)
                unit["item_index"] = int(window.get("item_index") or 0)
            if (
                v5_mode
                and window.get("candidate_kind") != "local_retry"
                and generate_v5.alignment_needs_retry(
                    {
                        "raw_alignment_coverage": raw_coverage,
                        "alignment_repaired_unit_count": repaired_count,
                        "unit_count": len(aligned_units),
                    }
                )
            ):
                repaired_units = [unit for unit in aligned_units if unit.get("alignment_repaired")]
                if repaired_units:
                    retry_start = min(int(unit.get("start_frame") or window["start_frame"]) for unit in repaired_units)
                    retry_end = max(int(unit.get("end_frame") or retry_start + 1) for unit in repaired_units)
                else:
                    center = (int(window["start_frame"]) + int(window["end_frame"])) // 2
                    retry_start = center - int(round(8.0 * float(window["fps"])))
                    retry_end = center + int(round(8.0 * float(window["fps"])))
                padding = int(round(2.5 * float(window["fps"])))
                retry_start = max(int(window["start_frame"]), retry_start - padding)
                retry_end = min(int(window["end_frame"]), retry_end + padding)
                maximum = int(round(16.0 * float(window["fps"])))
                if retry_end - retry_start > maximum:
                    center = (retry_start + retry_end) // 2
                    retry_start = max(int(window["start_frame"]), center - maximum // 2)
                    retry_end = retry_start + maximum
                alignment_retry_regions.append(
                    {
                        "track_index": track_index,
                        "start_frame": retry_start,
                        "end_frame": retry_end,
                        "reason": "alignment_quality",
                    }
                )
            diagnostic["hotword_replacement_count"] += apply_hotword_replacements_to_units(
                aligned_units, hotword_entries
            )
            units_by_track.setdefault(track_index, []).extend(aligned_units)
            successful_alignment_windows.append(
                {
                    "track_index": track_index,
                    "start_frame": int(window["start_frame"]),
                    "end_frame": int(window["end_frame"]),
                }
            )
            backend = str(raw_payload.get("backend") or backend)
            model = str(raw_payload.get("model") or model)

        if v5_mode and v5_writeback == "live" and alignment_retry_regions:
            merged_alignment_regions: list[dict[str, Any]] = []
            for region in sorted(
                alignment_retry_regions,
                key=lambda item: (int(item["track_index"]), int(item["start_frame"])),
            ):
                overlaps_existing = any(
                    int(existing.get("track_index") or 0) == int(region["track_index"])
                    and min(int(existing["end_frame"]), int(region["end_frame"]))
                    > max(int(existing["start_frame"]), int(region["start_frame"]))
                    for existing in retry_regions
                )
                if overlaps_existing:
                    continue
                if (
                    merged_alignment_regions
                    and int(merged_alignment_regions[-1]["track_index"]) == int(region["track_index"])
                    and int(region["start_frame"]) <= int(merged_alignment_regions[-1]["end_frame"])
                ):
                    merged_alignment_regions[-1]["end_frame"] = max(
                        int(merged_alignment_regions[-1]["end_frame"]),
                        int(region["end_frame"]),
                    )
                else:
                    merged_alignment_regions.append(dict(region))

            retry_offset = len(retry_regions)
            for local_index, region in enumerate(merged_alignment_regions, start=1):
                retry_index = retry_offset + local_index
                track_index = int(region["track_index"])
                track = track_by_index[track_index]
                retry_path = tmp_path / "alignment_local_retry" / f"track_{track_index:03d}_retry_{retry_index:04d}.wav"
                generate_v4.write_context_window_audio(
                    Path(track["audio_path"]),
                    retry_path,
                    int(track["timeline_start_frame"]),
                    int(region["start_frame"]),
                    int(region["end_frame"]),
                    float(track["fps"]),
                )
                try:
                    retry_payload = retry_transcribe_fn(retry_path, args.model, args.language)
                    retry_units, retry_align_diagnostic = generate_v4.require_aligned_units(
                        retry_path,
                        retry_payload,
                        retry_align_fn,
                        align_language,
                        float(track["fps"]),
                        int(region["start_frame"]),
                    )
                except (RuntimeError, generate_v4.V4AlignmentError):
                    diagnostic["low_confidence_region_count"] = int(diagnostic.get("low_confidence_region_count") or 0) + 1
                    continue
                retry_units = generate_v4.score_aligned_units_from_audio(
                    retry_path,
                    retry_units,
                    int(region["start_frame"]),
                    float(track["fps"]),
                    float(retry_align_diagnostic.get("alignment_coverage") or 0.0),
                )
                speech_regions, _speech_onsets, _vad_diagnostic = detect_speech_regions(retry_path)
                retry_units = generate_v4.annotate_independent_vad(
                    retry_units,
                    speech_regions,
                    int(region["start_frame"]),
                    float(track["fps"]),
                )
                for unit in retry_units:
                    unit["track_index"] = track_index
                    unit["window_index"] = 200000 + retry_index
                    unit["candidate_kind"] = "local_retry"
                    unit["retry_region_index"] = retry_index
                    unit["retry_reason"] = "alignment_quality"
                    unit["raw_alignment_coverage"] = float(retry_align_diagnostic.get("raw_alignment_coverage") or 0.0)
                    unit["alignment_repaired_unit_count"] = int(retry_align_diagnostic.get("alignment_repaired_unit_count") or 0)
                    unit["unit_count"] = len(retry_units)
                    unit["batch_id"] = f"{engine_label}_track_{track_index}"
                    unit["track_order"] = int(track.get("track_order") or 0)
                    unit["item_index"] = int(track.get("item_index") or 0)
                diagnostic["hotword_replacement_count"] += apply_hotword_replacements_to_units(
                    retry_units, hotword_entries
                )
                units_by_track.setdefault(track_index, []).extend(retry_units)
                retry_regions.append(
                    {
                        **region,
                        "retry_region_index": retry_index,
                        "reason": "alignment_quality",
                    }
                )
            diagnostic["local_retry_region_count"] = len(retry_regions)

        uncovered_alignment_windows = generate_v4.alignment_failures_without_alternative(
            failed_alignment_windows,
            successful_alignment_windows,
        )
        diagnostic["alignment_window_alternative_count"] = (
            len(failed_alignment_windows) - len(uncovered_alignment_windows)
        )
        diagnostic["alignment_window_uncovered_count"] = len(uncovered_alignment_windows)
        if uncovered_alignment_windows:
            first_failure = uncovered_alignment_windows[0]
            raise generate_v4.V4AlignmentError(
                f"{engine_label} Forced Alignment 无替代麦覆盖: "
                f"轨道={first_failure['track_index']}, "
                f"范围={first_failure['start_frame']}-{first_failure['end_frame']}; "
                f"{first_failure.get('error') or '未知错误'}"
            )
        if not units_by_track:
            raise generate_v4.V4AlignmentError(f"{engine_label} 没有可用的 Forced Alignment 结果")

        write_progress(
            progress_path,
            "select_subtitle_sources",
            f"{engine_label} 正在进行{'自适应' if v5_mode else '文本'}候选仲裁",
            progress_index=85,
            progress_total=100,
        )

        candidates: list[dict[str, Any]] = []
        for track_index in sorted(units_by_track):
            track_windows = list((track_by_index.get(track_index) or {}).get("windows") or [])
            primary_units = [
                unit
                for unit in units_by_track[track_index]
                if str(unit.get("candidate_kind") or "primary") != "local_retry"
            ]
            stitched, seam_diagnostic = generate_v4.stitch_track_window_units(
                primary_units,
                track_windows,
            )
            diagnostic["window_seam_suppressed_count"] += int(
                seam_diagnostic.get("window_seam_suppressed_count") or 0
            )
            deduped, overlap_diagnostic = generate_v4.dedupe_overlap_units(stitched)
            diagnostic["overlap_unit_suppressed_count"] += int(
                overlap_diagnostic.get("overlap_unit_suppressed_count") or 0
            )
            if v5_mode:
                for region in (
                    item
                    for item in retry_regions
                    if int(item.get("track_index") or 0) == track_index
                ):
                    retry_region_index = int(region.get("retry_region_index") or 0)
                    retry_units = [
                        unit
                        for unit in units_by_track[track_index]
                        if str(unit.get("candidate_kind") or "") == "local_retry"
                        and int(unit.get("retry_region_index") or 0) == retry_region_index
                    ]
                    if not retry_units:
                        continue
                    if v5_writeback == "live":
                        deduped, selection_diagnostic = generate_v5.select_retry_region_units(
                            deduped,
                            retry_units,
                            region,
                        )
                        selection_diagnostic["local_retry_writeback_mode"] = "live"
                        selection_diagnostic.setdefault(
                            "local_retry_proposed",
                            bool(selection_diagnostic.get("local_retry_selected")),
                        )
                    else:
                        deduped, selection_diagnostic = generate_v5.arbitrate_retry_region_units(
                            deduped,
                            retry_units,
                            region,
                        )
                    diagnostic["local_retry_writeback_mode"] = str(
                        selection_diagnostic.get("local_retry_writeback_mode") or "shadow"
                    )
                    if selection_diagnostic.get("local_retry_proposed"):
                        diagnostic["local_retry_proposed_count"] = int(
                            diagnostic.get("local_retry_proposed_count") or 0
                        ) + 1
                    if selection_diagnostic.get("local_retry_selected"):
                        diagnostic["local_retry_selected_count"] = int(
                            diagnostic.get("local_retry_selected_count") or 0
                        ) + 1
                    if selection_diagnostic.get("low_confidence_region"):
                        diagnostic["low_confidence_region_count"] = int(
                            diagnostic.get("low_confidence_region_count") or 0
                        ) + 1
            candidates.extend(deduped)
        diagnostic["stages"]["aligned_units"] = [
            {
                "track_index": int(unit.get("track_index") or 0),
                "window_index": int(unit.get("window_index") or 0),
                "start_frame": int(unit.get("start_frame") or 0),
                "end_frame": int(unit.get("end_frame") or 0),
                "text": str(unit.get("text") or ""),
                "source_score": float(unit.get("source_score") or 0.0),
                "speaker_score_db": float(unit.get("speaker_score_db") or 0.0),
                "asr_punctuation_strength": float(unit.get("asr_punctuation_strength") or 0.0),
                "independent_vad": bool(unit.get("independent_vad")),
                "alignment_repaired": bool(unit.get("alignment_repaired")),
                "candidate_kind": str(unit.get("candidate_kind") or "primary"),
                "retry_region_index": int(unit.get("retry_region_index") or 0),
                "raw_alignment_coverage": float(unit.get("raw_alignment_coverage") or 0.0),
            }
            for unit in candidates
        ]
        if v5_mode:
            canonical_units, adaptive_diagnostic = select_adaptive_subtitle_candidates(
                candidates,
                float(args.fps or 30.0),
            )
            for key, value in adaptive_diagnostic.items():
                if key in diagnostic and isinstance(value, (int, float)):
                    diagnostic[key] += value
                else:
                    diagnostic[key] = value
        else:
            echo_filtered_candidates, echo_diagnostic = generate_v4.suppress_cross_mic_echo_regions(
                candidates,
                float(args.fps or 30.0),
            )
            diagnostic.update(echo_diagnostic)
            canonical_units, exclusive_diagnostic = generate_v4.build_exclusive_unit_stream(
                echo_filtered_candidates,
                float(args.fps or 30.0),
            )
            diagnostic.update(exclusive_diagnostic)
            canonical_units, near_duplicate_diagnostic = generate_v4.suppress_near_duplicate_units(
                canonical_units,
                float(args.fps or 30.0),
            )
            diagnostic.update(near_duplicate_diagnostic)
        canonical_units = generate_v4.propagate_original_word_boundaries(candidates, canonical_units)
        write_progress(
            progress_path,
            "segment_subtitles",
            f"{engine_label} 正在进行字幕断句",
            progress_index=90,
            progress_total=100,
        )
        if not v5_mode:
            profile = load_segmentation_profile_v4(getattr(args, "segmentation_profile", None))
        diagnostic["stages"]["source_selection"] = [
            {
                "track_index": int(unit.get("track_index") or 0),
                "start_frame": int(unit.get("start_frame") or 0),
                "end_frame": int(unit.get("end_frame") or 0),
                "text": str(unit.get("text") or ""),
                "source_score": float(unit.get("source_score") or 0.0),
                "speaker_score_db": float(unit.get("speaker_score_db") or 0.0),
                "speaker_decision": str(unit.get("speaker_decision") or "viterbi_exclusive"),
                "asr_punctuation_strength": float(unit.get("asr_punctuation_strength") or 0.0),
                "independent_vad": bool(unit.get("independent_vad")),
            }
            for unit in canonical_units
        ]
        subtitle_rows, segmentation_diagnostic = generate_v4.segment_canonical_units(
            canonical_units,
            subtitle_mode,
            profile,
            float(args.fps or 30.0),
            max_chars=getattr(args, "max_chars", None),
        )
        subtitle_rows, overlong_tail_reclaimed_count = generate_v4.reclaim_overlong_unit_tails(
            subtitle_rows,
            canonical_units,
            float(args.fps or 30.0),
        )
        diagnostic["overlong_tail_reclaimed_count"] = overlong_tail_reclaimed_count
        subtitle_rows, word_boundary_protected_count = generate_v4.protect_word_boundaries(
            subtitle_rows,
            canonical_units,
            float(args.fps or 30.0),
        )
        diagnostic["word_boundary_protected_count"] = word_boundary_protected_count
        subtitle_rows, hard_char_split_count = generate_v4.enforce_hard_char_limit(
            subtitle_rows,
            canonical_units,
            getattr(args, "max_chars", None),
        )
        diagnostic["hard_char_split_count"] = hard_char_split_count
        subtitle_rows = generate_v4.restore_display_spacing(subtitle_rows, canonical_units)
        subtitle_rows, textnorm_diagnostic = generate_textnorm.normalize_subtitle_rows(subtitle_rows)
        diagnostic["textnorm_changed_row_count"] = textnorm_diagnostic["textnorm_changed_row_count"]
        if v5_mode and v5_writeback == "live" and subtitle_rows:
            refined_rows = []
            refinement_diagnostic = {
                "audio_refinement_mode": v5_writeback,
                "audio_refined_row_count": 0,
                "energy_valley_boundary_count": 0,
                "confirmed_silence_preserved_count": 0,
            }
            for track in prepared_tracks:
                track_index = int(track["track_index"])
                track_rows = [
                    row
                    for row in subtitle_rows
                    if int(row.get("speaker_track_index") or 0) == track_index
                ]
                if not track_rows:
                    continue
                refine_boundaries = (
                    generate_v5.refine_subtitle_boundaries
                    if v5_writeback == "live"
                    else generate_v5.shadow_refine_subtitle_boundaries
                )
                refined_track_rows, track_refinement = refine_boundaries(
                    track_rows,
                    Path(track["audio_path"]),
                    int(track["timeline_start_frame"]),
                    float(track["fps"]),
                )
                refined_rows.extend(refined_track_rows)
                for key, value in track_refinement.items():
                    if isinstance(value, (int, float)):
                        refinement_diagnostic[key] = int(refinement_diagnostic.get(key) or 0) + int(value)
                    else:
                        refinement_diagnostic[key] = value
            subtitle_rows, refinement_conflicts = generate_v5.preserve_refined_row_order(refined_rows)
            refinement_diagnostic["audio_refinement_overlap_prevented_count"] = refinement_conflicts
            refinement_diagnostic["audio_refined_row_count"] = sum(
                row["start_frame"] != row["original_start_frame"] or row["end_frame"] != row["original_end_frame"]
                for row in subtitle_rows
            )
            diagnostic.update(refinement_diagnostic)
        # Display continuity is the final timing step. In v5, keep the original
        # long pauses protected above instead of filling them from duration alone.
        tail_extension_gap_seconds = generate_v4.SUBTITLE_ROW_TAIL_EXTENSION_GAP_SECONDS
        if v5_mode:
            tail_extension_gap_seconds = min(tail_extension_gap_seconds, 0.20)
        tail_extension_max_gap_frames = int(round(tail_extension_gap_seconds * float(args.fps or 30.0)))
        subtitle_rows, tail_extended_row_count = generate_v4.extend_subtitle_row_tails(
            subtitle_rows,
            float(args.fps or 30.0),
            tail_extension_max_gap_frames,
        )
        diagnostic["tail_extended_row_count"] = tail_extended_row_count
        diagnostic["hotword_replacement_count"] += apply_hotword_replacements_to_units(
            subtitle_rows, hotword_entries
        )
        for index, row in enumerate(subtitle_rows, start=1):
            row["index"] = index
        diagnostic.update(segmentation_diagnostic)
        mode_profile = active_mode_profile if v5_mode else (((profile or {}).get("modes") or {}).get(subtitle_mode) if profile else None)
        boundary_model = (mode_profile or {}).get("boundary_model") if isinstance(mode_profile, dict) else None
        canonical_text = "".join(generate_v4.normalize_text(unit.get("text")) for unit in canonical_units)
        text_cursor = 0
        segmentation_stage: list[dict[str, Any]] = []
        for row in subtitle_rows:
            text_cursor += len(generate_v4.normalize_text(row.get("text")))
            probability = (
                generate_v4.boundary_probability(canonical_text, text_cursor, boundary_model)
                if boundary_model and 0 < text_cursor < len(canonical_text)
                else 1.0
            )
            segmentation_stage.append(
                {
                    "start_frame": int(row.get("start_frame") or 0),
                    "end_frame": int(row.get("end_frame") or 0),
                    "text": str(row.get("text") or ""),
                    "track_index": int(row.get("speaker_track_index") or 0),
                    "boundary_probability": round(float(probability), 6),
                }
            )
        diagnostic["stages"]["segmentation"] = segmentation_stage
        diagnostic["canonical_unit_count"] = len(canonical_units)
        diagnostic["generated_subtitle_count"] = len(subtitle_rows)
        diagnostic["forced_aligned_unit_coverage"] = 1.0 if canonical_units else 0.0
        diagnostic["segmentation_profile_schema"] = str((profile or {}).get("schema_version") or "")
        for track in prepared_tracks:
            track_index = int(track["track_index"])
            output_batches.append(
                {
                    "ok": True,
                    "batch_id": f"{engine_label}_track_{track_index}",
                    "track_index": track_index,
                    "timeline_start_frame": int(track["timeline_start_frame"]),
                    "fps": float(track["fps"]),
                    "backend": backend,
                    "model": model,
                    "segments": [],
                    "subtitle_rows": [
                        row for row in subtitle_rows if int(row.get("speaker_track_index") or 0) == track_index
                    ],
                    "diagnostic": {
                        "item_count": int(track["item_count"]),
                        "window_count": len(track.get("windows") or []),
                    },
                }
            )
    write_progress(
        progress_path,
        "write_output",
        f"{engine_label} 连续词时间线生成完成",
        batch_index=len(batches),
        total_batches=len(batches),
        progress_index=95,
        progress_total=100,
    )
    return {
        "ok": True,
        "backend": backend,
        "model": model,
        "segments": [],
        "subtitle_rows": subtitle_rows,
        "batches": output_batches,
        "text": "".join(str(row.get("text") or "") for row in subtitle_rows),
        "diagnostic": diagnostic,
    }


def run_generate_subtitles_batch_plan_v5(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
    fixture_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run v5 through the shared continuous-timeline pipeline with v5 gates enabled."""
    args.generate_engine = "v5"
    return run_generate_subtitles_batch_plan_v4(
        batches,
        args,
        progress_path,
        fixture_payload=fixture_payload,
    )


def run_generate_subtitles_batch_plan(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
    fixture_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    engine = str(getattr(args, "generate_engine", "v5") or "v5").lower()
    if engine != "v5":
        raise RuntimeError("字幕生成已统一使用 v5，请移除旧版引擎参数")
    return run_generate_subtitles_batch_plan_v5(batches, args, progress_path, fixture_payload=fixture_payload)


def qwen3_support_roots() -> list[Path]:
    module_dir = Path(__file__).resolve().parent
    roots = [
        module_dir / ".subfix_support",
        module_dir,
        Path.cwd() / ".subfix_support",
    ]
    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        root_key = str(root)
        if root_key not in seen:
            seen.add(root_key)
            unique_roots.append(root)
    return unique_roots


def discover_qwen3_cpp_bin() -> Path | None:
    for root in qwen3_support_roots():
        for candidate in (
            root / "qwen3-asr.cpp" / "build" / "qwen3-asr-cli",
            root / "bin" / "qwen3-asr-cli",
        ):
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate
    resolved_bin = shutil.which("qwen3-asr-cli")
    return Path(resolved_bin) if resolved_bin else None


def discover_qwen3_aligner_model() -> Path | None:
    for root in qwen3_support_roots():
        model_dir = root / "models"
        for model_name in QWEN3_FORCED_ALIGNER_MODEL_NAMES:
            candidate = model_dir / model_name
            if candidate.exists():
                return candidate
        for candidate in sorted(model_dir.glob("qwen3-forced-aligner-0.6b-*.gguf")):
            if candidate.exists():
                return candidate
    return None


def resolve_qwen3_cpp_paths() -> tuple[Path, Path]:
    bin_value = str(os.getenv(QWEN3_CPP_BIN_ENV) or "").strip()
    if bin_value:
        bin_path = Path(bin_value).expanduser()
        if not bin_path.exists():
            resolved_bin = shutil.which(bin_value)
            if resolved_bin:
                bin_path = Path(resolved_bin)
            else:
                raise RuntimeError(f"{QWEN3_CPP_BIN_ENV} 指向的 qwen3-asr-cli 不存在: {bin_value}")
    else:
        discovered_bin = discover_qwen3_cpp_bin()
        if not discovered_bin:
            raise RuntimeError(f"{QWEN3_CPP_BIN_ENV} 未配置，且未在 .subfix_support 中找到 qwen3-asr-cli")
        bin_path = discovered_bin
    if not os.access(bin_path, os.X_OK):
        raise RuntimeError(f"{QWEN3_CPP_BIN_ENV} 指向的 qwen3-asr-cli 不可执行: {bin_path}")

    model_value = str(os.getenv(QWEN3_CPP_ALIGNER_GGUF_ENV) or "").strip()
    if model_value:
        model_path = Path(model_value).expanduser()
        if not model_path.exists():
            raise RuntimeError(f"{QWEN3_CPP_ALIGNER_GGUF_ENV} 指向的模型不存在: {model_value}")
    else:
        discovered_model = discover_qwen3_aligner_model()
        if not discovered_model:
            raise RuntimeError(f"{QWEN3_CPP_ALIGNER_GGUF_ENV} 未配置，且未在 .subfix_support/models 中找到 Qwen3 forced aligner GGUF")
        model_path = discovered_model
    return bin_path, model_path


def qwen3_cpp_forced_align_text_rows(
    audio_path: Path,
    rows: list[dict[str, Any]],
    language: str | None,
    fps: float,
    timeline_start_frame: int,
    work_dir: Path,
) -> dict[str, Any]:
    alignment_text = build_alignment_text(rows)
    if not alignment_text:
        raise RuntimeError("Qwen3 forced align 文本为空")

    bin_path, model_path = resolve_qwen3_cpp_paths()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / f"qwen_forced_align_{int(time.time() * 1000)}_{os.getpid()}.json"
    cmd = [
        str(bin_path),
        "-m",
        str(model_path),
        "-f",
        str(audio_path),
        "--align",
        "--text",
        alignment_text,
        "--language",
        qwen3_language_name(language) or "Chinese",
        "-o",
        str(output_path),
    ]
    result = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"Qwen3 forced align 执行失败: {detail or result.returncode}")
    if not output_path.exists():
        raise RuntimeError(f"Qwen3 forced align 未生成输出 JSON: {output_path}")

    # 输出 JSON 仅用于进程间回传：解析（无论成功或失败）后立即删除，
    # 避免在 subfix_qwen_align_outputs 里持续堆积含识别文本的临时文件。
    try:
        try:
            qwen_payload = json.loads(output_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"Qwen3 forced align 输出 JSON 非法: {exc}") from exc
    finally:
        output_path.unlink(missing_ok=True)

    timestamp_items = qwen3_timestamp_words(qwen_payload)
    if not timestamp_items:
        raise RuntimeError("Qwen3 forced align 输出没有词/字时间戳")
    latest_timestamp = max(
        max(float(item.get("start") or 0.0), float(item.get("end") or 0.0))
        for item in timestamp_items
    )
    if latest_timestamp <= 0.001:
        raise RuntimeError(
            "Qwen3 forced align 时间戳无效：所有词/字均停留在 0 秒"
        )
    row_segments = qwen3_remap_timestamp_items_to_rows(
        rows,
        timestamp_items,
        fps=fps,
        timeline_start_frame=timeline_start_frame,
    )
    aligned_rows = apply_alignment_to_rows(
        rows,
        row_segments,
        fps,
        timeline_start_frame,
        allow_non_monotonic=True,
    )
    if len(aligned_rows) != len(row_segments):
        raise ValueError("对齐结果行数不一致: aligned=%d segments=%d" % (len(aligned_rows), len(row_segments)))
    for aligned_row, segment in zip(aligned_rows, row_segments):
        aligned_row["stable_ts_start_frame"] = aligned_row["start_frame"]
        aligned_row["ctc_start_frame"] = int(segment["ctc_start_frame"])
        aligned_row["ctc_end_frame"] = int(segment["ctc_end_frame"])
        aligned_row["ctc_confidence"] = segment.get("ctc_confidence")
        aligned_row["ctc_char_count"] = segment.get("ctc_char_count")
        aligned_row["row_remap_score"] = segment.get("row_remap_score")
        aligned_row["row_remap_decision"] = segment.get("row_remap_decision")
        aligned_row["remap_text_candidate"] = segment.get("remap_text_candidate")
        aligned_row["row_remap_target"] = segment.get("row_remap_target")
        aligned_row["alignment_mode"] = "qwen3_forced_aligner"

    return {
        "backend": "qwen3_cpp_forced_aligner",
        "model": str(model_path),
        "segments": row_segments,
        "aligned_rows": aligned_rows,
        "text": alignment_text,
        "diagnostic": {
            "align_engine": "qwen3_cpp",
            "qwen_item_count": len(timestamp_items),
            "qwen_output_path": str(output_path),
            "qwen_cli": str(bin_path),
            "qwen_model": str(model_path),
        },
    }


def clamp_first_row_to_speech_onset(
    aligned_rows: list[dict[str, Any]],
    audio_path: Path,
    fps: float,
    timeline_start_frame: int,
) -> dict[str, Any] | None:
    """把首行字幕起点夹紧到真实语音起点，修开头长静音误对齐。

    Qwen3ForcedAligner 面对选区开头数秒静音时，可能把文稿首行的首字时间戳压到
    音频 0 点（整条字幕从选区第一帧就显示），而人声其实在数秒后才进入。
    用能量 VAD（detect_speech_regions，与 cpp 批量路径同款）找第一个与首行
    时间区间重叠的语音区；若对齐起点比该语音区起点还早出明显一段，就把首行
    起点推到语音起点前保留少量字头提前量。只修首行、且要求大偏移才动手，
    避免误伤“开头本来就有轻声说话”的素材。
    """
    if not aligned_rows:
        return None
    try:
        regions, _onsets, vad_diag = detect_speech_regions(Path(audio_path))
    except Exception:
        return None
    if not regions:
        return None

    first = aligned_rows[0]
    try:
        first_start = float(first.get("start") or 0.0)
        first_end = float(first.get("end") or first_start)
    except (TypeError, ValueError):
        return None

    # 只认与首行时间区间重叠（含 0.35s 邻接容差，覆盖字尾轻声未被 VAD 收进区域）
    # 的第一个语音区，避免把首行推给与其无关的开头噪声。
    matched_region: dict[str, float] | None = None
    for region in regions:
        if float(region["end"]) >= first_start - 0.35 and float(region["start"]) <= first_end + 0.35:
            matched_region = region
            break
    if matched_region is None:
        return None

    onset = float(matched_region["start"])
    gap_seconds = 0.25   # 对齐起点比语音早 250ms 以上才判定为“吃进了静音”
    lead_seconds = 0.08  # 夹紧后仍保留 80ms 字头提前量（显示层还会再提前几帧）
    if first_start + gap_seconds < onset:
        new_start = max(0.0, onset - lead_seconds)
        rate = max(1.0, float(fps or 30.0))
        tl = int(timeline_start_frame or 0)
        new_start_frame = int(round(tl + new_start * rate))
        first["start"] = round(new_start, 3)
        first["start_frame"] = new_start_frame
        # end/end_frame 维持对齐结果（首行结尾在语音区内，本来就对）。
        if "ctc_start_frame" in first:
            first["ctc_start_frame"] = new_start_frame
        first["onset_clamped"] = True
        return {
            "first_row_onset_clamped": True,
            "aligned_first_start": round(first_start, 3),
            "speech_onset": round(onset, 3),
            "clamped_start": round(new_start, 3),
            "vad": vad_diag,
        }
    # 反向：首行起点明显晚于首个语音区起点（文稿首行本该是第一句话，
    # 但对齐器把它排到了第二句话之后），把首行拉回到首个语音起点。
    # 只在偏移足够大（>0.3s）时才动手，避免正常的句间停顿被误判。
    first_onset = float(regions[0]["start"])
    pull_gap_seconds = 0.3
    if first_start > first_onset + pull_gap_seconds:
        new_start = max(0.0, first_onset - lead_seconds)
        rate = max(1.0, float(fps or 30.0))
        tl = int(timeline_start_frame or 0)
        new_start_frame = int(round(tl + new_start * rate))
        first["start"] = round(new_start, 3)
        first["start_frame"] = new_start_frame
        if "ctc_start_frame" in first:
            first["ctc_start_frame"] = new_start_frame
        first["onset_clamped"] = True
        return {
            "first_row_onset_clamped": True,
            "first_row_pulled_earlier": True,
            "aligned_first_start": round(first_start, 3),
            "speech_onset": round(first_onset, 3),
            "clamped_start": round(new_start, 3),
            "vad": vad_diag,
        }
    return None


def clamp_last_row_to_speech_end(
    aligned_rows: list[dict[str, Any]],
    audio_path: Path,
    fps: float,
    timeline_start_frame: int,
    skip_redistribute: bool = False,
) -> dict[str, Any] | None:
    """把尾部字幕重排到真实语音终点，修结尾整段字幕提前结束。

    Qwen3ForcedAligner 在音频尾部可能把末几行的时间戳整体压缩（多行挤在
    语音结尾之前的一小段），导致最后数条字幕在人声还没说完时就全部结束。
    仅拉长末行结尾会把末行拉成异常长条，反而更不对。正确做法是：用 VAD 取
    真实语音终点，找出尾部被压缩的行（ASR 时长明显短于按语速估算的时长），
    把这些行按字数比例重新铺开到语音终点。只在末行终点比语音终点早 1s 以上
    时才动手，避免误伤“结尾本来就有静音/音乐”的素材。
    """
    if not aligned_rows:
        return None
    try:
        regions, _onsets, vad_diag = detect_speech_regions(Path(audio_path))
    except Exception:
        return None
    if not regions:
        return None

    last = aligned_rows[-1]
    try:
        last_start = float(last.get("start") or 0.0)
        last_end = float(last.get("end") or last_start)
    except (TypeError, ValueError):
        return None

    # 找与末行区间重叠的最后一个语音区
    matched_region: dict[str, float] | None = None
    for region in reversed(regions):
        if float(region["start"]) <= last_end + 0.35 and float(region["end"]) >= last_start - 0.35:
            matched_region = region
            break
    if matched_region is None:
        return None

    # 用最后一个语音区的终点作为 speech_end，而不是仅用与末行重叠的语音区。
    # 原因：被拒行尾部可能没有覆盖到最后一个语音区（如 ASR 坍缩导致尾部行偏短），
    # 此时只看重叠区会遗漏后面仍有人声的语音区。
    # 但要注意：片尾音乐/环境音常被 VAD 误判为语音区。仅当最后一个语音区与重叠区
    # （真实语音匹配区）基本连续（间隔 <= 阈值）时才外推到它；否则认为那是片尾噪声，
    # speech_end 留在真实语音重叠区终点，避免把尾部字幕窗口拉到静音/音乐区导致整体错位。
    _speech_end_gap_thr = float(os.getenv("SUBFIX_SCRIPT_MATCH_SPEECH_END_MAX_GAP", "1.5"))
    speech_end = float(matched_region["end"])
    last_region_end = float(regions[-1]["end"])
    if last_region_end > speech_end:
        _last_region_gap = float(regions[-1]["start"]) - speech_end
        if _last_region_gap <= _speech_end_gap_thr:
            speech_end = last_region_end
    # DEBUG: log clamp_last_row state
    try:
        with open(os.path.expanduser("~\\subfix_remap_debug.log"), "a", encoding="utf-8") as _dlf:
            _dlf.write(f"[clamp_last] last_start={last_start:.3f} last_end={last_end:.3f} "
                       f"matched_region=({matched_region['start']:.3f},{matched_region['end']:.3f}) "
                       f"speech_end={speech_end:.3f} regions={len(regions)} "
                       f"last_region=({regions[-1]['start']:.3f},{regions[-1]['end']:.3f})\n")
    except Exception:
        pass
    # 末行终点比语音结束早 1s 以上才判定为"尾部压缩"
    gap_seconds = 1.0
    tail_seconds = 0.08
    if last_end + gap_seconds >= speech_end:
        try:
            with open(os.path.expanduser("~\\subfix_remap_debug.log"), "a", encoding="utf-8") as _dlf:
                _dlf.write(f"[clamp_last] NO_TRIGGER: last_end+gap={last_end + gap_seconds:.3f} >= speech_end={speech_end:.3f}\n")
        except Exception:
            pass
        return None

    rate = max(1.0, float(fps or 30.0))
    tl = int(timeline_start_frame or 0)

    # 用所有行估算整体语速（字/秒），用于判断哪些行被压缩
    total_chars = 0
    total_speech = 0.0
    for row in aligned_rows:
        try:
            s = float(row.get("start") or 0.0)
            e = float(row.get("end") or s)
            text = str(row.get("text") or "")
            total_chars += max(1, len(text))
            total_speech += max(0.0, e - s)
        except (TypeError, ValueError):
            continue
    speech_rate = (total_chars / total_speech) if total_speech > 1e-6 else 4.0
    if not (1.0 <= speech_rate <= 20.0):
        speech_rate = 4.0

    # 从末行向前找被压缩的行：ASR 时长 < 50% 按语速估算的时长
    # 找到第一个未被压缩的行就停止，避免误伤对齐正确的行
    compress_count = 0
    for row in reversed(aligned_rows):
        try:
            s = float(row.get("start") or 0.0)
            e = float(row.get("end") or s)
            text = str(row.get("text") or "")
            chars = max(1, len(text))
            asr_dur = max(0.0, e - s)
            expected_dur = chars / speech_rate
            if asr_dur < 0.5 * expected_dur:
                compress_count += 1
            else:
                break
        except (TypeError, ValueError):
            break
    # DEBUG: log compress_count and speech_rate
    gap = speech_end - last_end
    try:
        with open(os.path.expanduser("~\\subfix_remap_debug.log"), "a", encoding="utf-8") as _dlf:
            _dlf.write(f"[clamp_last] speech_rate={speech_rate:.3f} compress_count={compress_count} "
                       f"gap={gap:.3f}\n")
    except Exception:
        pass

    # 若尾部与语音终点差距较大（>2s），即使各行时长未被判定为"压缩"，
    # 也需要重分配若干尾部行来覆盖语音区，否则只拉长末行会变成异常长条。
    if compress_count <= 0 and gap > 2.0:
        chars_needed = gap * speech_rate
        accumulated = 0
        for row in reversed(aligned_rows):
            try:
                accumulated += max(1, len(str(row.get("text") or "")))
                compress_count += 1
                if accumulated >= chars_needed:
                    break
            except (TypeError, ValueError):
                break
        compress_count = max(1, min(compress_count, len(aligned_rows)))

    if compress_count <= 0:
        # 末行未被压缩但终点仍早于语音结束，仅把末行终点推到语音终点
        new_end = max(last_end, speech_end - tail_seconds)
        last["end"] = round(new_end, 3)
        last["end_frame"] = int(round(tl + new_end * rate))
        if "ctc_end_frame" in last:
            last["ctc_end_frame"] = last["end_frame"]
        last["end_clamped"] = True
        return {
            "last_row_end_clamped": True,
            "aligned_last_end": round(last_end, 3),
            "speech_end": round(speech_end, 3),
            "clamped_end": round(new_end, 3),
            "vad": vad_diag,
        }

    # 尾部已由大模型切片重对齐：不要再二次机械重排，保留其相对时序。
    if skip_redistribute:
        return None

    # 重分配尾部 compress_count 行：从第一行被压缩行的起点铺到语音终点。
    # 优先用对齐器给出的“相对时长”缩放（保留大模型相对时序），仅当各行时长
    # 全部退化（sum_orig<=0）时回退到纯字数比例。
    tail_start_idx = len(aligned_rows) - compress_count
    first_tail = aligned_rows[tail_start_idx]
    try:
        redist_start = float(first_tail.get("start") or 0.0)
    except (TypeError, ValueError):
        redist_start = last_start
    redist_end = speech_end - tail_seconds
    total_tail_chars = sum(
        max(1, len(str(aligned_rows[i].get("text") or "")))
        for i in range(tail_start_idx, len(aligned_rows))
    )
    window = max(0.1, redist_end - redist_start)
    # 预计算尾部各行在对齐器输出中的原始时长，用于相对时长缩放
    _orig_durs: list[float] = []
    for i in range(tail_start_idx, len(aligned_rows)):
        try:
            ss = float(aligned_rows[i].get("start") or 0.0)
            ee = float(aligned_rows[i].get("end") or ss)
            _orig_durs.append(max(0.0, ee - ss))
        except (TypeError, ValueError):
            _orig_durs.append(0.0)
    _sum_orig = sum(_orig_durs)
    # DEBUG: log redistribution
    try:
        with open(os.path.expanduser("~\\subfix_remap_debug.log"), "a", encoding="utf-8") as _dlf:
            _dlf.write(f"[clamp_last] redist: tail_start_idx={tail_start_idx} "
                       f"redist_start={redist_start:.3f} redist_end={redist_end:.3f} "
                       f"window={window:.3f} total_tail_chars={total_tail_chars} "
                       f"sum_orig={_sum_orig:.3f} use_relative={'yes' if _sum_orig > 1e-6 else 'no'}\n")
    except Exception:
        pass
    cursor = redist_start
    for i in range(tail_start_idx, len(aligned_rows)):
        row = aligned_rows[i]
        text = str(row.get("text") or "")
        chars = max(1, len(text))
        if _sum_orig > 1e-6:
            duration = window * (_orig_durs[i - tail_start_idx] / _sum_orig)
        else:
            duration = window * (chars / total_tail_chars)
        seg_start = cursor
        seg_end = max(cursor + 0.05, cursor + duration)
        row["start"] = round(seg_start, 3)
        row["end"] = round(seg_end, 3)
        row["start_frame"] = int(round(tl + seg_start * rate))
        row["end_frame"] = int(round(tl + seg_end * rate))
        if "ctc_start_frame" in row:
            row["ctc_start_frame"] = row["start_frame"]
        if "ctc_end_frame" in row:
            row["ctc_end_frame"] = row["end_frame"]
        row["end_clamped"] = True
        cursor = seg_end

    return {
        "tail_rows_redistributed": True,
        "redistributed_count": compress_count,
        "speech_end": round(speech_end, 3),
        "original_last_end": round(last_end, 3),
        "vad": vad_diag,
    }


def qwen3_py_forced_align_text_rows(
    audio_path: Path,
    rows: list[dict[str, Any]],
    language: str | None,
    fps: float,
    timeline_start_frame: int,
) -> dict[str, Any]:
    """用 transformers 版 Qwen3ForcedAligner 把文稿行强制对齐到音频。

    与 qwen3_cpp_forced_align_text_rows 的 remap 逻辑完全一致，只是底层对齐引擎换成
    安装器已预下载的 Qwen3-ForcedAligner-0.6B（HF 模型），无需单独构建 cpp 二进制。
    对齐结果直接给出每条文稿行的绝对时间轴（基于 timeline_start_frame）。
    """
    alignment_text = build_alignment_text(rows)
    if not alignment_text:
        raise RuntimeError("Qwen3 forced align 文本为空")
    items = qwen3_force_align_items(Path(audio_path), alignment_text, qwen3_language_name(language) or "Chinese")
    if not items:
        raise RuntimeError("Qwen3 forced align 没有返回时间戳")
    # 预先用 VAD 取真实语音起点/终点，传给 remap 使开头/尾部被拒行能对齐到语音边界；
    # clamp_first/last_row 内部会再次调用 detect_speech_regions，开销可忽略。
    speech_onset: float | None = None
    speech_end: float | None = None
    speech_regions: list[dict[str, float]] | None = None
    try:
        _regions, _onsets, _vad_diag = detect_speech_regions(Path(audio_path))
        if _regions:
            speech_onset = float(_regions[0]["start"])
            speech_end = float(_regions[-1]["end"])
            speech_regions = _regions
    except Exception:
        speech_onset = None
        speech_end = None
        speech_regions = None
    row_segments = qwen3_remap_timestamp_items_to_rows(
        rows, items, fps=fps, timeline_start_frame=timeline_start_frame,
        speech_end=speech_end, speech_onset=speech_onset, speech_regions=speech_regions
    )
    if len(rows) != len(row_segments):
        raise ValueError("Qwen3 forced align 结果行数不一致: rows=%d segments=%d" % (len(rows), len(row_segments)))
    aligned_rows: list[dict[str, Any]] = []
    for row, segment in zip(rows, row_segments):
        segment_start = float(segment.get("start") or 0.0)
        segment_end = float(segment.get("end") or segment_start)
        start_frame = int(round(timeline_start_frame + segment_start * fps))
        end_frame = max(start_frame + 1, int(round(timeline_start_frame + segment_end * fps)))
        aligned_row = {
            "index": int(row.get("index") or (len(aligned_rows) + 1)),
            "text": str(row.get("text") or ""),
            "start": round(segment_start, 3),
            "end": round(segment_end, 3),
            "start_frame": start_frame,
            "end_frame": end_frame,
        }
        for key in (
            "ctc_start_frame",
            "ctc_end_frame",
            "ctc_confidence",
            "ctc_char_count",
            "alignment_mode",
            "row_remap_score",
            "row_remap_decision",
            "remap_text_candidate",
            "row_remap_target",
        ):
            if key in segment:
                aligned_row[key] = segment[key]
        aligned_rows.append(aligned_row)
    # ---- 尾部坍缩切片重对齐（核心修复：让大模型在尾部真正生效）----
    # 对齐器在长音频尾部退化（时间戳坍缩）后，原本会被“按字数比例机械重排”兜底覆盖，
    # 造成后部分字幕不与音频对齐。这里对尾部被拒段单独切片重跑 Qwen3 强制对齐，
    # 用大模型的相对时序替换机械分配；失败则回退到既有兜底逻辑（行为不劣于现状）。
    tail_realign_info = _try_realign_tail_with_aligner(
        Path(audio_path), aligned_rows, language, fps, timeline_start_frame, speech_end
    )
    tail_realigned = tail_realign_info is not None
    if tail_realigned:
        for _idx, _s, _e in tail_realign_info["rows"]:
            _row = aligned_rows[_idx]
            _rate = max(1.0, float(fps or 30.0))
            _tl = int(timeline_start_frame or 0)
            _row["start"] = round(_s, 3)
            _row["end"] = round(_e, 3)
            _row["start_frame"] = int(round(_tl + _s * _rate))
            _row["end_frame"] = max(_row["start_frame"] + 1, int(round(_tl + _e * _rate)))
            if "ctc_start_frame" in _row:
                _row["ctc_start_frame"] = _row["start_frame"]
            if "ctc_end_frame" in _row:
                _row["ctc_end_frame"] = _row["end_frame"]
            _row["row_remap_decision"] = "realigned_tail"
            _row["tail_realigned"] = True
    onset_diag = clamp_first_row_to_speech_onset(aligned_rows, Path(audio_path), fps, timeline_start_frame)
    end_diag = clamp_last_row_to_speech_end(
        aligned_rows, Path(audio_path), fps, timeline_start_frame,
        skip_redistribute=tail_realigned,
    )
    pad_aligned_rows_display(aligned_rows, fps, timeline_start_frame)
    diagnostic: dict[str, Any] = {
        "align_engine": "qwen3_py",
        "row_count": len(rows),
        "aligned_count": len(aligned_rows),
    }
    if tail_realigned:
        diagnostic["tail_realign"] = {
            "used": True,
            "tail_rows": len(tail_realign_info["rows"]),
            "slice_start": round(tail_realign_info["slice_start"], 3),
            "slice_end": round(tail_realign_info["slice_end"], 3),
        }
    if onset_diag:
        diagnostic.update(onset_diag)
    if end_diag:
        diagnostic.update(end_diag)
    return {
        "backend": "qwen3_forced_aligner_py",
        "model": os.getenv("SUBFIX_QWEN3_ALIGNER_MODEL") or _local_forced_aligner_model() or QWEN3_FORCED_ALIGNER_MODEL,
        "aligned_rows": aligned_rows,
        "text": alignment_text,
        "diagnostic": diagnostic,
    }


def _tail_zero_duration_run(units: list[dict[str, Any]], threshold: float = 0.001) -> int:
    """从末尾统计连续零时长（退化）unit 的run长度，用于判断切片重对齐是否仍坍缩。"""
    run = 0
    for _i in range(len(units) - 1, -1, -1):
        _dur = max(0.0, float(units[_i].get("end", 0)) - float(units[_i].get("start", 0)))
        if _dur <= threshold:
            run += 1
        else:
            break
    return run


def _cut_wav_slice(audio_path: Path, start_s: float, end_s: float, pad_seconds: float = 0.0) -> Path | None:
    """用 wave 模块按原始格式切出 [start_s, end_s] 区间的临时 WAV；失败返回 None。

    pad_seconds>0 时在其后追加等量静音，用于把对齐器的尾部坍缩点推到静音区（重对齐时丢弃）。
    """
    try:
        with wave.open(str(audio_path), "rb") as w:
            sr = w.getframerate()
            nch = w.getnchannels()
            sw = w.getsampwidth()
            nframes = w.getnframes()
            if sr <= 0 or nframes <= 0:
                return None
            start_f = max(0, int(round(start_s * sr)))
            end_f = min(nframes, int(round(end_s * sr)))
            if end_f <= start_f:
                return None
            w.setpos(start_f)
            data = w.readframes(end_f - start_f)
        if pad_seconds > 0.0:
            pad_frames = max(1, int(round(pad_seconds * sr)))
            data += bytes(pad_frames * nch * sw)
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".wav", prefix="subfix_tail_realign_")
        os.close(fd)
        with wave.open(p, "wb") as out:
            out.setnchannels(nch)
            out.setsampwidth(sw)
            out.setframerate(sr)
            out.writeframes(data)
        return Path(p)
    except Exception:
        return None


def _log_tail_skip(reason: str) -> None:
    """记录尾部重对齐放弃原因，便于用真实素材诊断（而非静默返回 None）。"""
    try:
        with open(os.path.expanduser("~\\subfix_remap_debug.log"), "a", encoding="utf-8") as _dlf:
            _dlf.write(f"[tail_realign] SKIP: {reason}\n")
    except Exception:
        pass


def _try_realign_tail_with_aligner(
    audio_path: Path,
    aligned_rows: list[dict[str, Any]],
    language: str | None,
    fps: float,
    timeline_start_frame: int,
    speech_end: float | None,
) -> dict[str, Any] | None:
    """尾部坍缩兜底修复：对尾部连续被拒段切片重跑 Qwen3 强制对齐。

    返回 {"rows": [(idx, start, end), ...], "slice_start":, "slice_end":} 或 None（走原兜底）。
    设计要点（基于调试日志实证，见 FIX 文档）：
      * 切片起点 = 最后已对齐行终点 - lead（lead 默认 1.5s，环境变量可调），
        而非坍缩点 collapse_timestamp——日志显示首句尾部真实语音比坍缩点早 ~1.76s，
        从坍缩点切片会丢掉真实音频导致重对齐错位。
      * 切片内文本 = 锚点句（最后已对齐句）+ 尾部被拒文稿，使对齐器有上下文锚点。
      * 校验：切片结果仍坍缩/有被拒/非单调/末行偏离 speech_end 过多则放弃，回退原逻辑。
    """
    try:
        lead = float(os.getenv("SUBFIX_SCRIPT_MATCH_TAIL_LEAD_SECONDS", "1.5"))
        pad_seconds = float(os.getenv("SUBFIX_SCRIPT_MATCH_TAIL_PAD_SECONDS", "30.0"))
        max_slice = float(os.getenv("SUBFIX_SCRIPT_MATCH_TAIL_MAX_SLICE_SECONDS", "180.0"))
        n = len(aligned_rows)
        if n == 0:
            _log_tail_skip("empty_rows")
            return None

        def _is_rej(r: dict[str, Any]) -> bool:
            return str(r.get("row_remap_decision") or "").startswith("rejected")

        # 尾部连续被拒段（必须一路拒到行尾）
        run_end = n - 1
        while run_end >= 0 and _is_rej(aligned_rows[run_end]):
            run_end -= 1
        run_start = run_end + 1
        if run_start >= n:
            _log_tail_skip("no_tail_rejected")
            return None
        if run_start == 0:
            _log_tail_skip("no_anchor")
            return None  # 没有已对齐锚点，无法切片重对齐
        for i in range(run_start, n):
            if not _is_rej(aligned_rows[i]):
                _log_tail_skip("non_tail_rejected")
                return None  # 非尾部被拒，不在本次修复范围

        anchor = aligned_rows[run_start - 1]
        anchor_end = float(anchor.get("end") or 0.0)
        anchor_text = str(anchor.get("text") or "")
        if not anchor_text:
            _log_tail_skip("empty_anchor_text")
            return None
        tail_rows = aligned_rows[run_start:n]
        tail_texts = [str(r.get("text") or "") for r in tail_rows]
        if any(not t for t in tail_texts):
            _log_tail_skip("empty_tail_text")
            return None

        audio_dur = audio_duration_seconds(Path(audio_path))
        slice_start = max(0.0, anchor_end - lead)
        real_end = min(audio_dur, float(speech_end if speech_end is not None else anchor_end) + 0.3)
        if real_end - slice_start < 1.0:
            _log_tail_skip("slice_too_short")
            return None
        if real_end - slice_start > max_slice:
            real_end = slice_start + max_slice
        real_dur = real_end - slice_start

        # 切片并补静音：把对齐器在输入尾部的坍缩点推到静音区，真实语音段不退化。
        slice_path = _cut_wav_slice(Path(audio_path), slice_start, real_end, pad_seconds=pad_seconds)
        if slice_path is None:
            _log_tail_skip("cut_failed")
            return None
        try:
            combined = anchor_text + "。" + "".join(tail_texts)
            try:
                items = qwen3_force_align_items(
                    slice_path, combined, qwen3_language_name(language) or "Chinese"
                )
            except Exception as _e:
                _log_tail_skip("align_exception:" + repr(_e)[:120])
                return None
            if not items:
                _log_tail_skip("empty_items")
                return None
            units = qwen_timestamp_items_to_char_units(items)
            # 仅统计真实语音段（剔除 padding 静音区）内的退化，避免静音末尾的零时长单位误判坍缩
            _real_units = [u for u in units if float(u.get("end", 0)) <= real_dur + 0.05]
            if _tail_zero_duration_run(_real_units) >= 3:
                _log_tail_skip("slice_collapsed_in_real_region")
                return None

            tmp_rows = [{"text": anchor_text}] + [{"text": t} for t in tail_texts]
            try:
                segs = qwen3_remap_timestamp_items_to_rows(
                    tmp_rows, items, fps=fps, timeline_start_frame=0,
                    speech_end=real_end, speech_onset=0.0, speech_regions=None,
                )
            except Exception as _e:
                _log_tail_skip("remap_exception:" + repr(_e)[:120])
                return None
            if len(segs) != len(tmp_rows):
                _log_tail_skip("seg_count_mismatch")
                return None
            tail_segs = segs[1:]
            if any(str(s.get("row_remap_decision") or "").startswith("rejected") for s in tail_segs):
                _log_tail_skip("tail_still_rejected")
                return None  # 切片内仍有被拒（字符丢失），放弃

            results: list[tuple[float, float]] = []
            prev_end = slice_start
            for s in tail_segs:
                s0 = float(s.get("start") or 0.0) + slice_start
                e0 = max(s0 + 0.05, float(s.get("end") or s0) + slice_start)
                # 不允许对齐结果被推入 padding 静音区（说明对齐器把文字铺满含静音的整段）
                if e0 > real_dur + slice_start + 0.5:
                    _log_tail_skip("pushed_into_padding")
                    return None
                if e0 < s0:
                    _log_tail_skip("negative_dur")
                    return None
                if s0 < prev_end - 0.05:
                    _log_tail_skip("not_monotonic")
                    return None  # 非单调
                prev_end = e0
                results.append((round(s0, 3), round(e0, 3)))
            if not results:
                _log_tail_skip("no_results")
                return None
            if speech_end is not None and abs(results[-1][1] - speech_end) > 3.0:
                _log_tail_skip("tail_end_off_%.1f" % (results[-1][1] - speech_end))
                return None  # 末行偏离语音终点过多，重对齐不可信

            out = [(run_start + i, s0, e0) for i, (s0, e0) in enumerate(results)]
            try:
                with open(os.path.expanduser("~\\subfix_remap_debug.log"), "a", encoding="utf-8") as _dlf:
                    _dlf.write(
                        f"[tail_realign] OK run={run_start}-{n - 1} slice={slice_start:.3f}-{real_end:.3f} "
                        f"pad={pad_seconds:.1f} rows={len(out)} tail_end={results[-1][1]:.3f} speech_end={speech_end}\n"
                    )
            except Exception:
                pass
            return {"rows": out, "slice_start": slice_start, "slice_end": real_end, "tail_count": len(out)}
        finally:
            try:
                slice_path.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as _e:
        _log_tail_skip("exception:" + repr(_e)[:200])
        return None


def run_qwen_forced_align_batch_plan(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
) -> dict[str, Any]:
    if not batches:
        raise RuntimeError("缺少 Qwen3 forced align batch plan")

    total_batches = len(batches)
    output_batches: list[dict[str, Any]] = []
    diagnostic: dict[str, Any] = {
        "mode": "fixture" if args.fixture_json else "qwen_forced_align_text_batches",
        "requested_mode": args.mode,
        "align_engine": "qwen3_cpp",
        "batch_count": total_batches,
        "requested_ffmpeg": args.ffmpeg,
        "qwen_item_count": 0,
        "qwen_output_path": "",
    }

    fixture_payload = json.loads(Path(args.fixture_json).read_text(encoding="utf-8")) if args.fixture_json else None
    ffmpeg_path = resolve_ffmpeg(args.ffmpeg) if not fixture_payload else None
    if not fixture_payload:
        diagnostic["ffmpeg"] = ffmpeg_path

    qwen_output_paths: list[str] = []
    qwen_output_dir = Path(args.output).parent / "subfix_qwen_align_outputs"
    with tempfile.TemporaryDirectory(prefix="subfix_qwen_align_batches_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        for batch_index, batch in enumerate(batches, start=1):
            batch_id = str(batch.get("batch_id") or batch_index)
            rows = batch["rows"]
            batch_fps = float(batch.get("fps") or args.fps or 30.0)
            timeline_start_frame = int(batch.get("timeline_start_frame") or 0)
            try:
                write_progress(
                    progress_path,
                    "cut_audio" if not fixture_payload else "write_output",
                    f"正在处理第 {batch_index}/{total_batches} 批",
                    batch_index=batch_index,
                    total_batches=total_batches,
                    align_engine="qwen3_cpp",
                )
                if fixture_payload:
                    speech_regions = fixture_payload.get("speech_regions") or []
                    speech_onsets = fixture_payload.get("speech_onsets") or [
                        region.get("start")
                        for region in speech_regions
                        if isinstance(region, dict)
                    ]
                    raw_payload = dict(fixture_payload)
                else:
                    cut_path = tmp_root / f"batch_{batch_index}.wav"
                    audio_path = Path(str(batch.get("audio") or ""))
                    if not audio_path.exists():
                        raise RuntimeError("audio file not found")
                    cut_audio(
                        str(ffmpeg_path),
                        audio_path,
                        cut_path,
                        float(batch.get("source_start") or 0.0),
                        batch.get("source_end"),
                        int(batch.get("audio_channel_index")) if batch.get("audio_channel_index") else None,
                    )
                    write_progress(
                        progress_path,
                        "qwen_forced_align",
                        "正在执行 Qwen3 forced alignment",
                        batch_index=batch_index,
                        total_batches=total_batches,
                        align_engine="qwen3_cpp",
                    )
                    speech_regions, speech_onsets, onset_diagnostic = detect_speech_regions(cut_path)
                    raw_payload = qwen3_cpp_forced_align_text_rows(
                        cut_path,
                        rows,
                        args.language or "zh",
                        batch_fps,
                        timeline_start_frame,
                        qwen_output_dir,
                    )
                    raw_payload = dict(raw_payload)
                    raw_payload.setdefault("diagnostic", {}).update(onset_diagnostic)

                aligned_payload = {
                    "aligned_rows": raw_payload.get("aligned_rows") or [],
                    "segments": raw_payload.get("segments") or [],
                    "text": str(raw_payload.get("text") or "").strip(),
                }
                batch_diagnostic = dict(raw_payload.get("diagnostic") or {})
                batch_diagnostic["row_count"] = len(rows)
                batch_diagnostic["aligned_count"] = len(aligned_payload["aligned_rows"])
                diagnostic["qwen_item_count"] += int(batch_diagnostic.get("qwen_item_count") or 0)
                if batch_diagnostic.get("qwen_output_path"):
                    qwen_output_paths.append(str(batch_diagnostic["qwen_output_path"]))
                output_batches.append(
                    {
                        "batch_id": batch_id,
                        "ok": True,
                        "aligned_rows": aligned_payload["aligned_rows"],
                        "segments": aligned_payload["segments"],
                        "speech_onsets": speech_onsets,
                        "speech_regions": speech_regions,
                        "text": aligned_payload["text"],
                        "diagnostic": batch_diagnostic,
                    }
                )
            except Exception as exc:
                output_batches.append(
                    {
                        "batch_id": batch_id,
                        "ok": False,
                        "aligned_rows": [],
                        "segments": [],
                        "speech_onsets": [],
                        "speech_regions": [],
                        "error": str(exc),
                        "diagnostic": {"row_count": len(rows), "align_engine": "qwen3_cpp"},
                    }
                )

    diagnostic["qwen_output_path"] = ",".join(qwen_output_paths)
    # 每批的 JSON 已在解析后即删；此处仅在输出目录为空时移除本工具创建的目录，
    # 绝不 rmtree（目录里若有用户/旧版本残留文件则原样保留）。
    try:
        qwen_output_dir.rmdir()
    except OSError:
        pass
    diagnostic["successful_batch_count"] = sum(1 for batch in output_batches if batch.get("ok") is True)
    diagnostic["failed_batch_count"] = total_batches - diagnostic["successful_batch_count"]
    failed_batch_errors = [
        f"第 {batch.get('batch_id')} 批：{batch.get('error')}"
        for batch in output_batches
        if batch.get("ok") is False and str(batch.get("error") or "").strip()
    ]
    if failed_batch_errors:
        diagnostic["failed_batch_errors"] = failed_batch_errors[:3]
    write_progress(
        progress_path,
        "done",
        "批量 Qwen3 forced alignment helper 已完成",
        batch_index=total_batches,
        total_batches=total_batches,
        align_engine="qwen3_cpp",
    )
    payload = {
        "ok": diagnostic["failed_batch_count"] == 0,
        "model": os.getenv(QWEN3_CPP_ALIGNER_GGUF_ENV) or args.model,
        "batches": output_batches,
        "diagnostic": diagnostic,
    }
    if failed_batch_errors:
        payload["error"] = "；".join(failed_batch_errors[:3])
    return payload


SCRIPT_MATCH_MAX_SECONDS = 1800.0


def load_script_match_plan(plan_json: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float, int, str]:
    """解析文稿匹配批量计划：音频源列表 + 文稿行 + fps + 时间线起点 + 语言。"""
    if not plan_json:
        raise RuntimeError("缺少文稿匹配批量计划")
    # 文稿文本可能夹带损坏/混编字节（Fusion 文本框在中文 Windows 下偶发）：
    # 先按 UTF-8 严格解码；失败再按系统默认编码（中文 Windows 为 cp936/GBK）尝试；
    # 仍失败则按 UTF-8 容错解码，最大限度保留有效字符（损坏字节以 U+FFFD 占位）。
    raw = Path(plan_json).read_bytes()
    try:
        plan_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        import locale
        enc = locale.getpreferredencoding(False) or "gbk"
        try:
            plan_text = raw.decode(enc)
        except UnicodeDecodeError:
            plan_text = raw.decode("utf-8", errors="replace")
    payload = json.loads(plan_text)
    fps = float(payload.get("fps") or 30.0)
    timeline_start_frame = int(float(payload.get("timeline_start_frame") or 0))
    language = payload.get("language") or "auto"
    audio_sources: list[dict[str, Any]] = []
    for src in payload.get("audio_sources") or []:
        if not src:
            continue
        audio_sources.append({
            "audio": str(src.get("audio") or ""),
            "source_start": float(src.get("source_start") or 0.0),
            "source_end": float(src["source_end"]) if src.get("source_end") is not None else None,
            "timeline_start_frame": int(float(src.get("timeline_start_frame") or 0)),
            "fps": float(src.get("fps") or fps),
            "audio_channel_index": int(src["audio_channel_index"]) if int(src.get("audio_channel_index") or 0) > 0 else None,
        })
    rows = normalize_rows_payload(payload.get("rows") or [])
    # 去除容错解码产生的替换符，避免脏字符进入对齐模型。
    for row in rows:
        txt = row.get("text")
        if isinstance(txt, str) and ("\ufffd" in txt):
            row["text"] = txt.replace("\ufffd", "")
    return audio_sources, rows, fps, timeline_start_frame, language


def run_script_match_align(
    audio_sources: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
    language: str,
    progress_path: Path | None,
) -> dict[str, Any]:
    """文稿匹配核心：拼接选区音频 → Qwen3 强制对齐 → 输出 subtitle_rows。

    多源音频按时间线顺序用 ffmpeg amix+adelay 叠加（源间静音间隙保留为静音，因此
    绝对时间轴与选区一致）；单源则直接裁剪。随后用 transformers 版 Qwen3ForcedAligner
    把整段文稿对齐到拼接后的音频，字幕文字严格等于文稿。
    """
    if not audio_sources:
        raise RuntimeError("文稿匹配缺少音频源")
    if not rows:
        raise RuntimeError("文稿匹配缺少文稿内容")
    ffmpeg_path = resolve_ffmpeg(None)
    if not ffmpeg_path:
        raise RuntimeError("未找到 ffmpeg，无法拼接音频")

    ordered = sorted(audio_sources, key=lambda s: float(s.get("timeline_start_frame") or 0))
    with tempfile.TemporaryDirectory(prefix="subfix_script_match_") as tmp_root:
        tmp_root_path = Path(tmp_root)
        cut_paths: list[tuple[Path, float]] = []
        for index, src in enumerate(ordered, start=1):
            audio_path = Path(str(src.get("audio") or ""))
            if not audio_path.exists():
                raise RuntimeError(f"音频文件不存在: {audio_path}")
            cut_path = tmp_root_path / f"src_{index}.wav"
            cut_audio(
                ffmpeg_path,
                audio_path,
                cut_path,
                float(src.get("source_start") or 0.0),
                src.get("source_end"),
                int(src["audio_channel_index"]) if src.get("audio_channel_index") else None,
            )
            cut_paths.append((cut_path, float(src.get("timeline_start_frame") or 0)))

        if len(cut_paths) == 1:
            combined_path: Path = cut_paths[0][0]
        else:
            filter_parts: list[str] = []
            input_args: list[str] = []
            for i, (cut_path, offset_frame) in enumerate(cut_paths, start=1):
                input_args.extend(["-i", str(cut_path)])
                delay_ms = max(0, int(round((offset_frame - timeline_start_frame) / max(fps, 1.0) * 1000)))
                filter_parts.append(f"[{i - 1}:a]adelay=delays={delay_ms}|{delay_ms}[a{i}]")
            amix_inputs = "".join(f"[a{i}]" for i in range(1, len(cut_paths) + 1))
            filter_parts.append(f"{amix_inputs}amix=inputs={len(cut_paths)}:normalize=0[aout]")
            combined_path = tmp_root_path / "combined.wav"
            cmd = [
                ffmpeg_path, "-y", "-hide_banner", "-nostdin",
                *input_args,
                "-filter_complex", ";".join(filter_parts),
                "-map", "[aout]",
                "-ac", "1", "-ar", "16000", str(combined_path),
            ]
            subprocess.run(cmd, check=True, text=True, encoding="utf-8", errors="replace", capture_output=True)

        # combined.wav 由上面的 ffmpeg 刚产出；读不出时长说明管线真出错，
        # 必须显式报错，不能再静默吞成 0 秒继续生成塌缩字幕。
        duration = audio_duration_seconds(combined_path)
        if duration <= 0:
            raise RuntimeError("文稿匹配选区音频时长为 0，无法对齐，请检查选区是否包含音频")
        if duration > SCRIPT_MATCH_MAX_SECONDS:
            raise RuntimeError(
                f"文稿匹配选区过长（约 {duration:.0f}s，上限 {SCRIPT_MATCH_MAX_SECONDS:.0f}s），请缩小 In/Out 选区后重试"
            )

        # 为尚无时长的文稿行设定名义时长占位，低置信度回退行不至于全部塌缩到 0 帧。
        nominal_slot = max(1.0, duration / max(1, len(rows)))
        for i, row in enumerate(rows, start=1):
            row["index"] = i
            ns_start = timeline_start_frame + int(round((i - 1) * nominal_slot * fps))
            row["start_frame"] = ns_start
            row["end_frame"] = ns_start + max(1, int(round(nominal_slot * fps)))

        if progress_path:
            write_progress(progress_path, "qwen_forced_align", "正在执行 Qwen3 文稿强制对齐", align_engine="qwen3_py")
        result = qwen3_py_forced_align_text_rows(combined_path, rows, language, fps, timeline_start_frame)

        aligned_rows = result.get("aligned_rows") or []
        subtitle_rows: list[dict[str, Any]] = []
        for i, row in enumerate(aligned_rows, start=1):
            row["index"] = i
            subtitle_rows.append(row)
        if not subtitle_rows:
            raise RuntimeError("Qwen3 文稿对齐未产出字幕，请检查文稿与音频是否匹配")

    payload: dict[str, Any] = {
        "ok": True,
        "subtitle_rows": subtitle_rows,
        "diagnostic": dict(result.get("diagnostic") or {}),
    }
    payload["diagnostic"]["mode"] = "script_match_align"
    payload["diagnostic"]["source_count"] = len(audio_sources)
    return payload


def sweep_stale_temp_dirs(root=None, now=None, max_age_days=7):
    """清理系统 TEMP 下本产品残留的旧临时目录。

    只删除同时满足以下条件的条目：位于 TEMP 根目录、名字以小写 "subfix_" 开头、
    是非符号链接的目录、mtime 早于 max_age_days 天前。
    新目录、非目录文件、其他大小写前缀（如 SubFixDumps）一律不动；任何异常不外抛。
    返回实际删除的目录数量。
    """
    try:
        if root is None:
            root = tempfile.gettempdir()
        if now is None:
            now = time.time()
        cutoff = now - float(max_age_days) * 86400.0
        try:
            names = os.listdir(root)
        except OSError:
            return 0
        removed = 0
        for name in names:
            if not name.startswith("subfix_"):
                continue
            path = os.path.join(root, name)
            try:
                if (
                    os.path.isdir(path)
                    and not os.path.islink(path)
                    and os.path.getmtime(path) < cutoff
                ):
                    shutil.rmtree(path, ignore_errors=True)
                    if not os.path.exists(path):
                        removed += 1
            except OSError:
                continue
        return removed
    except Exception:
        return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sweep_stale_temp_dirs()
    except Exception:
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio")
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--language", default="auto")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--source-start", type=float, default=0.0)
    parser.add_argument("--source-end", type=float)
    parser.add_argument("--audio-channel-index", type=int)
    parser.add_argument("--rows-json")
    parser.add_argument("--batch-plan-json")
    parser.add_argument("--windows-json")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--timeline-start-frame", type=int, default=0)
    parser.add_argument("--srt-output")
    parser.add_argument("--srt-base-frame", type=int)
    parser.add_argument("--subtitle-mode", choices=("narration", "live"), default="narration")
    parser.add_argument(
        "--generate-engine",
        choices=("v5",),
        default="v5",
    )
    parser.add_argument("--segmentation-profile")
    parser.add_argument("--hotwords-json")
    parser.add_argument("--calibration-json", action="append", default=[])
    parser.add_argument(
        "--backend",
        choices=(
            "auto",
            "mimo_asr",
            "qwen3_asr",
            "mlx_whisper",
            "openai_whisper",
            "doubao_asr",
            "doubao_asr_v2",
        ),
        default="auto",
    )
    parser.add_argument(
        "--mode",
        choices=(
            "qwen_forced_align_text_batches",
            "script_match_align",
            "transcribe",
            "generate_subtitles",
            "generate_subtitles_batch",
            "learn_segmentation_profile",
            "onsets",
        ),
        default="transcribe",
    )
    parser.add_argument("--fixture-json")
    parser.add_argument("--progress-json")
    parser.add_argument("--diagnostic-output")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help=(
            "字幕长度（每条字幕最大字数）注入点，仅作用于 v4/v5 主断句路径"
            " (segment_canonical_units)。不传时保持现有 length_model/profile 行为不变。"
        ),
    )
    args = parser.parse_args(argv)

    output_path = Path(args.output)
    progress_path = Path(args.progress_json) if args.progress_json else None
    try:
        diagnostic: dict[str, Any] = {
            "requested_ffmpeg": args.ffmpeg,
            "requested_mode": args.mode,
            "requested_backend": args.backend,
            "source_start": args.source_start,
            "source_end": args.source_end,
        }
        transcribe_windows = load_transcribe_windows(args.windows_json)
        if args.mode == "learn_segmentation_profile":
            if not args.calibration_json:
                raise RuntimeError("缺少 --calibration-json")
            calibration_payloads: list[dict[str, Any]] = []
            for calibration_path_value in args.calibration_json:
                calibration_path = Path(calibration_path_value).expanduser()
                calibration_payload = json.loads(calibration_path.read_text(encoding="utf-8"))
                calibration_payload["source_path"] = str(calibration_path)
                calibration_payloads.append(calibration_payload)
            profile = generate_v5.build_segmentation_profile_v4(calibration_payloads)
            write_payload(output_path, profile)
            return 0 if profile.get("diagnostic", {}).get("training_region_count") else 1
        if args.mode == "qwen_forced_align_text_batches":
            payload = run_qwen_forced_align_batch_plan(load_batch_plan(args.batch_plan_json), args, progress_path)
            write_payload(output_path, payload)
            return 0 if payload.get("ok") else 1
        if args.mode in {"generate_subtitles", "generate_subtitles_batch"}:
            if args.mode == "generate_subtitles_batch":
                batches = load_generate_subtitles_batch_plan(args.batch_plan_json)
            else:
                if not args.audio:
                    raise RuntimeError("缺少 --audio")
                if args.source_end is not None and args.source_end <= args.source_start:
                    raise RuntimeError("音频结束时间必须大于开始时间")
                batches = [{
                    "batch_id": "1",
                    "audio": args.audio,
                    "source_start": args.source_start,
                    "source_end": args.source_end,
                    "timeline_start_frame": args.timeline_start_frame,
                    "fps": args.fps,
                    "audio_channel_index": args.audio_channel_index,
                    "track_index": 1,
                    "track_order": 1,
                    "item_index": 1,
                }]
            fixture_payload = json.loads(Path(args.fixture_json).read_text(encoding="utf-8")) if args.fixture_json else None
            payload = run_generate_subtitles_batch_plan(
                batches,
                args,
                progress_path,
                fixture_payload=fixture_payload,
            )
            if args.srt_output:
                srt_path = Path(args.srt_output)
                srt_base_frame = args.srt_base_frame if args.srt_base_frame is not None else args.timeline_start_frame
                payload.setdefault("diagnostic", {})["srt_output"] = str(srt_path)
                payload["diagnostic"]["srt_row_count"] = write_subtitle_rows_to_srt(
                    srt_path,
                    payload.get("subtitle_rows") or [],
                    args.fps,
                    srt_base_frame,
                )
            write_payload(output_path, build_generate_writeback_payload(payload))
            if args.diagnostic_output:
                write_payload(
                    Path(args.diagnostic_output).expanduser(),
                    sanitize_generate_diagnostic_payload(payload),
                )
            completion_index, completion_total = generate_helper_completion_progress(
                payload.get("diagnostic", {})
            )
            write_progress(
                progress_path,
                "done",
                "批量生成字幕 helper 已完成",
                batch_index=payload.get("diagnostic", {}).get("batch_count", 0),
                total_batches=payload.get("diagnostic", {}).get("batch_count", 0),
                progress_index=completion_index,
                progress_total=completion_total,
            )
            return 0 if payload.get("ok") else 1
        if args.mode == "script_match_align":
            audio_sources, rows, fps, timeline_start_frame, language = load_script_match_plan(args.batch_plan_json)
            payload = run_script_match_align(audio_sources, rows, fps, timeline_start_frame, language, progress_path)
            write_payload(output_path, build_generate_writeback_payload(payload))
            write_progress(progress_path, "done", "文稿匹配对齐已完成", progress_index=1, progress_total=1)
            return 0 if payload.get("ok") else 1
        if args.fixture_json:
            write_progress(progress_path, "write_output", "正在写入 fixture 对齐结果")
            raw_payload = json.loads(Path(args.fixture_json).read_text(encoding="utf-8"))
            audio_used = str(args.fixture_json)
            diagnostic["mode"] = "fixture"
            if args.mode == "transcribe" and transcribe_windows:
                fixture_segments = normalize_segments(raw_payload)
                raw_payload = {
                    "backend": "fixture",
                    "model": args.model,
                    "segments": [],
                    "text": "",
                    "windows": [
                        {
                            "ok": True,
                            "window_id": str(window.get("window_id") or index),
                            "review_type": window.get("review_type") or "",
                            "row_label": window.get("row_label") or "",
                            "source_start": window.get("source_start"),
                            "source_end": window.get("source_end"),
                            "backend": "fixture",
                            "model": args.model,
                            "segments": fixture_segments,
                            "speech_onsets": raw_payload.get("speech_onsets") or [],
                            "speech_regions": raw_payload.get("speech_regions") or [],
                            "text": str(raw_payload.get("text") or "").strip(),
                            "diagnostic": {"mode": "fixture_window"},
                        }
                        for index, window in enumerate(transcribe_windows, start=1)
                    ],
                }
            rows = load_rows(args.rows_json)
            speech_regions = raw_payload.get("speech_regions") or []
            speech_onsets = raw_payload.get("speech_onsets") or [region.get("start") for region in speech_regions if isinstance(region, dict)]
            if args.mode == "generate_subtitles":
                segments = normalize_segments(raw_payload)
                raw_payload = dict(raw_payload)
                raw_payload["segments"] = segments
                raw_payload["subtitle_rows"] = generate_subtitle_rows_from_segments(
                    segments,
                    args.fps,
                    args.timeline_start_frame,
                    segmentation_profile=load_segmentation_profile(args.segmentation_profile),
                    speech_regions=speech_regions,
                    subtitle_mode=args.subtitle_mode,
                )
                diagnostic["generated_subtitle_count"] = len(raw_payload["subtitle_rows"])
            diagnostic["backend"] = "fixture"
        else:
            if not args.audio or not Path(args.audio).exists():
                raise RuntimeError("audio file not found")
            rows = load_rows(args.rows_json)
            if args.mode == "transcribe" and transcribe_windows:
                raw_payload = run_transcribe_windows(Path(args.audio), transcribe_windows, args, progress_path)
                diagnostic["mode"] = args.mode
                diagnostic["ffmpeg"] = raw_payload.get("diagnostic", {}).get("ffmpeg")
                diagnostic["backend"] = raw_payload.get("backend")
                diagnostic["model"] = raw_payload.get("model") or args.model
                if raw_payload.get("diagnostic", {}).get("fallback_errors"):
                    diagnostic["fallback_errors"] = raw_payload.get("diagnostic", {}).get("fallback_errors")
                audio_used = str(args.audio)
                speech_regions = []
                speech_onsets = []
                segments = normalize_segments(raw_payload)
                diagnostic["raw_segment_count"] = len(raw_payload.get("segments") or [])
                diagnostic["segment_count"] = len(segments)
                diagnostic["text_length"] = len(str(raw_payload.get("text") or "").strip())
                write_progress(progress_path, "write_output", "正在写入批量局部转写结果")
                write_payload(
                    output_path,
                    {
                        "ok": True,
                        "model": str(raw_payload.get("model") or args.model),
                        "backend": raw_payload.get("backend") or diagnostic.get("backend"),
                        "audio": audio_used,
                        "segments": segments,
                        "aligned_rows": raw_payload.get("aligned_rows") or [],
                        "speech_onsets": speech_onsets,
                        "speech_regions": speech_regions,
                        "text": str(raw_payload.get("text") or "").strip(),
                        "windows": raw_payload.get("windows") or [],
                        "diagnostic": diagnostic,
                    },
                )
                write_progress(progress_path, "done", "批量局部转写 helper 已完成")
                return 0
            with tempfile.TemporaryDirectory(prefix="subfix_asr_") as tmp_dir:
                cut_path = Path(tmp_dir) / "audio.wav"
                ffmpeg_path = resolve_ffmpeg(args.ffmpeg)
                diagnostic["mode"] = args.mode
                diagnostic["ffmpeg"] = ffmpeg_path
                write_progress(progress_path, "cut_audio", "正在截取音频片段")
                cut_audio(ffmpeg_path, Path(args.audio), cut_path, args.source_start, args.source_end, args.audio_channel_index)
                diagnostic["cut_audio_bytes"] = cut_path.stat().st_size if cut_path.exists() else 0
                write_progress(progress_path, "detect_speech", "正在检测音频起点")
                speech_regions, speech_onsets, onset_diagnostic = detect_speech_regions(cut_path)
                diagnostic.update(onset_diagnostic)
                if args.mode == "onsets":
                    raw_payload = {"segments": [], "aligned_rows": [], "text": ""}
                else:
                    raw_payload = transcribe_with_backend(cut_path, args.model, args.language, args.backend)
                    diagnostic["backend"] = raw_payload.get("backend")
                    diagnostic["model"] = raw_payload.get("model") or args.model
                    backend_diagnostic = raw_payload.get("diagnostic") if isinstance(raw_payload.get("diagnostic"), dict) else {}
                    for key in (
                        "forced_aligner",
                        "device_map",
                        "requested_model",
                        "forced_align_item_count",
                        "uses_word_timing",
                    ):
                        if key in backend_diagnostic:
                            diagnostic[key] = backend_diagnostic[key]
                    if raw_payload.get("fallback_errors"):
                        diagnostic["fallback_errors"] = raw_payload.get("fallback_errors")
                    if args.mode == "generate_subtitles":
                        segments = normalize_segments(raw_payload)
                        raw_payload = dict(raw_payload)
                        raw_payload["segments"] = segments
                        raw_payload["subtitle_rows"] = generate_subtitle_rows_from_segments(
                            segments,
                            args.fps,
                            args.timeline_start_frame,
                            segmentation_profile=load_segmentation_profile(args.segmentation_profile),
                            speech_regions=speech_regions,
                            subtitle_mode=args.subtitle_mode,
                        )
                        diagnostic["generated_subtitle_count"] = len(raw_payload["subtitle_rows"])
                        diagnostic["generated_subtitles_used_word_timing"] = any(segment.get("words") for segment in segments)
                audio_used = str(cut_path)

        segments = normalize_segments(raw_payload)
        diagnostic["raw_segment_count"] = len(raw_payload.get("segments") or [])
        diagnostic["segment_count"] = len(segments)
        diagnostic["text_length"] = len(str(raw_payload.get("text") or "").strip())
        if args.srt_output and args.mode in {"generate_subtitles", "generate_subtitles_batch"}:
            srt_path = Path(args.srt_output)
            srt_base_frame = args.srt_base_frame if args.srt_base_frame is not None else args.timeline_start_frame
            diagnostic["srt_output"] = str(srt_path)
            diagnostic["srt_row_count"] = write_subtitle_rows_to_srt(
                srt_path,
                raw_payload.get("subtitle_rows") or [],
                args.fps,
                srt_base_frame,
            )
        write_progress(progress_path, "write_output", "正在写入对齐结果")
        write_payload(
            output_path,
            {
                "ok": True,
                "model": str(raw_payload.get("model") or args.model),
                "backend": raw_payload.get("backend") or diagnostic.get("backend"),
                "audio": audio_used,
                "segments": segments,
                "aligned_rows": raw_payload.get("aligned_rows") or [],
                "subtitle_rows": raw_payload.get("subtitle_rows") or [],
                "speech_onsets": speech_onsets,
                "speech_regions": speech_regions,
                "text": str(raw_payload.get("text") or "").strip(),
                "windows": raw_payload.get("windows") or [],
                "diagnostic": diagnostic,
            },
        )
        write_progress(progress_path, "done", "对齐 helper 已完成")
        return 0
    except Exception as exc:
        write_progress(progress_path, "failed", str(exc))
        write_payload(output_path, {"ok": False, "error": str(exc), "segments": []})
        if args.mode in {"generate_subtitles", "generate_subtitles_batch"} and args.diagnostic_output:
            failure_code = "generate_failed"
            if args.generate_engine in {"v4", "v5"} and isinstance(exc, generate_v4.V4AlignmentError):
                failure_code = f"{args.generate_engine}_alignment_failed"
            write_payload(
                Path(args.diagnostic_output).expanduser(),
                sanitize_generate_diagnostic_payload(
                    {
                        "ok": False,
                        "diagnostic": {
                            "subtitle_mode": args.subtitle_mode,
                            "generate_engine": args.generate_engine,
                            "failure_code": failure_code,
                        },
                        "subtitle_rows": [],
                    }
                ),
            )
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
