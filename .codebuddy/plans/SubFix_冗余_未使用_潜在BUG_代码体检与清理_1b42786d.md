---
name: SubFix 冗余/未使用/潜在BUG 代码体检与清理
overview: 对 SubFix.lua 做三类体检：冗余代码、未使用代码、潜在BUG。通过只读的 Python 静态分析（提取全部顶层函数定义并按词边界统计引用）发现 30 个「仅定义、零引用」的死代码函数（已确认全文件无 _G[...]/loadstring 动态分发，启发式可靠，且以 export_srt 真死代码复核验证）。另发现 export_srt 被 do_export_action 取代、subfix_ps_dq 是 subfix_dq 的未使用副本等冗余。潜在BUG方面，上一轮已修复调试日志磁盘泄漏与 subfix_temp_dir 递归；本轮将针对后台 curl/嵌套RunLoop/取消逻辑与 FFI 写文件回退再做专项审查。所有清理均在确认无调用方后进行，并用 lua-language-server 静态校验 + 重新部署 + SHA256 比对保证不影响正常功能。
todos:
  - id: verify-unused
    content: 用 [skill:lsp-code-analysis] 语义核查 30 个候选函数引用，确认未使用并排除误报
    status: completed
  - id: remove-dead-code
    content: 删除确认未使用的死代码函数及冗余实现 export_srt、subfix_ps_dq
    status: completed
    dependencies:
      - verify-unused
  - id: review-potential-bugs
    content: 审查后台 curl/RunLoop/取消与 FFI 写文件回退等潜在 BUG 并修复
    status: completed
  - id: static-check-deploy
    content: 用 lua-language-server 校验并重新部署，核对安装副本 SHA256 一致
    status: completed
    dependencies:
      - remove-dead-code
      - review-potential-bugs
---

## 用户需求

对 SubFix.lua 进行三类代码体检，并在不影响正常功能的前提下定位、处理：

1. 冗余代码（重复/近似重复实现、被新实现取代的遗留代码）
2. 未使用代码（定义后从未被引用的函数/变量/常量）
3. 潜在 BUG（逻辑错误、平台差异、竞态、资源泄漏等）

## 核查结论（已通过只读静态分析确认）

- **未使用/冗余代码**：用只读 Python 静态分析提取全部顶层函数定义并按词边界统计引用，得到 30 个「仅定义、零引用」的死代码候选（见下）。全文件 `_G[...]`/`loadstring`/`dofile`/`pcall(load` 命中数为 0，排除按名动态分发，启发式可靠；并以 `export_srt` 真死代码复核验证。
- `export_srt`（#18381-18422）全文件仅 1 处（定义）。实际导出链路为 `ExportConfirmBtn.Clicked`（#18804）→ `do_export_action(...)`（#18812），旧 `export_srt` 从未被调用，既是未使用代码也是被取代的冗余实现。
- `subfix_ps_dq`（#118-121，PowerShell 单引号转义器）全文件仅 1 处（定义）；脚本统一使用 `subfix_dq`（#112，双引号转义，9 处引用），其为未使用的冗余副本。
- 其余 28 个候选（如 `show_log_window`、`perform_redo`、`RefreshSubtitleTree`、`pre_delivery_*`、`selection_writeback_*` 等）均经引用计数判定为死代码。
- **潜在 BUG**：上一轮已修复「调试日志 SubFix_debug.log 清理路径不一致导致的磁盘泄漏」与「`subfix_temp_dir` 非 Windows 空环境变量时的无限递归」；本轮将专项审查后台 curl + 嵌套 RunLoop + 取消/kill 与 FFI 写文件回退等高风险区。

## 技术栈

- 运行环境：单文件 Lua 脚本（LuaJIT），运行于 DaVinci Resolve / Fusion；无外部框架。
- 校验/分析工具：本地 `lua-language-server`（sumneko 3.19.1）做 `--check` 静态校验；Python 做只读引用计数分析；[skill:lsp-code-analysis] 做语义级「查找引用」二次确认。
- 部署：覆盖安装副本 `C:\Users\10840\AppData\Roaming\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\SubFix\SubFix.lua`，并以 SHA256 比对一致性。

## 实现方案

### 策略

1. **语义二次确认（防误报）**：对 30 个候选函数使用 [skill:lsp-code-analysis] 的「查找引用/符号查找」做语义级核查，确认确实无任何调用方（函数内私有子函数一并核查），排除任何潜在误报后再删除。
2. **安全删除死代码**：逐个删除确认未使用的函数。删除前验证：该函数定义体内未调用「仅被它自己使用」的私有子函数；删除不改变任何被保留函数的行为。优先删除相互独立、无副作用的辅助函数。
3. **合并/删除冗余实现**：删除 `export_srt`（已被 `do_export_action` 取代）、`subfix_ps_dq`（已被 `subfix_dq` 取代）。
4. **潜在 BUG 专项审查**：通读 `后台 curl + 嵌套 RunLoop + 取消/kill`（#17497-17630、#6880-6890、#7122-7275）与 `FFI 写文件回退`（#360-375），修复确属逻辑错误的点；保持 `pcall` 包裹，不改动导出/备份/AI 核心流程与用户数据。
5. **校验与部署**：`lua-language-server --check` 通过（exit 0，无 error/warn）；重新部署并比对 SHA256。

### 实施注意

- 所有改动仅触及死代码、冗余实现与确认的逻辑错误点，导出/备份/AI 请求/窗口开关等核心功能路径不变。
- 文件/路径操作继续以 `pcall` 包裹，保证异常时主流程不中断。
- 删除函数后若触发 Lua 全局命名空间变化，需以静态校验确认无残留引用。

## 架构设计

- 单文件脚本，函数为全局命名空间；删除未使用函数不改变模块结构，仅减少体积与认知负担。
- 清理生命周期不涉及新增模块，仅在现有 `subfix_cleanup_subfix_logs` 等既有机制上保持一致性。

## 目录结构

```
f:\SubFix-main\SubFix-main\
└── SubFix.lua   # [MODIFY] 删除 30 个确认未使用的死代码函数；删除/合并冗余实现 export_srt、subfix_ps_dq；修复专项审查中确认的潜在 BUG。所有改动保持 pcall 包裹，不影响导出/备份/AI 核心流程。
```

（部署目标：上述安装副本目录，部署后 SHA256 与源一致。）

## 关键代码定位（死代码候选，行号: 函数名）

- 118: subfix_ps_dq｜423: subfix_open_url｜3580: get_checkbox_checked｜3759: set_layout_row_collapsed｜3957: set_tree_node_display_text｜4707: update_pending_report_detail_for_item｜5356: perform_redo｜6669: apply_shared_state_to_window｜7478: restore_subtitle_track_state_snapshot｜7820: activate_subtitle_target_track_via_ui
- 13588: RefreshSubtitleTree｜13601: show_log_window｜14215: snapshot_subtitle_track_timing_keys｜14246: filter_new_subtitle_items_all_tracks｜14258: new_items_match_loaded_selection_rows｜14323: build_selection_writeback_diagnostic｜14388: collect_selection_overlapping_items｜14406: delete_selection_overlapping_items｜14423: append_srt_to_timeline_with_clipinfo｜14525: write_rows_to_update_srt
- 16188: parse_ai_fix_payload｜16319: validate_ai_fix_candidate｜18381: export_srt｜21351: join_speech_check_asr_text_for_rows｜21389: collect_speech_consistency_groups｜21921: should_run_speech_review_window｜22188: collect_pre_delivery_ctc_consistency_issues｜22219: collect_pre_delivery_speech_consistency_issues｜22667: format_pre_delivery_issue_report_entries｜22681: pre_delivery_issue_summary_text

## Agent Extensions

### Skill

- **lsp-code-analysis**
- Purpose: 对 30 个「仅定义、零引用」候选函数做语义级「查找引用 / 符号查找」，二次确认它们确实无任何调用方（含函数内私有子函数），排除 Python 文本分析的误报。
- Expected outcome: 产出每个候选函数的引用清单；确认可安全删除的函数集合，并标记任何存在引用、不应删除的函数。