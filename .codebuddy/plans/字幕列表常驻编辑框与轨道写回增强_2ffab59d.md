---
name: 字幕列表常驻编辑框与轨道写回增强
overview: 在「改个字幕」主窗口的字幕列表下方新增一个常驻编辑框：选中某条字幕后即可修改其文本，保存后更新内存中的字幕数据（current_rows）与列表显示，并通过现有「更新到时间线」按钮统一写回 DaVinci 字幕轨道。保留双击=跳转时间线，不改动现有 ✎ 列编辑与整轨写回逻辑。
todos:
  - id: add-edit-box-ui
    content: 在列表与底部栏间插入编辑框UI，用[subagent:code-explorer]核对ID与插入点
    status: completed
  - id: add-edit-functions
    content: 新增全局编辑/保存/重置函数，复用 save_preview_edit_dialog_changes
    status: completed
    dependencies:
      - add-edit-box-ui
  - id: wire-events
    content: 接线选中加载与保存按钮，保留双击跳转
    status: completed
    dependencies:
      - add-edit-functions
  - id: verify
    content: 复测主chunk local为0并校验语法与轨道写回
    status: completed
    dependencies:
      - wire-events
---

## 用户需求

- 编辑入口：在「改个字幕」主窗口字幕列表下方新增一个常驻编辑框；选中某条字幕后，可直接修改其文本，点「保存」或回车即保存。
- 双击行为：保留「左键双击 = 时间线跳转到该字幕区域」不变。
- 写回时机：编辑只更新内存中的字幕数据（current_rows）与列表显示；点现有「更新到时间线」按钮时统一整轨写回 DaVinci 字幕轨道（现状增强，不做每改即写回）。

## 功能边界

- 编辑框用于修改字幕纯文本（支持多行）。
- 不改动现有 ✎ 列编辑弹窗、双击跳转、整轨写回逻辑；保持二者并存。
- 仅涉及 SubFix.lua 单文件修改。

## 技术栈与约束

- 语言：Lua 5.x（DaVinci Resolve Fusion UI Manager 声明式 UI），单文件脚本 SubFix.lua。
- 硬性约束：上一轮已将主 chunk（顶层）local 数量由 339 降到 0，解决了 200 上限导致的加载崩溃。本次新增代码**严禁引入任何顶层 local 变量/函数**——所有新函数必须以全局 `function 名称()` 形式声明；UI 构建块 `create_full_content()` 内只能使用 `ui:...` 声明式调用，不得新增顶层局部变量。

## 实现方案

1. **UI 布局**：在 `create_full_content()` 中，字幕列表 `SubtitleTree`（约 18236–18242）与底部栏 `BottomBar`（约 18244）之间插入一个 `VGroup`（ID 例 `SubtitleEditArea`）：

- `Label`（ID `EditSubtitleLabel`）：默认文案“请选择一条字幕”，选中后显示 “#序号  时间码”。
- `TextEdit`（ID `EditSubtitleText`）：多行、`Weight=1`、`AcceptRichText=false`，承载选中字幕文本。
- `HGroup`：`Button`（`EditSubtitleSaveBtn`，“保存修改”）、`Button`（`EditSubtitleRevertBtn`，“还原”）。

2. **复用现有保存逻辑**：新增全局函数 `load_selected_subtitle_into_edit_box(win, row)`、`sync_edit_box_from_selected(win)`、`reset_subtitle_edit_box(win)`。保存时直接复用 `save_preview_edit_dialog_changes(win, current_selected_row_id, new_text)`（4604），该函数已完成：按 row_id 定位行、比较文本、更新 `row.text`、刷新 `display_text`、提交 undo 快照、同步列表树节点、状态提示“未写回时间线”。
3. **事件接线**：

- `win.On.SubtitleTree.ItemClicked`（22345）：在现有逻辑（含 `is_preview_tree_edit_column_event` 弹 ✎ 窗）之后，若 `row` 有效则调用加载函数把文本送入编辑框。
- `win.On.SubtitleTree.ItemDoubleClicked`（22353）：保持不变，仍 `go_to_subtitle` 跳转。
- `win.On.EditSubtitleSaveBtn.Clicked`：读取 `EditSubtitleText` 内容 → 调用复用保存函数 → 刷新标签。
- `win.On.EditSubtitleRevertBtn.Clicked`：从 `row.text` 重新载入，丢弃未保存修改。
- 回车保存：为 `EditSubtitleText` 设 `Events={ReturnPressed=true}` 并绑定 `ReturnPressed`（若 Fusion 支持）；因多行字幕需 Enter 换行，**主路径使用「保存」按钮**，回车作为增强而非强制。

4. **选中态与刷新一致性**：在 `refresh_subtitles` 成功/失败及 `rebuild_tree_from_rows` 之后调用 `reset_subtitle_edit_box(win)`，清空编辑框与标签，避免停留在已失效的选中行。
5. **写回轨道**：无需改动。`win.On.UpdateBtn.Clicked`（22339）→ `update_timeline()`（14289）已遍历 `current_rows` 生成 SRT 并整轨重写目标字幕轨，天然包含编辑后的文本。

## 实现注意（防回归）

- 严禁任何顶层 `local`；`create_full_content` 内仅声明式构建，不引入变量。
- 新增控件 ID 必须全局唯一，避免与现有 ID 冲突（用 code-explorer 核对）。
- 改动后用 `_count_locals_scan.py` 复测，确保 MAIN-CHUNK local total = 0、无函数超过 200；并用 luaparser 校验语法。
- 保持“双击跳转”与“✎ 弹窗编辑”原有行为不变，仅做增量增强，不重构无关逻辑。

## 数据流

```mermaid
flowchart LR
    A[选中字幕行] --> B[加载文本到编辑框]
    B --> C[用户编辑文本]
    C --> D[点保存/回车]
    D --> E[save_preview_edit_dialog_changes 更新 current_rows 与列表]
    E --> F[点 更新到时间线]
    F --> G[update_timeline 整轨写回字幕轨]
```

## 目录结构

- `SubFix.lua` [MODIFY] 主脚本：在 `create_full_content()` 新增 `SubtitleEditArea` UI 块；新增 3 个全局函数（加载/保存/重置编辑框）；新增 2 个按钮事件与可选 ReturnPressed；在刷新/重建处调用重置；不改动其他逻辑、不改动部署副本以外的文件（部署副本在写回验证阶段同步）。

## Agent Extensions

### SubAgent

- **code-explorer**
- Purpose: 在约 6.5 万行的 SubFix.lua 中精确核对新增控件 ID 全局唯一性、确认 `create_full_content()` 中字幕列表与底部栏之间的插入点行号，以及 `save_preview_edit_dialog_changes`、`refresh_subtitles`、`rebuild_tree_from_rows` 的精确位置，避免误改或 ID 冲突。
- Expected outcome: 输出准确的插入行号与无冲突的控件 ID 清单，供编辑步骤直接使用。