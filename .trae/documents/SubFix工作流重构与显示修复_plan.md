# SubFix 工作流重构（选区识别前置）与默认窗口裁切修复 / UI 抛光计划

> 框架：uicraft（先结构后表达）；环境为 Fusion UIManager（Qt 子集 QSS，Lua 5.1/LuaJIT）。
> 用户诉求：① 默认窗口打开底部文字显示不全，拖动面板后才完整（截图 1→2）；② 流程应从「选区识别」（语音转写/文稿匹配）开始，生成后才进入「精修工具」；③ 细节打磨抛光。

## 一、仓库调研结论（根因，均有实证）

### 1. 默认窗口裁切（截图 1：撤回/清空、更新到轨、状态行整体消失）

- 默认尺寸 `SUBFIX_WINDOW_SIZE.full = {1180,700}`（[SubFix.lua L664-666](file:///f:/SubFix/SubFix.lua#L664-L666)）；用户屏幕为 150% DPI（截图 1756px 物理宽 ≈ 1180 逻辑）。
- 内容最小高超过窗口客户区：右栏 VGroup 固定内容 ≈ 484px（边距 24＋标题 26＋信息 label＋音频树 min **150**＋多选提示＋长度/模型/热词/文稿 4 行×20＋文稿状态 2 行＋选区范围＋按钮 30＋Spacing 8×约 10），叠加顶栏 ~86、编辑区 ~92、底栏 ~180，整体最小高约 **840 逻辑像素** > 客户区。Weight=1 的树把底栏顶出视口。
- 窗口**无 MinimumSize、Show 后无布局重算**（[L17903](file:///f:/SubFix/SubFix.lua#L17903) 仅 `win:Show()`）；embedded 描述符在 AddWindow 前注入，Qt 首次布局用了未稳定的 sizeHint，必须手动 resize 才 relayout —— 与「拖一下才完整」完全吻合。
- 小屏钳制存在（[centered_geometry L931-950](file:///f:/SubFix/SubFix.lua#L931-L950)），但只缩窗口、不缩内容最小高。

### 2. 右栏信息文字叠印到音频树（两张截图右栏顶部均可见两行字压在树上）

- `GenerateSelectionInfoLabel` 为 `WordWrap=true, Weight=0` 且**无显式高度**（[core L2407](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L2407)）。
- 长文案（[core L1438](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L1438)）：`请先用 I/O 设置 In/Out 选区；灰色时间线选择或鼠标选中片段 Resolve 脚本 API 读不到`，约 40 字在 ~306px 可用宽内折 2–3 行；Fusion Label 按 1 行申请高度，多行部分溢出绘制到下方树。
- `set_info`（[core L2515-2520](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L2515-L2520)）只改 Text，不调高度。

### 3. 信息架构顺序与用户流程相反

- 当前：左栏 Tab 默认「精修工具」（[L17826-17832](file:///f:/SubFix/SubFix.lua#L17826-L17832)），右栏常驻「选区识别」；首次打开无字幕时精修按钮虽被 `GATED_ACTION_IDS`（[L1053-1058](file:///f:/SubFix/SubFix.lua#L1053-L1058)）禁用，但用户不知道该先做什么。
- 目标流程：**① 选区识别（ASR/文稿匹配）→ ② 精修工具 / ③ AI 工作台 → 写回时间线**。
- 好消息：生成完成已有宿主钩子 `on_finish`（[L10581-10589](file:///f:/SubFix/SubFix.lua#L10581-L10589)）会刷新树；字幕加载状态有统一闸口 `set_subtitle_loaded_state`（[L1195-1201](file:///f:/SubFix/SubFix.lua#L1195-L1201)）；core 全部事件绑定按控件 ID 查找（`window:GetItems()`），**把控件换容器/重排不需要改任何业务代码**。

## 二、改版方案

### A. 流程结构：三步工作台（核心）

1. MainTabs 改为三项：**「① 选区识别」「② 精修工具」「③ AI 工作台」**，默认第 0 页。
2. **右栏整体移除**，core 面板以 **wide 形态**嵌入第 0 页；字幕树/编辑区/底栏获得全宽（长字幕预览受益）。
3. TabStack 四个页面：`RecognitionPage`（识别，wide 面板）、`ToolTabPage`（现有精修内容原样迁入）、`AITabPage`（现有 AI 内容原样迁入）、`LockedHintPage`（锁定引导：文字说明 +「去选区识别」按钮）。点击②③但字幕未加载 → 显示锁定页并在状态栏提示；已加载 → 正常页。**不依赖 TabBar 禁用 API**（Fusion 支持度不确定）。
4. **空间让渡**：识别页激活时隐藏字幕树容器与编辑区（`Hidden=true`），音频树获得主高度；切到②③恢复。识别页内容最小高 ≤ 460px，任何屏都完整。
5. **自动流转**：
   - 生成成功 `on_finish`：刷新树 → 解锁 → 自动切「② 精修工具」→ 状态「选区字幕已生成，开始精修吧」。
   - 启动自动刷新发现轨道已有字幕（`set_subtitle_loaded_state(true)`）：解锁并自动进②；无时间线/无字幕停留①。
   - 会话内解锁后不再回锁（避免刷新间隙跳页打扰）。
6. core 改动：`build_embedded_panel(opts)` 与 `build_descriptor(wide)` 增加 wide 布局——根容器去 330–390 固定宽改 `Weight=1`、`ContentsMargins={0,0,0,0}`；音频树 `Weight=1, MinimumSize={0,120}`；长度/模型/热词/文稿改**两列网格**（左右各两行，标签 46px 对齐）；文稿状态与选区范围合并为一行紧凑元信息；进度块/开始生成/取消 ID 与语义全部不变。standalone 弹窗路径（wide=nil）保持现状。

### B. 裁切与初始布局修复

7. 默认尺寸 1180×700 → **1240×760**（保留小屏 clamp 逻辑）。
8. **压缩固定高度**：底栏「备份行」与「撤回/清空行」合并为一行（`📁 [备份下拉 Weight=1] 撤回 清空`，省约 38px）；移除多余 VGap。
9. **Show 后强制重算布局**：新增 `subfix_recalc_layout()`，依次尝试 `win:RecalcLayout()`（pcall，Fusion 部分版本提供）；兜底用「读取当前 Geometry 并原样设回」触发 Qt relayout（用户实测 resize 可修复，等价触发）。调用点：`win:Show()` 后立即一次、`full_window_deferred_sync_timer`（60ms）一次、每次 Tab 切换/树与编辑区显隐/生成开始结束后一次。
10. 不设窗口 MinimumSize（小屏上会反向超界）；靠内容最小高收敛保证各页完整。

### C. 信息标签叠印修复

11. core `set_info` 按折行容量反算高度：字符宽度权重 ASCII=1、CJK=2，wide 容量 90 单位/行、narrow 42 单位/行，显式 `\n` 计行；设置 `MinimumSize={0, lines*18+6}`（初始 20）。
12. 长文案拆分（[core L1438](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L1438)）：主信息「未检测到 In/Out 选区」，细节「灰色选择或鼠标框选 Resolve 脚本读不到，请用 I/O 打点后再检测」，高度自适应两行不再叠印。

### D. 细节抛光（uicraft：层级/状态/空态/间距）

13. 识别页加两个 12px 灰色小标题分组：「识别来源」（音频树＋多选提示）、「生成选项」（两列网格）；底部开始生成为该页唯一主色按钮。
14. **字幕树空状态**：树容器改为 `SubtitleTreeWrap`（Weight=1）内含树与 `SubtitleEmptyState`（居中空状态：「当前轨道没有字幕」＋「去选区识别」主按钮＋「刷新字幕」次按钮），按 `#current_rows` 切换显隐；有数据自动换回树。
15. 锁定页/空状态图标一律用文字符号（`!` `→`），不引入新 emoji，避免 Windows 字体回退豆腐块。
16. 间距全面归 8pt 网格（识别页内 4/8）；识别页复用页面根 QSS，令牌沿用现有 `SUBFIX_DESIGN`，不新增色板。
17. 「①/②/③」编号即步骤引导；状态栏语义色沿用上轮成果（成功/警示/危险自动着色）。

## 三、涉及文件与模块

- [SubFix.lua](file:///f:/SubFix/SubFix.lua)
  - L664 默认尺寸；
  - `create_full_content()`（L14965+）：去右栏、三步 Tab、Stack 四页、wide 面板注入、树容器空状态、底栏合并、识别页显隐容器；
  - 初始化（L17813+）：AddTab 三项、锁定页映射函数、初始页逻辑、RecalcLayout；
  - `set_subtitle_loaded_state`（L1195）：解锁/自动进②钩子；
  - `build_generate_embedded_panel_descriptor`（L10540）：透传 wide；`on_finish`（L10581）：成功后切②；
  - 两个启动 timer（L15544/L15559）：追加 relayout 与"已有字幕自动进②"。
- [subfix_generate_selection_core.lua](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua)
  - `build_embedded_panel(opts)`（L5403）/ `build_descriptor(wide)`（L2393）：wide 分支；
  - `set_info`（L2515）：折行高度自适应；L1438 文案拆分。
- 安装器/Python 不改。

## 四、依赖与注意事项

- **控件 ID 是重排安全边界**：wide 面板必须保持 `GenerateAudioTrackTree/GenerateSubtitleLength*/GenerateSubtitleEngine*/GenerateHotword*/GenerateScriptMatch*/GenerateSelectionRangeLabel/GenerateEmbeddedProgressBlock/GenerateSelectionConfirmBtn/GenerateSelectionCancelBtn/GenerateEmbeddedRescanBtn/GenerateSelectionInfoLabel` 全部同名同义；embedded controller 单例在描述符构造时创建、attach 时按 ID 绑定，跨 Stack 层级可正常 Find。
- Lua 5.1 主 chunk 200 local 上限：新增函数全部全局/`do…end`，不在主 chunk 堆 local。
- `Hidden` + Weight 切换后必须 relayout（Fusion 已知特性），已在第 9 步全覆盖。
- 描述符构造在 `create_full_content()` 内（AddWindow 之前），core wide 分支不能依赖窗口已存在的对象；它本来就只返回描述符，安全。
- 「取消生成」idle 隐藏（上轮）在 wide 页保持；进度块嵌在识别页选项下方。

## 五、验证

1. LuaJIT `luaL_loadbuffer` 两文件 rc=0；wide 分支做 ID 静态核对（grep 新旧描述符 ID 集合一致）。
2. IDE 诊断 0；`python -m pytest tests -q` 保持 81 passed/27 skipped。
3. 安装器重新部署 + SHA256 核验。
4. **真机冒烟清单**（用户在 Resolve 执行）：
   - 1080p@150% 与 1366×768：脚本打开默认即完整（撤回/清空/更新时间线/状态行全部可见），无需拖动；
   - 无时间线/无 I/O：停在①，信息标签两行完整不叠树；
   - 生成成功后自动跳②、树有数据、按钮解锁；未解锁时点②③出现锁定页；
   - 空轨时树区显示空状态与两个按钮；识别页内音频树/两列选项/进度块/取消按钮正常；
   - 各 Tab 切换无残留、无裁切；写回/导出/备份回归正常。

## 六、风险与兜底

- `win:RecalcLayout()` 若不存在：pcall 静默 + Geometry 自赋值兜底（等价 resize，已被用户截图证明有效）。
- wide 布局在小屏过挤：两列网格可在 <1000px 宽时（真机探针后决定）退回单列；音频树 min 150→120 已预留空间。
- 自动跳页打扰老手：仅在"生成成功"与"启动已有字幕"两个时机各跳一次；手动切到①后不强制拉回。
- 改动集中在布局与导航，业务逻辑/控件 ID 不动，功能回归面小。
