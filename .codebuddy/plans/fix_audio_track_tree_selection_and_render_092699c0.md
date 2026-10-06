---
name: fix_audio_track_tree_selection_and_render
overview: 重新实现"生成选区字幕"面板中音频轨道树的选择语义与视觉样式：默认选中第 1 条；普通点击=单选互斥（点新行取消旧行）；按住 Ctrl/Shift 点击=多选追加/切换；选中行=橙底(#FF6A00)+白字，未选中行=白灰色字。同时修复"点击后不立即更新/卡顿"的问题（改用 window:RecalcLayout()+window:Update() 强制刷新），并排查上一轮"整棵树变灰白、完全不符要求"的渲染根因（逐项 BackgroundColor[] 是否实际生效 / 构建期是否报错）。
todos:
  - id: fix-tree-qss
    content: 修改 SUBFIX_ROOT_STYLESHEET：树未选中灰白字、选中橙底白字
    status: completed
  - id: set-selection-mode
    content: 设置 track_tree 为 ExtendedSelection 并默认选中首条轨道
    status: completed
    dependencies:
      - fix-tree-qss
  - id: rewire-selection
    content: 移除逐项上色与手动toggle，改读原生选中态收集轨道并清理无用代码
    status: completed
    dependencies:
      - set-selection-mode
  - id: deploy-verify
    content: 部署到 DaVinci 并验证橙底白字/灰字及单选+Ctrl/Shift多选
    status: completed
    dependencies:
      - rewire-selection
---

## 用户需求

在"生成选区字幕"面板的音频轨道树中修复选择样式与选择语义。

## 核心功能

- 视觉：未选中行显示白灰字；选中行显示橙色底（#FF6A00）+ 白色文字。
- 选择语义：打开面板默认选中第 1 条轨道；普通点击某行=单选互斥（自动取消其他行）；按住 Ctrl 或 Shift 点击=多选（追加/切换该行，保留其他）。
- 修复上一轮改动导致的面板渲染异常（整棵树发灰/发白、与要求不符），恢复稳定渲染。

## 边界与约束

- 仅修改现有核心 Lua 文件，不新增界面结构。
- 选中颜色需在本机 DaVinci Resolve + Fusion UIManager 环境实际可见。

## 技术栈

- 运行环境：DaVinci Resolve + Fusion UIManager（Lua 脚本）。
- 修改目标（单文件）：`f:\SubFix-main\SubFix-main\.subfix_support\subfix_generate_selection_core.lua`
（部署目标：`C:\Users\10840\AppData\Roaming\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\.subfix_support\subfix_generate_selection_core.lua`）

## 实现方案

### 总体策略

废弃上一轮"逐项 `BackgroundColor[]`/`TextColor[]` + 手动 toggle + 每次刷新清掉 `item.Selected`"的脆弱做法，改用 **Fusion Tree 原生选择机制 + QSS 样式**承载选中态。原生选择由 Qt 直接绘制，配合 `QTreeWidget::item:selected` 的 QSS 覆盖即可稳定得到"橙底白字"，且默认主题已证明选中高亮能渲染（上一轮用户看到的黄色即默认高亮），因此 QSS 覆盖可信。

### 关键技术决策

1. **选择语义用 `SelectionMode = "ExtendedSelection"`**：原生即"普通点击单选互斥、Ctrl/Shift 多选"，无需手写修饰键判定，零额外逻辑，与已确认需求完全一致。
2. **颜色用 QSS 而非逐项 `BackgroundColor`**：逐项 `BackgroundColor` 在本 Fusion 构建疑似不可靠渲染，且上一轮在刷新时强制 `item.Selected = false` 与 Qt 原生选择机制互相打架，是面板"整树灰白"的根因。改用 QSS 后不再触碰每项的选中属性，渲染稳定、无重绘循环（顺带解决"点击不立即更新"的观感）。
3. **默认选中与读取**：构建后设 `track_rows[1].item.Selected = true`（兜底 `track_tree:SetSelection(...)`）；确认时遍历 `track_tree:SelectedItems()` 或逐项读 `.Selected` 收集已选轨道，移除旧的 `row.checked` 维护。

### 性能与可靠性

- 选择变更交由 Qt 原生处理，无 Lua 全表重绘循环，`safe_refresh_tree_widget` 仅做 `tree:Update()`/`Repaint()` + 窗口级 `selection_window:RecalcLayout()`/`Update()` 兜底，开销极小。
- 所有属性写入均包 `pcall`，避免单点失败中断面板构建。

### 兜底方案（验证用）

若重启后选中行仍不显示橙色（说明本环境 `::item:selected` QSS 也不生效），则退化为"在 `ItemClicked` 读取 `item.Selected` 后同步逐项 `TextColor`/`BackgroundColor`，且绝不写 `item.Selected`"，颜色逻辑与上一轮相同但不再与原生选择冲突。

## 实现要点（防回归）

- 修改窗口级 `SUBFIX_ROOT_STYLESHEET`（约 line 155-158）：`QTreeWidget` 基础 `color` 改为灰白 `#9AA7B4`；`QTreeWidget::item:selected` 改为 `background-color:#FF6A00; color:#FFFFFF`；`::item:hover` 保留深色。
- 在 `track_tree` 初始化处（约 line 2426-2431）加 `pcall(function() track_tree.SelectionMode = "ExtendedSelection" end)`。
- 删除：常量 `SUBFIX_TRACK_CHECKED_TEXT/UNCHECKED_TEXT/CHECKED_BG/UNCHECKED_BG`（line 144-147）、`set_tree_item_background_color`（331-333）、`apply_audio_track_row_style`（335-345）、`refresh_audio_track_rows`（3116-3124）及其调用（3124）、`set_track_checked`（3136-3139）。
- 简化 `ItemClicked`（3142-3151）：不再手动 toggle，交由原生选择；仅保留必要的信息标签刷新。
- 改写 `collect_checked_audio_sources`（3126-3134）：按 `.Selected`/`SelectedItems()` 收集，保持对外接口 `selected_audio_sources` 不变。

## 目录结构（仅修改单文件）

```
.subfix_support/
└── subfix_generate_selection_core.lua   # [MODIFY] 音频轨道树样式与选择逻辑：
    #  - SUBFIX_ROOT_STYLESHEET：树文字改灰白、选中态改橙底白字（line ~149-160）
    #  - track_tree 初始化：新增 SelectionMode=ExtendedSelection（line ~2426）
    #  - 删除逐项上色常量/helper（line 144-147, 331-345）
    #  - 删除 refresh_audio_track_rows/set_track_checked（line 3116-3139）
    #  - ItemClicked 改为依赖原生选择（line 3142-3151）
    #  - collect_checked_audio_sources 改读 .Selected（line 3126-3134）
    #  - 构建后默认选中首条 + 窗口级刷新兜底
```

## 关键代码结构（节选）

```
-- QSS（窗口级 SUBFIX_ROOT_STYLESHEET 中 QTreeWidget 相关节选）
QTreeWidget { background-color: #0F141A; color: #9AA7B4; border: 1px solid #34506b; border-radius: 5px; font-size: 14px; }
QTreeWidget::item:selected { background-color: #FF6A00; color: #FFFFFF; }
QTreeWidget::item:hover { background-color: #243240; }
QTreeWidget::item { padding: 6px; }

-- 默认选中首条并兜底刷新
pcall(function() track_rows[1].item.Selected = true end)
pcall(function() track_tree:SetSelection(track_rows[1].item) end)
safe_refresh_tree_widget(track_tree)

-- 确认时收集已选轨道
local function collect_checked_audio_sources()
    local checked_sources = {}
    local selected = {}
    pcall(function() selected = track_tree:SelectedItems() or {} end)
    if #selected == 0 then
        for _, row in ipairs(track_rows) do
            if row.item and row.item.Selected then selected[#selected + 1] = row.item end
        end
    end
    for _, item in ipairs(selected) do
        local idx = item_map[item]
        if idx and track_rows[idx] then checked_sources[#checked_sources + 1] = track_rows[idx].source end
    end
    return checked_sources
end
```