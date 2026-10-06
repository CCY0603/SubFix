---
name: style_main_window_like_selection
overview: 把“改个字幕”主窗口（SubFix.lua 的 MainRoot）的 UI 统一成“生成字幕选区”那套深色主题 + 橙色强调：套用深色根样式表（背景#1A212B、文字#D6DDE7、输入框/下拉/字幕树/Tab 标签深色、14px），并把 5 个主操作按钮（刷新字幕、保存修改、更新时间线、开始 AI 处理、执行批量替换）设为橙底白字；其余按钮保持 Resolve 原生外观。
todos:
  - id: add-style-constants
    content: 在 SubFix.lua 用 or 守卫新增 SUBFIX_BTN_ACCENT / SUBFIX_MAIN_ROOT_STYLESHEET / SUBFIX_DIALOG_FONT 全局常量
    status: completed
  - id: apply-root-style
    content: 给 MainRoot 容器加 Font(14px) 与 StyleSheet 深色根样式表
    status: completed
    dependencies:
      - add-style-constants
  - id: style-accent-buttons
    content: 给刷新字幕/执行批量替换/开始AI处理/保存修改/更新时间线 5 个按钮加 SUBFIX_BTN_ACCENT
    status: completed
    dependencies:
      - add-style-constants
  - id: verify-no-override
    content: 检查这 5 个按钮是否被运行时 SetAttrs 覆盖并协调
    status: completed
    dependencies:
      - style-accent-buttons
  - id: deploy-verify
    content: 备份并部署 SubFix.lua 到 DaVinci 插件目录，重启验证深色主题与橙底按钮
    status: completed
    dependencies:
      - verify-no-override
---

## 用户需求

把“改个字幕”主窗口（SubFix.lua 的 `MainRoot`）的 UI 统一成“生成字幕选区”那套深色 + 橙色强调风格，使其视觉语言与主窗口一致。

## 核心功能

- 整窗深色主题：窗口背景 `#1A212B`、文字 `#D6DDE7`、输入框/下拉框/字幕树/Tab 标签统一深色、14px 字体。
- 5 个主操作按钮（刷新字幕、保存修改、更新时间线、开始 AI 处理、执行批量替换）改为橙底 `#FF6A00` + 白字 + 加粗的强调样式（`SUBFIX_BTN_ACCENT`）。
- 其余按钮（检查更新、还原、撤回、清空、精修 4 按钮、配置、强制退出、微调箭头等）保持 Resolve 原生外观，不套任何样式。
- 字幕树选中行、TabBar 选中页均显示橙底白字，与“生成选区”窗口观感保持统一。

## 技术栈

- 运行环境：DaVinci Resolve + Fusion UIManager（Lua 脚本）。
- 修改目标（单文件）：`f:\SubFix-main\SubFix-main\SubFix.lua`
- 视觉语言复用既有 `subfix_generate_selection_core.lua` 的 `SUBFIX_ROOT_STYLESHEET / SUBFIX_BTN_ACCENT / DIALOG_FONT` 体系（颜色、圆角、悬停态完全一致）。

## 实现方案

### 关键技术决策

1. **样式常量必须在本文件内新增全局定义（用 `or` 守卫）**：核心模块的 `SUBFIX_ROOT_STYLESHEET/SUBFIX_BTN_ACCENT/DIALOG_FONT` 是 `local`（129–168 行），主脚本通过 `loadfile` 把核心当独立 chunk 执行（13350–13367），取不到这些常量。因此要在 `SubFix.lua` 侧用 `SUBFIX_xxx = SUBFIX_xxx or "..."` 新增全局常量，避免重复定义又能与既有 `SUBFIX_BTN_BASE/SELECTED/TOGGLE`（466–474）共存。
2. **根样式表不含通用 `QPushButton{}` 规则**：由于用户要求其余按钮保持原生，若写一条全局 `QPushButton{}` 会把检查更新/还原/撤回等全染成深灰底，违背需求。深色背景只落在 `QWidget/QLabel/QLineEdit/QTextEdit/QComboBox/QTreeWidget/QHeaderView/QTabBar` 上；Fusion 风格下通用 `QWidget{background}` 不会覆盖 `QPushButton` 自带背景，故原生按钮仍保持默认外观（实施后目视确认）。
3. **5 个主按钮单独挂 `SUBFIX_BTN_ACCENT`**：橙底白字、加粗、5px 圆角、橙色悬停态，与生成选区窗口“生成”按钮一致。

### 性能与可靠性

- 纯 QSS 字符串 + `StyleSheet` 属性，无 Lua 运行时重绘循环，开销为零。
- 所有常量写入走 `or` 守卫，重复加载脚本不会覆盖既有定义；按钮 `StyleSheet` 在声明时一次性赋值，不触发运行时刷新。
- 字体：优先在 `MainRoot` 赋值 `Font = ui:Font{PixelSize = 14}`（与核心 `DIALOG_FONT` 一致）；若该作用域 `ui` 受限，则退化到根样式表内各控件的 `font-size: 14px`（核心表已含，足够覆盖）。

### 实现要点（防回归）

- 新增 `SUBFIX_BTN_ACCENT`（14px 橙底白字，对齐核心 157 行）。
- 新增 `SUBFIX_MAIN_ROOT_STYLESHEET`：在核心根表基础上**移除通用 `QPushButton` 规则**、补 `QHeaderView::section` 与 `QTabBar::tab(:selected)` 规则，使字幕树表头与 Tab 选中页也呈橙底白字。
- 给 `MainRoot`（18340）加 `Font` + `StyleSheet = SUBFIX_MAIN_ROOT_STYLESHEET`。
- 给 `RefreshBtn`(18377)、`BatchReplaceBtn`(18453)、`AIFixBtn`(18479)、`EditSubtitleSaveBtn`(18521)、`UpdateBtn`(18585) 各加 `StyleSheet = SUBFIX_BTN_ACCENT`。
- 通读 `create_full_content` 及后续事件绑定，搜索这 5 个按钮是否被 `SetAttrs({StyleSheet=...})` 运行时覆盖；若有则协调（保留橙底）。

### 兜底

若 `QWidget{background:#1A212B}` 在 Fusion 下意外把原生按钮也染深（通常不会），则将背景规则收窄为 `#MainRoot` 或具体容器类，仅 5 个主按钮显式橙色，其余严格原生。

## 目录结构（仅修改单文件）

```
SubFix.lua                                       # [MODIFY]
  ├─ 全局样式常量区（约 466–474 附近）            # 新增 SUBFIX_BTN_ACCENT / SUBFIX_MAIN_ROOT_STYLESHEET / SUBFIX_DIALOG_FONT，用 or 守卫
  ├─ create_full_content() 根容器 MainRoot(18339) # [MODIFY] 增加 Font(14px) + StyleSheet = SUBFIX_MAIN_ROOT_STYLESHEET
  ├─ RefreshBtn(18377) / BatchReplaceBtn(18453)   # [MODIFY] 增加 StyleSheet = SUBFIX_BTN_ACCENT
  ├─ AIFixBtn(18479) / EditSubtitleSaveBtn(18521) # [MODIFY] 增加 StyleSheet = SUBFIX_BTN_ACCENT
  └─ UpdateBtn(18585)                            # [MODIFY] 增加 StyleSheet = SUBFIX_BTN_ACCENT
```

（其余 12 个按钮及所有输入框/下拉/树/Tab 不改动，仅随根样式表自动统一深色。）

## 关键代码结构（节选）

```css
-- 新增主操作按钮样式（对齐核心 157 行）
SUBFIX_BTN_ACCENT = SUBFIX_BTN_ACCENT or
  "QPushButton{font-size:14px;background-color:#FF6A00;color:#FFFFFF;font-weight:bold;" ..
  "border:1px solid #FF6A00;border-radius:5px;padding:6px 12px;}" ..
  "QPushButton:hover{background-color:#CC5600;}"

-- 新增主窗口根样式表（无通用 QPushButton 规则，补 Header/Tab）
SUBFIX_MAIN_ROOT_STYLESHEET = SUBFIX_MAIN_ROOT_STYLESHEET or [[
QWidget { font-size: 14px; color: #D6DDE7; background-color: #1A212B; }
QLabel { font-size: 14px; color: #D6DDE7; background-color: transparent; }
QLineEdit, QTextEdit, QComboBox { background-color: #0F141A; color: #D6DDE7; border: 1px solid #34506b; border-radius: 5px; padding: 6px; font-size: 14px; }
QTreeWidget { background-color: #0F141A; color: #D6DDE7; border: 1px solid #34506b; border-radius: 5px; font-size: 14px; selection-background-color: #FF6A00; selection-color: #FFFFFF; }
QTreeWidget::item:selected { background-color: #FF6A00; color: #FFFFFF; }
QTreeWidget::item:!selected:hover { background-color: #243240; }
QTreeWidget::item { padding: 6px; }
QHeaderView::section { background-color: #243240; color: #D6DDE7; border: 1px solid #34506b; padding: 4px; }
QTabBar::tab { background-color: #243240; color: #D6DDE7; border: 1px solid #34506b; border-bottom: none; padding: 6px 14px; font-size: 14px; }
QTabBar::tab:selected { background-color: #FF6A00; color: #FFFFFF; font-weight: bold; }
QTabBar::tab:!selected:hover { background-color: #2c3e52; }
QComboBox QAbstractItemView { background-color: #0F141A; color: #D6DDE7; selection-background-color: #FF6A00; }
]]
```