# SubFix 界面专业设计评审与改版计划

> 评审框架：uicraft（视觉层级、排版、配色与对比度、间距系统、交互状态、认知负荷、可用性启发式、可访问性）。
> 评审对象：达芬奇 Fusion UIManager 原生 Lua 界面（非网页，QSS 为 Qt 子集；无浏览器响应式/Core Web Vitals 概念）。
> 证据来源：[SubFix.lua](file:///f:/SubFix/SubFix.lua) 与 [subfix_generate_selection_core.lua](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua) 的布局与样式静态审查。
> 限制：本机无法启动 Resolve 截图，所有视觉结论由 QSS/布局代码推导；实施前必须在真机截图建立 baseline。

## 一、仓库调研结论（评审发现）

### 1. 做得好的部分（事实）

- 配色克制且有三层深色层级：页面底 `#1A212B`、容器 `#243240`、输入/树 `#0F141A`，单一强调橙 `#FF6A00`；无渐变/玻璃/发光等廉价装饰。
- 正文对比度优秀：`#D6DDE7` on `#1A212B` ≈ **11.8:1**；树字 `#C2C7D0` on `#0F141A` ≈ **11.0:1**（均超 AAA 7:1）。
- 选中态用 Qt `:checked` 伪状态驱动（[SubFix.lua L678-683](file:///f:/SubFix/SubFix.lua#L678-L683)），规避了 Fusion 运行时改 StyleSheet 不生效的坑。
- 窗口居中 + 小屏钳制（Windows FFI GetSystemMetrics / macOS JXA，[L724-822](file:///f:/SubFix/SubFix.lua#L724-L822)）。
- 右栏三态（无时间线/无 I-O/有 I-O）有信息文案；进度块平时隐藏、完成态停留片刻再收起。
- 危险动作（撤回/清空）未滥用红色，与备份组合放置。

### 2. 问题清单（按严重度；事实与判断分开）

#### P1 — 同屏一致性（实测，建议优先修）

1. **两套同名样式令牌发生漂移，且在合并窗口内同时渲染**
   - `SUBFIX_BTN_TOGGLE`：宿主 13px / radius 4 / padding 4·10（[L678](file:///f:/SubFix/SubFix.lua#L678)）vs core 14px / radius 5 / padding 6·12（[core L194](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L194)）；`BTN_BASE/SELECTED` 同样 13 vs 14px。
   - 宿主根 QSS **刻意不含**通用 QPushButton 规则（[L706-708](file:///f:/SubFix/SubFix.lua#L706-L708)），core 根 QSS **含**通用 `QPushButton{#243240…}`（[core L178-179](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L178-L179)）。
   - hover 语言不同：core 通用按钮 hover 变橙边，宿主 secondary hover 是灰边 `#6E6E6E`。
   - 结果：左右栏相隔 6px 相邻，左「配置」13px/`#282828`，右「管理…/文稿…/重新检测选区/取消」14px/`#243240`，同类控件尺寸、底色、悬停反馈肉眼可辨地不一致。
2. **间距是两套节奏**：左栏 `Spacing=5`（[L14856](file:///f:/SubFix/SubFix.lua#L14856)），右栏 embedded `Spacing=9`（[core L2489](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L2489)）；全局出现 0/3/4/5/6/8/9/10/11/16 多种间距与 8/10/12/16 多种边距，无 4pt 网格。
3. **字号只有两档**（10/13/14/15 无 token 体系），标题/分组/正文/元信息几乎同为 14px，层级主要靠 Tab 与一个 15px 右栏标题支撑。

#### P1 — 对比度（WCAG 2.1 计算值）

4. **白字 on 橙 ≈ 2.8:1，不达 AA 4.5:1**（14px 粗体不算 WCAG 大字）。影响面：所有主按钮（刷新/更新时间线/保存/开始生成…）、选中 Tab、树选中行；hover 态 `#CC5600` ≈ 4.2:1 仍差一点。
   - 方案 A（改动小、观感保持）：橙底配深色字/图标 `#1A212B` on `#FF6A00` ≈ **5.7:1**。
   - 方案 B：填充橙压深到约 `#C24E00`（白字 ≈ 4.8:1），hover 再略深。
   - 倾向方案 A 用于按钮/选中文字，方案 B 用于需要白字的场景，二选一在评审通过后定。

#### P2 — 信息层级与主次动作

5. **同屏橙色主按钮 6 个**：刷新字幕、执行批量替换、开始 AI 处理、保存修改、更新时间线、开始生成。其中「刷新字幕」是高频重载动作，与真正写回时间线的「更新时间线」同色同权重，终极动作不突出。
6. **顶栏职责混杂**：轨道选择 + 刷新 + 低频的「检查更新」+ 导出 + 44×40 的「⏻ 强制退出」（[L14894-14901](file:///f:/SubFix/SubFix.lua#L14894-L14901)）。不可逆的危险动作常驻右上黄金位且尺寸最大，仅靠 tooltip 防护；检查更新应归入设置/菜单。
7. 左栏「查找/替换」标签宽 35/45px，与右栏 46px 右对齐表单不统一；「备份 📁」纯图标按钮无文字、无 tooltip（[L15073](file:///f:/SubFix/SubFix.lua#L15073)）。

#### P2 — 控件模式（事实）

8. **伪进度条**：两处用 Unicode 字符 Label 拼 `ᗧ□□…⚑`（[core L2456-2459](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L2456-L2459)、[SubFix.lua L6720](file:///f:/SubFix/SubFix.lua#L6720)），宽度依赖字体回退（`□/ᗧ` 字形 advance 跨平台不稳，易错位），且无语义进度；应改 `ui:ProgressBar`（**需真机确认 Fusion UIManager 支持度**，不支持则用等宽字体+定宽 Label 兜底）。
9. **自研数字微调**：LineEdit+▲▼（20×16 / 24×18，[L14885-14886](file:///f:/SubFix/SubFix.lua#L14885-L14886)、[L15117-15118](file:///f:/SubFix/SubFix.lua#L15117-L15118)）替代 SpinBox：点击目标过小、无键盘上下键、可输入非法值。改 `ui:Slider`+数字或真机验证后的 SpinBox，退一步做输入校验。
10. checkable Button 同时承担分段控件与开关；「热词」开关上显示「N 条」（[core L2680](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L2680)），开关状态与计数混成一个标签。
11. 音频轨多选依赖 Tree 的 ExtendedSelection，无复选框、无「Ctrl/Shift 多选」提示；「取消生成」idle 时常驻置灰（合理但可 idle 隐藏减噪）。

#### P2 — 状态反馈与认知负荷

12. 校验/错误反馈仅底部一行 StatusLabel（如「请先选中一条字幕」[L4975](file:///f:/SubFix/SubFix.lua#L4975)），不区分 error/success/info 颜色或图标，用户在右栏操作时容易漏看。
13. 无结构化空状态：字幕树空时是空树+状态字；右栏三态也只是裸 Label。应给「空树态」一句引导 + 「刷新字幕」主动作；右栏三态加图标语义。
14. **弹窗过载**：SubFix.lua 17 个 AddWindow + core 12 个；各类「任务报告/待审核详情/违禁词」均为强模态，打断主流程，适合改非模态结果面板或树内标记（改动较大，列 P3 候选）。
15. 长任务存在两套进度 UI（模态「正在处理」[L6710](file:///f:/SubFix/SubFix.lua#L6710) + 右栏内嵌进度块），视觉语言重复。

#### P3 — 可访问性 / 适应性

16. 全窗口 Tooltip 仅 2 处；图标按钮无 tooltip；无快捷键（除编辑框回车）与 Esc 提示。
17. 树行状态仅靠颜色区分（橙/红粉 `rgb(230,180,170)`/绿粉 `rgb(160,230,180)`，[L5597-5599](file:///f:/SubFix/SubFix.lua#L5597-L5599)），色弱用户难辨，需图标/文字冗余编码。
18. Emoji 图标（📤⚙️📁📝⏻▲▼）在 Fusion Qt 下依赖平台 emoji 字体，Windows 可能单色/豆腐块，与专业深色主题不统一。
19. 右栏固定 330–390px、无 ScrollArea，1366×768 钳制高度后约 9 个区块可能压缩树或裁切；窗口未设显式 MinimumSize，手动缩小时多行 MinimumSize 行可能溢出。Tab 与内容区之间无连接边框/卡片，分组感弱。

### 3. 启发式评分（0–5，静态评审）

| 维度 | 分 | 说明 |
|---|---|---|
| 视觉一致性 | 2.5 | 双令牌漂移、双间距节奏、emoji 不统一 |
| 信息层级 | 3.0 | 功能全但主按钮过多、字号无梯级 |
| 状态反馈 | 2.5 | 状态无语义色、空状态弱、弹窗多 |
| 可访问性 | 2.0 | 橙底白字 2.8:1、无快捷键/tooltip、色觉单通道 |
| 布局与适应 | 3.0 | 两栏结构合理，小屏/右栏滚动有隐患 |
| 任务效率 | 3.5 | 老手效率高，学习成本偏高 |

## 二、改版方案（分阶段，每阶段独立可交付、可回滚）

### 阶段 P1：设计令牌统一 + 对比度（不改布局，风险最低，收益最大）

文件：[SubFix.lua](file:///f:/SubFix/SubFix.lua)、[subfix_generate_selection_core.lua](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua)

1. 在 SubFix.lua 建唯一令牌表（QSS 字符串 + 间距/字号数值）：
   - 色板：`bg/page #1A212B`、`bg/container #243240`、`bg/input #0F141A`、`text/primary #D6DDE7`、`text/tree #C2C7D0`、`border #34506b`、`accent #FF6A00`、`accent/hover`、`on-accent #1A212B`；语义色 `success/warning/danger/info`（各配深底可读前景）。
   - 4pt 间距阶：4/8/12/16；字号阶：12（元信息）/13（控件）/14（正文）/16（区块标题）；圆角统一 5px。
2. core embedded 形态优先复用宿主全局令牌（宿主注入或 `_G.SUBFIX_UI_TOKENS`），仅 standalone 保留自有兜底；消除 BTN_BASE/SELECTED/TOGGLE/根 QSS 的 13/14 与 hover 橙灰不一致。
3. 修橙底对比：选中 Tab/树选中/主按钮文字改为 `#1A212B`（方案 A），hover 态同步；验证后如观感不佳再切方案 B（填充 `#C24E00`）。
4. 同屏主按钮层级：终极写回「更新时间线」保持 accent；「刷新字幕」降为 secondary 或 outline-accent；各面板内主按钮维持单屏单主色原则（面板内主操作例外）。
5. StatusLabel 接语义色（info 默认、success/warning/danger 三个 token），错误提示带前缀图标（文字符号，如 `！`/`✓`，避免 emoji 豆腐块）。

### 阶段 P2：控件与状态修正

6. 字符进度条 → 真机验证 `ui:ProgressBar` 可用性后替换两处；不可用则定宽 Label + 等宽字体 + 数值百分比，去掉 `ᗧ/⚑`。
7. 轨道数字控件 → SpinBox/Slider（真机验证）或保留微调但补非法值校验与键盘焦点；▲▼ 目标至少 24×20。
8. 空状态：字幕树空态 overlay（引导文案 + 「刷新字幕」按钮位）；右栏三态加统一图标色；「备份」补文字或 tooltip；全部图标按钮补 Tooltip。
9. 「⏻ 强制退出」收入右上「设置/更多」菜单（含「检查更新」「强制退出」），顶栏只留任务相关动作；危险动作二次确认保留。
10. 热词开关：开关只显示开/关，计数放后随描述；音频轨多选改复选框列（真机支持则用 ItemIsUserCheckable）或加「按住 Ctrl 多选」提示。
11. 树行状态加非颜色冗余（前缀符号/列标记），去掉色觉单通道依赖。

### 阶段 P3：结构与适应性（改动较大，实施前再确认范围）

12. 右栏：加分隔/卡片容器与区块标题；内容超高度时可滚动；右栏间距并入统一阶。
13. 左栏表单标签宽度对齐右栏 46px 模式；Tab-content 连接边框。
14. 报告类强模态 → 非模态结果面板或树内标记（分批，先做「任务报告」一类试点）。
15. 快捷键（Esc 关弹窗、Ctrl+F 聚焦搜索、Ctrl+S 保存编辑、Ctrl+R 刷新）+ 窗口 MinimumSize。

## 三、涉及文件与模块

- [SubFix.lua](file:///f:/SubFix/SubFix.lua)：令牌表与根 QSS（L668-722）、主窗口布局（L14849-15133）、长任务进度（L6710-6783）、树行颜色（L4611、L5597-5641、L5773-5788）、StatusLabel 状态语义。
- [subfix_generate_selection_core.lua](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua)：自有令牌（L159-199）、内嵌面板（L2384-2496）、进度块（L2450-2460）、各子弹窗。
- 安装/打包脚本无需改（仅复制文件）；Python 侧不改。

## 四、依赖与注意事项

- Fusion UIManager 为 Qt 绑定，QSS 是子集，**ProgressBar/SpinBox/复选框列/ScrollArea/快捷键支持度必须真机先验**，每项保留现有控件兜底方案。
- embedded 与宿主同 VM：令牌以全局表共享，core 侧保持「宿主存在则复用、缺失则兜底」的现有防御模式；不得在加载期覆盖宿主定义（参照 SUBFIX_WINDOW_GEOMETRY 的守卫写法）。
- Lua 5.1 主 chunk 200 local 上限：令牌用全局表/do…end，不新增前向 local。
- 对比度数值为 sRGB→相对亮度手算，实施时用取色器在真机截图复核（Qt 实际渲染可能有抗锯齿差异）。
- 阶段 P1/P2 不改业务逻辑与控件 ID，仅改样式与局部控件，回归风险小；P3 触及布局结构，单独评审。

## 五、验证

1. 每阶段：`luaL_loadbuffer` 解析三个 Lua 文件 rc=0；IDE 诊断 0；Python 测试不受影响（`pytest tests -q` 基线 81 passed/27 skipped）。
2. 真机（DaVinci Resolve）截图 baseline 与 after：合并窗口左/右栏衔接处、各弹窗、1366×768 钳制态、生成中进度态、空态。
3. 真机逐项核对：橙底文字对比度（取色器算 ≥4.5:1）；同屏同类按钮尺寸/hover 一致；进度条不错位；SpinBox/ProgressBar 兜底分支。
4. 回归：选区识别启动/取消/写回、AI 面板、查找替换、备份/撤回/导出在样式改动后功能不变。

## 六、风险与兜底

- 风险：Fusion 对部分 QSS/控件支持与预期不符 → 每处视觉改动均可回退到当前字符串令牌；控件替换以「先真机探针后落地」推进，不支持即保留增强版旧控件。
- 风险：改橙底文字颜色影响选中辨识度 → 同时保留描边/字重双重编码，真机比对。
- 风险：统一 core 令牌影响 standalone 调试窗观感 → standalone 保留独立兜底常量，仅 embedded 复用宿主令牌。
