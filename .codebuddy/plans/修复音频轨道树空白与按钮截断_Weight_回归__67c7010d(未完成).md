---
name: 修复音频轨道树空白与按钮截断（Weight 回归）
overview: 通过对比部署目录中的 9/22 历史备份，定位到回归根因：音频轨道 Tree 被从可用版的 Weight=1 改成了 Weight=0 并加了 MaximumSize，导致内容溢出固定高度窗口时树的行区不渲染（空白）且底部按钮被裁。修正：恢复 Tree 为 Weight=1、去掉 MaximumSize、最小高度设为 120，并把窗口高度提到 560。橙色配色改动保持不变。
todos:
  - id: fix-tree-and-height
    content: 修改核心文件：Tree 恢复 Weight=1、MinimumSize=120、去掉 MaximumSize；窗口高度改为 560
    status: completed
  - id: redeploy-core
    content: 备份并重新部署核心文件到 Resolve 插件目录
    status: completed
    dependencies:
      - fix-tree-and-height
  - id: verify-dialog
    content: 重启达芬奇，验证轨道行与生成/取消按钮默认完整显示、配色为橙
    status: in_progress
    dependencies:
      - redeploy-core
---

## 产品概述

修复 SubFix（达芬奇/Fusion 字幕插件）"生成选区字幕"对话框中两个仍未解决的问题，并保留上一轮已完成的橙色配色：

1. 音频轨道列表为空：对话框内用于选择音频轨道的树（Tree）不显示任何轨道行（对照旧版应显示 `A1 音频 1`、`A2 音频 2`）。
2. 底部"取消"按钮被截断：窗口默认尺寸下按钮底部被裁掉，必须拉伸窗口才完整。

## 核心特性

- 音频轨道树在对话框打开时即正常渲染出各条轨道行（含勾选标记与轨道名），无需手动拉伸窗口。
- "生成/取消"按钮默认尺寸即完整可见，不被裁切。
- 保留上一阶段已生效的达芬奇橙 `#FF6A00` 配色（强调按钮、树选中、勾选标记等）。
- 重新部署核心文件并重启达芬奇后生效。

## 回归根因（已核实）

通过对比部署目录中的历史备份发现：9/22 三份可用旧版中，音频轨道树定义均为 `Weight = 1`（无 `MaximumSize`）；而回归版将其改为 `Weight = 0` 并新增 `MaximumSize`。`Weight = 0` 使该树在固定布局中只获得最小高度、行区不再渲染，表现为"空树"；叠加对话框新增了"文稿匹配"选项行与状态标签导致内容总高超出窗口，底部按钮被裁。

## 技术栈

- 运行环境：DaVinci Resolve / Fusion 内置 LuaJIT；UI 基于 Fusion UIManager（Qt）。`Weight` 对应布局伸缩因子，`MinimumSize`/`MaximumSize` 约束控件尺寸。
- 静态分析：Lua Language Server（sumneko），沿用工作区根 `.luarc.json`（运行时不变量）。
- 部署：将核心文件复制到 `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\.subfix_support\`，重启 Resolve 生效。

## 实现方案

目标文件：`f:\SubFix-main\SubFix-main\.subfix_support\subfix_generate_selection_core.lua`，函数 `show_audio_track_selection_dialog`（第 2259 行起）。

### 改动一：恢复音频轨道树布局（修复空树）

第 2327–2333 行 Tree 定义：

- `Weight = 0` → `Weight = 1`（恢复为可用旧版写法，使树参与伸缩、拿到足够高度并渲染行）
- `MinimumSize = {0, 80}` → `MinimumSize = {0, 120}`（保证至少容纳数条轨道）
- 删除 `MaximumSize = {0, 160},` 行（旧版无此约束；避免压缩行区）

树填充逻辑（第 3066–3080 行的 `NewItem` + `set_tree_item_text` + `AddTopLevelItem` + `safe_refresh_tree_widget`）与旧版一致，无需改动。

### 改动二：调高窗口高度（消除按钮截断）

第 2319 行 Geometry：`centered_geometry({460, 250, 440, 480})` → `centered_geometry({460, 250, 440, 560})`。
依据：480 时"取消"仍被裁，说明内容约 500px；恢复 Weight=1 且树最小高度提至 120 后所需高度约 540，取 560 留余量。`centered_geometry` 第 4 位为高度（第 237–252 行）。

### 改动三（保留，不撤销）

上一阶段的橙色配色改动保持：`SUBFIX_ACCENT_COLOR`、`SUBFIX_CHECKED_COLOR`（均 `{255,106,0}`）、`SUBFIX_ROOT_STYLESHEET` 中选中/hover 描边、`SUBFIX_BTN_ACCENT` 均为 `#FF6A00`。

### 部署与验证

- 备份当前部署版，再复制工作区核心文件到上述插件目录。
- 重启达芬奇使改动生效（脚本被缓存）。
- 验收：对话框默认尺寸下即显示音频轨道行与完整"生成/取消"按钮，无需拉伸；橙色配色保持。

## 实现细节

- 改动集中在同一文件的 Tree 定义与窗口几何两处，均为声明式属性，无业务逻辑变更、无控件 ID/事件变更，影响面可控。
- 若恢复 `Weight = 1` 后树仍为空（低概率），回退方案为在树填充处临时打印 `#audio_sources` 与 `NewItem` 结果以进一步定位；默认按现有证据先做上述最小改动。
- 高度 560 为保证不裁切的取值，若视觉过高可在确认可用后微调（如 540）。

## 目录结构

```
f:\SubFix-main\SubFix-main\
└── .subfix_support\
    └── subfix_generate_selection_core.lua  # [MODIFY] 第2319行：窗口高度 480→560；第2327-2333行：Tree Weight 0→1、MinimumSize→120、删除 MaximumSize
```

（部署目标：`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\.subfix_support\subfix_generate_selection_core.lua`，仅同步该文件）