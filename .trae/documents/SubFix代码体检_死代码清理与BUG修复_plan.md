# SubFix 单窗口同屏合并、代码清理与 BUG 修复实施计划

## 一、仓库研究结论

### 当前架构

* [SubFix.lua](file:///f:/SubFix/SubFix.lua)（22219 行）当前实际有两个自建窗口：

  * 启动先显示 mini 窗口「找个字幕」（[L17998-L18192](file:///f:/SubFix/SubFix.lua#L17998-L18192)、[L22206](file:///f:/SubFix/SubFix.lua#L22206)）；

  * 点「打开完整版」后懒创建 full 窗口「改个字幕」（[create\_full\_content L18194-L18455](file:///f:/SubFix/SubFix.lua#L18194-L18455)、[open\_full\_window L18808-L18876](file:///f:/SubFix/SubFix.lua#L18808-L18876)）。

* full 窗口当前纵向布局为：顶部工具栏 → `MainTabs`/`TabStack`（精修工具、AI 工作台）→ 字幕树 → 常驻编辑框 → 底部备份/更新/状态栏。

* 「生成选区字幕」由独立菜单 [生成选区字幕.lua](file:///f:/SubFix/%E7%94%9F%E6%88%90%E5%8C%BA%E5%AD%97%E5%B9%95.lua) 或 mini 占位按钮进入，最终调用 [.subfix\_support/subfix\_generate\_selection\_core.lua](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua)。

* core 当前自建第二个 `bmd.UIDispatcher`（[L123-L126](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L123-L126)）和独立 `On.Timeout` 路由（[L170-L176](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L170-L176)），初始选择界面是 960 行强模态窗口（[L2272-L3234](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L2272-L3234)），通过 `RunLoop/ExitLoop` 返回 8 个值；另有 6 个二级设置窗口和 1 个进度窗口。

* SubFix.lua 已有 core 加载/运行封装 [L13193-L13239](file:///f:/SubFix/SubFix.lua#L13193-L13239)，但每次点击重新 loadfile，且 full 主窗口没有生成入口。

* 已确认所有 core 业务控件使用 `Generate*` 前缀，与主窗口现有 ID 无冲突；但 core 若注入宿主 dispatcher，绝不能覆盖宿主的 `disp.On.Timeout`（主路由在 [SubFix.lua:6611](file:///f:/SubFix/SubFix.lua#L6611)）。

### 用户已确认的目标

1. **同屏合并**：不做第三个标签页，也不保留「点按钮再弹一个识别大窗」；在同一个主窗口内采用左右布局：左侧字幕修改，右侧选区识别。
2. **只保留一个用户菜单**：达芬奇 Workspace 脚本菜单只显示 SubFix；安装/更新后移除旧的「生成选区字幕」菜单副本。仓库可保留该薄壳作为手动调试入口，但不面向用户部署。
3. **同步完成上轮全部清理**：高危/中低危 BUG、死代码、冗余实现、发布工程和测试修复一并处理。

## 二、目标界面与交互

### 合并后的唯一主窗口

* full 窗口改名为 **「SubFix 字幕工作台」**（保留现有内部窗口 ID，避免牵连历史 Close 绑定），尺寸由当前实际 500/860 宽统一为约 **1180×700**，并用屏幕可用区域钳制，小屏幕自动缩到 92% 可用宽高。

* 布局：

```text
┌──────────────────────────────────────────────────────────────┐
│ 顶部工具栏：字幕轨 / 刷新 / 检查更新 / 导出 / 强制退出          │
├───────────────────────────────┬──────────────────────────────┤
│ 左侧：字幕修改区（弹性宽度）    │ 右侧：选区识别区（约320px）    │
│  MainTabs: 精修工具/AI工作台   │  In/Out 选区范围 + 重新检测    │
│  SubtitleTree 字幕树           │  音频轨道多选树                │
│                               │  字幕长度 / 识别引擎            │
│                               │  热词 / 文稿匹配               │
│                               │  进度条/状态/详情/取消          │
│                               │  [开始生成]                    │
├───────────────────────────────┴──────────────────────────────┤
│ 常驻字幕编辑框：还原 / 保存修改                                 │
├──────────────────────────────────────────────────────────────┤
│ 底部：备份 / 撤回 / 清空 / 更新到轨 / 状态                     │
└──────────────────────────────────────────────────────────────┘
```

* 左侧精修工具页补回上轮确认的三个功能：**敏感词替换、清理空行、提取纯文本**（第三行按钮）；旧版导出 SRT、英文排版重复入口、SetPathBtn 死处理删除。

* 右侧页面常驻显示生成状态；热词库、文稿编辑、豆包 Key、Qwen 安装确认这类低频编辑器保留为子弹窗（它们是编辑器，不再是识别主界面）。

* 云端失败重试/切换本地模型等决策弹窗可保留；普通识别进度、取消、写回阶段、成功/失败信息全部内嵌在右侧，不再弹 `GenerateProgressWindow`。

* 生成期间使用已有 `set_gated_actions_enabled` 机制禁用左侧修改/导出等操作，右侧取消键保持可用；完成/失败后恢复。

* 启动直接显示合并后的主窗口并自动刷新字幕，mini 窗口退役。右侧生成面板在无时间线/无 I/O 时显示明确原因和「重新检测选区」，不是静默无反应。

## 三、核心技术方案

### 1. core 支持宿主注入与同屏面板

修改 [subfix\_generate\_selection\_core.lua](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua)：

* 新增宿主环境表，由 SubFix.lua 在首次 loadfile 前传入：`ui`、`dispatcher`、timer 注册函数、模式标记。

  * embedded 模式：复用主窗口的 UIManager/dispatcher，不创建第二个 dispatcher，不覆盖 `On.Timeout`，timer 走主插件的 `register_ui_timer`。

  * standalone 调试模式：保持现有自建 dispatcher/RunLoop 行为。

* 从 [show\_audio\_track\_selection\_dialog](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L2272) 中抽出：

  1. `build_generation_panel_descriptor(options)`：返回右侧 VGroup 描述符，控件 ID 保持现有 `Generate*` 前缀；
  2. `GenerationPanelController`：负责绑定事件、加载/刷新音频候选、读写选项、打开热词/文稿/Key 编辑器、启动/取消生成、更新内嵌进度；
  3. standalone wrapper：继续用同一页面描述符包一层独立窗口，供仓库内手动调试。

* 新增内嵌进度控件（使用 `GenerateEmbedded*` 前缀，避免和 standalone 进度窗 ID 混淆）：状态 Label、进度条、详情/提示 Label；取消复用 `GenerateSelectionCancelBtn`。

* 将 [generate\_selection\_subtitles L4687-L4956](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L4687) 拆为：准备 timeline/scope/音频候选、收集面板选项、执行识别、后处理、备份写回、收尾刷新；弹窗模式和内嵌模式共用同一执行管线，只替换 UI adapter。

* 进度 UI adapter 抽象为统一接口：`set_state`、`update_progress`、`set_cancel_enabled`、`finish`、`fail`；standalone 用原进度窗，embedded 更新右侧面板，不再调用放大窗口的 writeback overlay。

* core 暴露给 SubFix.lua 的接口：`build_embedded_panel()`、`attach_embedded_panel(host_window, callbacks)`、`refresh_embedded_context()`、`start_embedded_generation()`、`shutdown()`（如需）；`run(options)` 保留给独立调试脚本。

### 2. SubFix.lua 同屏布局与启动改造

* core 加载器 [L13193-L13210](file:///f:/SubFix/SubFix.lua#L13193-L13210) 改为一次性缓存加载，先注入宿主环境；加载失败时右侧显示「生成组件不可用 + 重新检测」，不影响字幕修改主功能。

* `create_full_content()` 中把当前 TabBar/TabStack/SubtitleTree 包入左侧 VGroup，右侧插入 core 返回的生成面板描述符；编辑框和底栏保持整行宽度。

* `ensure_full_window_initialized()` 在创建窗口前确保 core descriptor 可构建；窗口创建后、Show 前绑定右侧面板事件。

* 顶部「刷新字幕」和右侧「重新检测选区」均触发 `refresh_embedded_context()`；生成成功后调用现有 `refresh_subtitles(win, ...)` 并重新加载右侧候选。

* 修复 [open\_full\_window L18846](file:///f:/SubFix/SubFix.lua#L18846) 硬编码 `500×700` 导致主窗口宽度与 `SUBFIX_WINDOW_SIZE.full` 不一致的问题，统一走窗口尺寸常量与屏幕钳制。

* 启动段 [L22196-L22212](file:///f:/SubFix/SubFix.lua#L22196-L22212) 改为创建并显示合并主窗口，`active_window=win`，自动刷新目标改为 win；不再创建/显示 mini\_win。

* 退役 mini：删除 `mini_content`、`create_mini_window`、mini 事件绑定块、Mini\* 专用控件分支与「打开完整版」入口；保留通用的 `target_window` 传参能力（弹窗仍需要），但所有用户可见路径只剩主窗口。删除前逐个 grep `mini_win|Mini` 确认无运行时引用。

### 3. 独立菜单与打包

* [install\_subfix.ps1:76](file:///f:/SubFix/install_subfix.ps1#L76)、[build\_pkg.sh](file:///f:/SubFix/build_pkg.sh)、[build\_pkg\_win.ps1](file:///f:/SubFix/build_pkg_win.ps1)、[build\_subfix\_test\_package.sh](file:///f:/SubFix/build_subfix_test_package.sh)、[sync\_to\_plugin.sh](file:///f:/SubFix/sync_to_plugin.sh) 不再向用户菜单复制 `生成选区字幕.lua`。

* [subfix\_update.py](file:///f:/SubFix/.subfix_support/subfix_update.py) 白名单删除 `SubFix/生成选区字幕.lua`；更新成功后主动删除 `target_root/SubFix/生成选区字幕.lua`，并扩展 legacy cleanup 覆盖用户级与系统级旧副本。

* 仓库中的 [生成选区字幕.lua](file:///f:/SubFix/%E7%94%9F%E6%88%90%E5%8C%BA%E5%AD%97%E5%B9%95.lua) 保留为开发/手动调试脚本，但其失败提示改为可视化消息窗。

## 四、上轮确认问题的修复清单（并入本次）

### A. 高危功能/BUG

1. **Windows 在线更新必然失败**：[subfix\_update.py:179-187](file:///f:/SubFix/.subfix_support/subfix_update.py#L179) 改用 `PurePosixPath`/`as_posix()` 统一白名单、manifest、zip entry key；补 POSIX/Windows 单元测试。
2. **更新目标路径缺少 Support 层**：[subfix\_update.py:24](file:///f:/SubFix/.subfix_support/subfix_update.py#L24) 修正用户根；SubFix.lua 安装更新显式传 `--target-root`（user/system 由现有 `subfix_dr_utility_root()` 决定）。
3. **接回 3 个精修功能**：在左侧精修页新增第三行按钮，并重写处理逻辑对齐现有活模型（plain 查找、`%` 转义、现有搜索/树刷新、快照机制）。
4. **轨道树失败返回值错位**：[core:2402](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L2402) 补齐第 6 错误返回值，避免误报「已取消」。
5. **批量替换含** **`%`** **报错**：[SubFix.lua:13730,13763](file:///f:/SubFix/SubFix.lua#L13730) 与敏感词替换统一转义 replacement 或改 plain 查找+拼接。
6. **Qwen 对齐 JSON 磁盘泄漏**：[asr:6716-6717](file:///f:/SubFix/subfix_asr_transcribe.py#L6716) 解析后即删，批次结束清理本次 `subfix_qwen_align_outputs`。
7. **旧 align 模式 CLI 锁死**：与 Lua 旧 stable-ts/whisperx/CTC 死链一并删除，不补 argparse choices；保留 qwen/v5 活路径。
8. **取消竞态孤儿进程**：PID 文件未出现时短轮询重试 kill；检查 Windows launch 返回值，失败立即返回而非等 30 秒；正常收尾清理 `.ps1`。
9. **独立脚本失败无提示**：core 致命错误与调试入口提供原生消息窗，print 作为回退。
10. **mimo\_asr Windows 命令失效**：改 argv 数组/Windows 双引号规则，去除 `shlex.quote + shell=True`。
11. **文稿/热词 JSON 与 textnorm**：core json\_escape 补 `\b/\f` 与 U+0000-001F；进度文件改用完整 JSON 解析；修复 [textnorm:291](file:///f:/SubFix/subfix_generate_textnorm.py#L291) 让 `三四五条/七八九条` 不再误转，同步 docstring 和测试。
12. **生成批次字段缺失**：v4/v5 回填 Lua 仲裁读取的 batch/track/speaker 字段，修复分贝择优恒为 0；以 v5 现有边界测试护航。

### B. 中低危 BUG/健壮性

* core：临时 JSON/SRT/batch\_plan/script\_match\_plan/rebuild SRT 在成功后清理（诊断 `.audio_source.json`、备份 SRT 保留）；取消 Qwen 安装不再弹失败；helper 目录取目录识别 `/` 与 `\`；DF 时间码补丢帧；Windows 非 ANSI 路径统一 FFI 宽字符读写/CreateProcessW（pcall + ANSI 回退）；停止无条件覆盖 `SUBFIX_WINDOW_GEOMETRY`。

* SubFix.lua：删除死函数内 nil 索引残留；简化恒等三元；后台启动失败清理残留；manifest 文件读取确保 close；核实并修进度路径分隔符。

* Python：豆包 v1 不再把 API Key 当 uid、非 JSON/网关响应重试与 v2 状态码行为对齐；音频时长异常不静默吞；v5 搜索窗按帧率缩放（30fps 仍为 12 帧）、能量峰值改高分位、尾部帧估算与 half-up round 统一；v4 恒真条件直白化。

* 卸载/安装/测试：[uninstall\_subfix.ps1](file:///f:/SubFix/uninstall_subfix.ps1) 删除后 Test-Path 复核、失败返回非零、补提权引导与 `.bat`；修复 [test\_ai\_script\_conversion.py:104,132](file:///f:/SubFix/tests/test_ai_script_conversion.py#L104) 失效锚点；setup\_asr\_env.ps1 存为 UTF-8 BOM，并核实旧 venv 提示是否仍有效。

* 发布工程：新增仓库内更新包构建脚本（白名单单一事实源，补 Windows `ffmpeg.exe` 条目，生成 manifest/zip/sha256）；打包文件清单增加入口依赖校验。

### C. 死代码/冗余清理（合并后预计净减更多）

* SubFix.lua：删除旧对齐引擎链、口播一致性终检链、参考轨/静音检测/ASR 复核窗旧簇、IIFE 内重复 JSON 解析器、collect\_exportable\_subtitles 等重复/零引用函数、15 个历史窗口 Close 绑定、空桩/别名。

* core：删除旧选中片段/播放头兜底选区簇、单片段 helper、SRT 旧解析、零引用常量/函数、热词条读写重复定义；同屏重构中避免把这些死代码搬进新面板。

* Python：删除 v3 旧 batch plan、旧 align/ctc 分支、零引用导入/函数；v4 保留（v5 基础算法库），仅清理确认死项并合并重复 PCM/RMS 实现。

* 仓库卫生：`SubFix.lua.bak` 取消 git 跟踪并删除，`.gitignore` 补 `*.bak`；核实 dist/ 跟踪状态。

## 五、实施步骤（依赖序）

1. **基线**：记录文件行数/哈希；确认 Lua 静态检查、Python py\_compile/pytest；列出所有待删符号核对表。
2. **独立 Python/更新器修复**：更新器路径/目标根、qwen 输出泄漏、mimo 命令、textnorm、豆包健壮性、v4/v5 字段与帧率问题；先补/改测试。
3. **删除非 UI 死代码**：Python 旧引擎/旧 align、SubFix.lua 旧引擎与旧终检链、core 已确认旧簇；每删一个符号立即 grep 复核。
4. **core 重构前 BUG 修复**：返回值、取消、临时文件、JSON、路径、DF、安装取消提示等，先保证 standalone 模式仍通过。
5. **core 面板化重构**：抽 descriptor/controller/progress adapter；standalone wrapper 回归原流程；再做宿主 dispatcher/timer 注入。
6. **SubFix 同屏布局**：右侧面板入窗、绑定 controller、生成 gate、刷新/成功回调、内嵌进度；保留左侧精修/AI/树/编辑/底栏行为。
7. **mini 退役与唯一窗口启动**：主窗口直接启动并自动刷新；删除 mini UI/事件/专用分支；修窗口尺寸与屏幕钳制；清理历史 Close 绑定。
8. **精修功能接回与 Lua 剩余 BUG**：新增 3 按钮、删除 3 个死处理、修替换 `%`、后台启动/文件句柄等。
9. **单菜单打包/更新迁移**：构建/安装/sync 去掉独立入口；更新器白名单与旧文件清理；卸载提权与结果复核；新增更新包构建工具。
10. **终检与交付**：静态检查、测试、打包清单校验、手动 Resolve 冒烟；不自动部署、不提交 git。

## 六、验证

### 自动/静态

* Lua：`lua-language-server --check`（依据 [.luarc.json](file:///f:/SubFix/.luarc.json)）或 LuaJIT 语法检查；所有删除符号 grep 零残留；禁止新增顶层 local 以避开 200 local 上限。

* Python：改动文件全部 `py_compile`；运行 tests/，新增 textnorm 电话/概数用例、更新器 POSIX/Windows zip 路径用例、v4/v5 字段与边界用例。

* 打包：断言发布 payload 只含 `SubFix/SubFix.lua` 一个用户菜单入口，core/Python 支持文件齐全；更新 zip 能通过白名单校验且安装到含 `Support` 的正确根。

* 资源：成功/失败/取消三路径核对临时文件与 qwen 对齐输出清理；诊断与备份文件保留。

### DaVinci Resolve 手动冒烟

1. 启动后只出现一个「SubFix 字幕工作台」，无 mini、无第二个识别主窗；Workspace 菜单无独立「生成选区字幕」。
2. 无时间线/无 I/O/有 I/O 三种状态右侧提示正确，「重新检测选区」可刷新轨道树。
3. 热词、文稿匹配、豆包 Key、Qwen 安装子弹窗可打开/保存/取消；主动取消 Qwen 安装不弹失败。
4. 多轨选择、长度/引擎切换、开始生成、进度百分比/阶段/取消均在右侧显示；取消后无孤儿 python/ffmpeg，无 `.ps1`/JSON/SRT 泄漏。
5. 生成成功后写回字幕轨 1，左侧字幕树自动刷新；失败原因可见且不破坏已加载字幕。
6. 左侧四个原按钮 + 新接回三个按钮、查找替换（含 `%`）、常驻编辑框、撤回/清空/备份/更新轨/导出/AI 四任务全部正常。
7. Windows 检查更新至少走到「当前已是最新」；mac 上布局与路径辅助无回归。

## 七、风险与处理

* **Fusion 同窗口事件/定时器行为不确定**：所有右侧事件在主窗口 Show 前绑定，timer 复用主插件已验证路由；不依赖两个 UIDispatcher 跨实例绑定。若 Resolve 实测发现异常，standalone wrapper 保留为即时回退路径。

* **core 控制器重构范围大**：先抽 UI descriptor 和 adapter，不改识别/写回算法；standalone 与 embedded 共用管线，先保证旧入口回归再接主窗。

* **mini 删除牵连双窗口分支**：按 grep 分批删除，先让主窗口独立启动稳定，再删 Mini 专用代码；通用 target\_window 机制保留给子弹窗。

* **宽窗口在小屏幕上溢出**：根据 screen bounds 钳制到 92% 可用区域，右栏 MinimumSize 约 300，左栏保持可压缩。

* **旧菜单清理权限**：系统级残留可能需要管理员；更新器用户级更新成功后尽力 pcall 清理，权限不足时在诊断/状态中提示，不伪装成成功。

* **生成字段补强改变多轨仲裁**：只填真实 batch/track/speaker 元数据；无法可靠取得的字段保持现有默认并写诊断，不臆造。

