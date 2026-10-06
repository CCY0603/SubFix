---
name: subfix-tree-color-range-fix
overview: 修复 Fusion 逐项颜色表取值区间错误（0–255 被钳位成 1.0 导致纯黄/纯白），使音频轨道树达到"未选中=灰字+深底、选中=橙底+白字"，并同步修正同一根因导致的待审/回退列表颜色失真。
todos:
  - id: fix-core-color-range
    content: 核心文件新增 ui_color 换算函数并改写全部颜色常量为 0–1 浮点，轨道未选中=深底灰字、选中=橙底白字
    status: completed
  - id: fix-subfix-color-range
    content: SubFix.lua 新增同源 ui_color 换算函数，改写待审/回退列表 6 组 0–255 颜色表
    status: completed
  - id: sweep-color-tables
    content: 用 [subagent:code-explorer] 扫描全仓残留 0–255 逐项颜色表并统一修正
    status: completed
    dependencies:
      - fix-core-color-range
      - fix-subfix-color-range
  - id: deploy-and-verify
    content: 备份并部署两个文件到 DaVinci 插件目录，核对新标记并提示重启验证
    status: completed
    dependencies:
      - sweep-color-tables
---

## 产品概述

修复 SubFix（达芬奇/Fusion 字幕插件）"生成选区字幕"面板中音频轨道树的选中态显示错误：当前未勾选行出现纯黄/纯白底、文字不可见，与需求完全不符。

## 核心特性

- 音频轨道树（"选择用于识别的音频轨道"）恢复正确的选择语言：
- 未勾选：深底 + 灰色文字（无黄色/白色底块）
- 已勾选：橙色底 + 白色文字
- 选中态必须由"勾选状态"决定，不随点击焦点漂移；点击任一轨道在两种状态间稳定切换。
- 同源问题一并修正：待审/回退列表（含列标题与状态列）颜色同样因同一原因失真，恢复为"选中=橙、未选中=浅灰"的正常观感。
- 不改动任何业务逻辑与勾选行为，仅修正颜色取值。

## 技术栈

- 运行环境：DaVinci Resolve / Fusion 内置 LuaJIT；UI 基于 Fusion UIManager（Qt 控件）。
- 颜色机制：QSS 中的六位十六进制色（如按钮 `#FF6A00`）渲染正常；但**逐个 TreeItem 的颜色属性 `TextColor[]` / `BackgroundColor[]` 取值必须是 0–1 浮点**，传入 0–255 会被钳位到 1.0。
- 部署：源文件覆盖到 `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\` 下的对应文件，重启 Resolve 生效。

## 实现方案

### 根因（已用截图证据确认）

逐项颜色表按 0–255 书写，超范围值被钳位到 1.0：

- 选中色 `{255,106,0}` → `(1,1,0)` = 纯黄 `#FFFF00`
- 未选中底 `{15,20,26}` → `(1,1,1)` = 纯白 `#FFFFFF`
- 未选中文字 `{154,167,180}` → `(1,1,1)` = 纯白（白底白字不可见）

三张截图与该结论完全一致（黄底、白底、白字不可见）。

### 修复策略

不改变架构与控件承载方式，只做"取值域"修正，并用统一换算函数杜绝复发：

1. 在两个文件中各引入一个颜色换算工具，把 0–255 输入转换为 0–1 浮点输出（DRY，防止再次写错）：

```
-- Fusion UIManager 逐项颜色为 0–1 浮点；0–255 会被钳位成 1.0（纯黄/纯白）
local function ui_color(r, g, b, a)
return {R = r / 255, G = g / 255, B = b / 255, A = (a or 255) / 255}
end
```

2. 核心文件：全部颜色常量改用 `ui_color(...)` 定义，得到目标观感：

- 轨道未选中底 `#0F141A`、未选中字 `#9AA7B4`
- 轨道选中底 `#FF6A00`、选中字 `#FFFFFF`
- 通用文字 `#D6DDE7`、强调/选中 `#FF6A00`

3. SubFix.lua：待审/回退列表共 6 组内联颜色表改用同一换算函数（`#FF6A00` / `#D2D2D2` / `#D2DCEB` / `#AAAAAA` / `#4678AA` / `#E6B4AA` / `#A0E6B4`），使"选中橙、未选中浅灰"恢复本来颜色。
4. 保留现有 `apply_audio_track_row_style()`、`refresh_audio_track_rows()` 与 `row.item.Selected = false` 清焦点高亮逻辑（已确认逐项底色可覆盖 Qt 原生高亮，清焦点是额外保险），无需重构。

### 关键决策与取舍

- 选择"修数值 + 抽公共换算函数"而非改控件：改动面最小、零业务风险，且未来新增颜色不会再踩同一坑。
- 保持 A 通道为 1.0（完全不透明），避免半透明底色与树背景叠加产生色偏。
- 保留 `pcall` 包裹写法，沿用既有容错模式；颜色表构造为加载期常量，无运行时性能开销。

## 依赖与风险

- 该结论基于截图反推 + 现有代码行为，未在 Resolve 内实测；若个别属性仍不接受表格式颜色，回退路径为：轨道勾选态改用"文字符号（如 ●/○）+ 通用文字色"表达（此时保留 `TextColor[]` 即可）。先整批改完再统一验证。

## 目录结构

```
f:\SubFix-main\SubFix-main\
├── .subfix_support\
│   └── subfix_generate_selection_core.lua  # [MODIFY] 新增 ui_color 换算函数；改写第 133–142 行全部颜色常量为 0–1 浮点（轨道未选中=深底#0F141A+灰字#9AA7B4，选中=橙底#FF6A00+白字#FFFFFF）；填充/刷新/点击逻辑不动
└── SubFix.lua                              # [MODIFY] 在全局常量区（约 457 行附近）新增同源 ui_color 换算函数；改写第 4495–4498、5521–5523、5542–5544、5556、5565、5697–5699、5712–5714 行共 6 组 0–255 颜色表为 0–1 浮点
```

部署目标：

- `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\.subfix_support\subfix_generate_selection_core.lua`
- `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\SubFix\SubFix.lua`

## 关键代码结构

```
-- 颜色换算工具（两文件同源，分别定义在各自作用域内）
local function ui_color(r, g, b, a)
    return {R = r / 255, G = g / 255, B = b / 255, A = (a or 255) / 255}
end
-- 核心文件示例
local SUBFIX_TRACK_CHECKED_BG    = ui_color(255, 106, 0)    -- #FF6A00 橙底
local SUBFIX_TRACK_UNCHECKED_BG  = ui_color(15, 20, 26)     -- #0F141A 深底
local SUBFIX_TRACK_CHECKED_TEXT  = ui_color(255, 255, 255)  -- #FFFFFF 白字
local SUBFIX_TRACK_UNCHECKED_TEXT= ui_color(154, 167, 180)  -- #9AA7B4 灰字
```

## 实现要点

- 先备份两份目标文件（`*.bak_时间戳`）再覆盖部署，便于回退。
- 部署后用内容检索核对新标记（`ui_color` 与浮点颜色）确已生效。
- 提示用户重启 Resolve 后验证：未勾选行=深底灰字；勾选行=橙底白字；点击切换稳定；待审/回退列表选中项为橙色。
- 全仓再扫一遍残留的 0–255 逐项颜色表，确保无遗漏（防止同类问题在其他面板复现）。

## Agent Extensions

### SubAgent

- **code-explorer**
- Purpose: 全仓扫描所有 `TextColor[]` / `BackgroundColor[]` 逐项颜色赋值点，确认是否仍有按 0–255 书写的颜色表（含未在本次对话中出现的文件与位置）
- Expected outcome: 输出完整的残留 0–255 颜色表清单（文件路径 + 行号 + 原值），确保本轮回值域修正零遗漏