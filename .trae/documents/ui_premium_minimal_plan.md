# SubFix UI「高级·简约」改版实施计划

> 技术约束：SubFix 是 DaVinci Resolve Fusion 原生窗口（Lua + UIManager 的 QSS 子集），**不是网页**。frontend-design 的设计原则（层级、克制、留白、字体）直接适用，落地方式是改 [SubFix.lua](file:///f:/SubFix/SubFix.lua) 中的设计令牌与静态 QSS；不引入任何 Web 技术。

## 一、现状评价（基于三张截图：①选区识别 / ②精修工具 / ③AI 工作台）

### 做得对的
- 暗色方向正确，与 Resolve 宿主环境协调；三步工作流（识别→精修→AI→写回）清晰。
- 样式令牌已集中在 [SUBFIX_DESIGN L721-L752](file:///f:/SubFix/SubFix.lua#L721-L752)，按钮 QSS 有分级常量，且做过 WCAG 对比度把关——改版可以"令牌驱动"，不用动布局。

### 显得"不高级"的 9 个具体问题
1. **橙色过载（最大问题）**：截图①同时有 5 处实心橙块——当前 Tab、选中的 A1 音轨、"标准≤25字"、"Qwen（本地）"、"开始生成"，底部还永远压着一条大橙"更新时间线"。处处是重点 = 没有重点。
2. **底色不纯**：`#1A212B`（蓝灰页面底）、`#243240`（蓝灰容器）、`#282828`（纯灰按钮）三种色温的暗灰混用，边框 `#34506b` 明显泛蓝，整体观感"脏"。
3. **硬边框方块满天飞**：每个按钮、输入框、区块都有 1px 可见描边；高级感来自明度分层，不是描边。
4. **Tab 像三个大按钮**：满宽实心橙块体量过重，Tab 的本质只是导航。
5. **"选项"伪装成"动作"**：分段选择器（标准/短视频、Qwen/豆包）选中态与"开始生成"同为满橙加粗，无法一眼区分哪是设置、哪是行动。
6. **字幕树选中太刺眼**：整行满橙底+深色字；且每行挂一个 ✎ 铅笔图标（[L4944](file:///f:/SubFix/SubFix.lua#L4944)），55 行 = 55 个噪音图标。
7. **底部"更新时间线"常年满橙**（[L16373](file:///f:/SubFix/SubFix.lua#L16373)），无论有没有待写回的改动，一直在喊"点我"。
8. **Emoji 当图标**：📁📝⚙️✂⏻（[L16070/L16194/L16313/L16330/L16373](file:///f:/SubFix/SubFix.lua#L16070)）在 Windows 上彩色字形与深色 UI 气质冲突，是典型的"工具感廉价"来源。
9. **排版没有节奏**：未设字体族（吃 Fusion 默认），12/13/14/16 字号梯级定义了但区块标题没真正拉开层级；4px 起步的间距偏碎，元信息（"按住 Ctrl/Shift…"、In/Out 数值）与正文灰度区分不够。

## 二、设计方向：Quiet Pro（安静的专业工具）

参照 Resolve/Final Cut/Linear 这类专业工具的共同语言：

- **中性炭灰阶**：去掉蓝调，统一色温；层级靠 3 级底色明度差，描边降到几乎不可见。
- **一种强调色，三处使用**：橙只给——每个视图唯一的主行动按钮、当前 Tab 的指示、自动跟随的状态点。所有"选中/checked"改用中性提亮面 + 白字。
- **控件幽灵化**：默认按钮无底无边或极弱边，hover 才浮现浅色面。
- **8px 留白节奏 + 小号弱化元信息**；字体统一为 Microsoft YaHei UI。
- **去掉所有 emoji 图标**，按钮只留文字。

建议新令牌（覆盖 [SUBFIX_DESIGN.color L722-L746](file:///f:/SubFix/SubFix.lua#L722-L746)）：

| 令牌 | 旧值 | 新值 | 用途 |
|---|---|---|---|
| page | #1A212B | #16181C | 窗口底 |
| container | #243240 | #1F2229 | 面板/默认按钮面 |
| surface_hover | —（新增 #2A2E37） | hover 面 |
| selected | —（新增 #2E333D） | 树/音轨/分段控件选中面 |
| input | #0F141A | #12141A | 输入框/树底 |
| border | #34506b | #333842 | 弱描边（降存在感） |
| text | #D6DDE7 | #E6E9EF | 主文字 |
| text_muted | —（新增 #9099A8） | 元信息/提示 |
| accent | #FF6A00 | #FF6A00（保留品牌橙，仅减少出场） |
| on_accent | #1A212B | #14161A | 橙底字 |

字体：`font.family = "Microsoft YaHei UI"`（QSS font-family，回退系统默认）；圆角 5→6；保留语义色 success/warning/danger/info 不动。

## 三、改动清单（全部集中在样式层 + 按钮文案，不动功能逻辑与布局结构）

### 阶段 0：HTML 视觉稿确认方向（先做，零风险）
用 frontend-design 能力产出一个**自包含 HTML 静态稿**（放在 `f:\SubFix\.trae\documents\ui_redesign_preview.html`，浏览器打开），按新色板/新控件规范 1:1 画出三个 Tab 的主窗口（含安静的 Tab、中性选中树、唯一橙按钮、去 emoji 底栏）。用户确认视觉方向后再动 Lua。

### 阶段 1：Lua 样式落地
1. **令牌表** [L721-L752](file:///f:/SubFix/SubFix.lua#L721-L752)：替换为新色板，新增 surface_hover/selected/text_muted/family。
2. **按钮 QSS** [L759-L810](file:///f:/SubFix/SubFix.lua#L759-L810)：
   - BASE → ghost 风：透明/container 底、弱边、hover 浮现 surface_hover；
   - SELECTED / TOGGLE:checked → **中性 selected 面 + 白字**（去橙），用于音轨、标准/短视频、Qwen/豆包；
   - ACCENT 橙样式保留但收紧使用点；
   - 新增 `SUBFIX_BTN_ACCENT_OUTLINE`（透明底+橙边+橙字）给"执行批量替换"这类次强动作。
3. **根样式表** [L815-L838](file:///f:/SubFix/SubFix.lua#L815-L838)：
   - 全局加 font-family；
   - `QTreeWidget::item:selected` 满橙 → 中性 selected 面、文字保持浅色；试验 `border-left: 3px solid accent` 做左侧指示条（Fusion QSS 不支持则回退纯中性面）；
   - `QTabBar::tab` → 透明底、无边框、padding 收敛（约 8px 18px）；`:selected` 不铺橙，改为橙字 + `border-bottom: 2px solid accent`；未选中 muted 灰字、hover 浅色面；
   - QComboBox 弹层、QHeaderView 选中色同步去橙改中性；输入框弱描边。
4. **按钮强调点重新分配**：
   - "更新时间线" [L16373](file:///f:/SubFix/SubFix.lua#L16373)：ACCENT → SECONDARY（视觉降权；"有改动才变橙"需要脏状态跟踪，列为阶段 2 可选）；
   - "执行批量替换" → ACCENT_OUTLINE；
   - 每视图只保留一个满橙主按钮：①开始生成、②保存修改（编辑区）、③开始 AI 处理；
   - 顶部 刷新字幕/检查更新/导出/退出 维持次级 ghost。
5. **去 emoji**（纯文本替换，6 处）："⏻ 退出"→"退出"、"⚙️ 配置"→"配置"、"✂ 拆分"→"拆分"、"📁"→"备份"（按钮带 ToolTip 不变）、"📝 更新时间线"→"更新时间线"；树标记列 `"  ✎"` [L4944](file:///f:/SubFix/SubFix.lua#L4944) 清空，标记列宽同步收窄（[L4929](file:///f:/SubFix/SubFix.lua#L4929) 附近）。
6. **embedded 生成面板**（选区识别页，[.subfix_support/subfix_generate_selection_core.lua](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua)）：同 VM 共用宿主令牌表，色板与音轨选中色自动继承；仅检查其 standalone 兜底常量无需改（embedded 不走兜底）。
7. 元信息文案标签（"按住 Ctrl/Shift…"、"In/Out…"、自动跟随行）统一套 text_muted 色：如代码已有专用 label 辅助函数则改一处；否则在 QSS 用 `QLabel#ObjectName` 精准命中（阶段 0 稿里先定样式，落地时按成本决定）。

### 阶段 2（可选，本次批准后可一起做或后续再说）
- "更新时间线"脏状态联动：有待写回改动时才变橙，需要在字幕编辑/AI 写改处挂 dirty 标记；
- 区块小标题（选区识别/生成选项）统一为 12px muted + 字间距，建立第二级排版节奏。

## 四、不做的事
- 不改窗口尺寸（1240×760）、布局结构、Spacing、任何功能逻辑与树选中机制（`node.Selected` 那套完全不碰）；
- 不做无边框窗口/自绘标题栏（回归风险大）；
- 不动语义色、不动弹窗功能，只让其继承新令牌。

## 五、验证
1. 阶段 0：浏览器目检 HTML 稿，用户确认视觉方向。
2. Lua 落地后：TEMP 下 lua5.1.dll `luaL_loadfilex` 语法校验 → `install_subfix.ps1` 部署 → SHA256 MATCH → GetDiagnostics 0。
3. 真机逐页目检：三个 Tab、音轨多选、分段控件、字幕树选中/hover/滚动、编辑区、配置弹窗、备份下拉、AI 跑批进度、自动跟随橙指示；截图与改版前对比。
4. 功能回归（只验不致坏）：识别生成、精修批量替换、AI 处理、保存修改、更新时间线、跟随定位各点一次。

## 六、风险
- **Fusion QSS 子集不确定性**：`rgba()`、`::item border-left`、`font-family` 三项真机首次加载即验证；rgba 不支持就用实色 `#333842/#2A2E37`，左指示条不支持就回退纯中性选中面，字体不生效则吃系统默认（YaHei 仍是实际默认字形之一，损失可接受）。
- **QSS 运行时不可改写**（[L771 注释](file:///f:/SubFix/SubFix.lua#L771) 已证实）：所有样式静态定义，无动态换肤代码。
- 去 emoji 后纯文字按钮宽度变化：布局用 Weight 分配，预期无溢出；真机目检兜底。
