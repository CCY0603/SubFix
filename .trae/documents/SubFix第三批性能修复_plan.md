# SubFix 第三批性能修复 实施计划

## 仓库调研结论

本批共 11 项（P1–P7、P9–P12；**P8 豆包流式经用户确认本批暂缓**，现有豆包极速版/2.0 代码一字不动）。所有问题均已在代码中逐一定位并取证：

| 项 | 现状（取证） | 影响 |
|---|---|---|
| P1 sleep | [SubFix.lua](file:///f:/SubFix/SubFix.lua#L746-L755) Windows 下 `subfix_sleep` 用 `os.clock()` **忙等自旋**；4 处调用（L229/L565/L8376/L15055 的 0.05–0.15s 轮询） | 等待期间打满一个 CPU 核 |
| P2 marker 哈希化 | 终检每条 issue 走"删旧→AddMarker→GetMarkers 回读"3 次 Resolve API（[L18481-L18510](file:///f:/SubFix/SubFix.lua#L18481-L18510)），每次终检先全删再全加（[L18530](file:///f:/SubFix/SubFix.lua#L18530)） | marker 闪烁、API 放大 3N 次 |
| P3 剪辑点二分 | 每条字幕边界都**线性遍历全部剪辑点**（[L18329-L18335](file:///f:/SubFix/SubFix.lua#L18329-L18335)，2×行数×剪辑点数）；播放跟随线性扫行（[find_row_at_playhead L6005-L6016](file:///f:/SubFix/SubFix.lua#L6005-L6016)） | 长时间线终检/拖动跟随卡顿 |
| P4 日志环形缓冲+防抖 | LogMsg 每条都：字符串前插（O(n²)，[L11992-L12002](file:///f:/SubFix/SubFix.lua#L11992-L12002)）、立即刷 UI、io.open/write/close 落盘；`workflow_log_buffer` 无行数上限 | 生成高峰（成千上万条）CPU/内存/磁盘三重放大 |
| P5 Qwen 检测缓存+消冻结 | 每次检测都**同步拉起一个 Python 进程**并阻塞 UI 线程轮询等待（[inspect_local_qwen L1116-L1139](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L1116-L1139)、[windows_run_sync_hidden L938-L996](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L938-L996)），结果不缓存；调用点：开始生成 L2900、安装面板 L3494 | 每次开面板/开始生成卡数百 ms~数十秒 |
| P6 PCM array 重写 | [compose_track_audio L335-L345](file:///f:/SubFix/subfix_generate_v4.py#L335-L345) 对**整条轨道逐样本 Python 循环**（153s 轨道 ≈ 245 万次 int 加法+钳制） | 合轨慢，素材越长越慢 |
| P7 ffmpeg timeout | [cut_audio L2854](file:///f:/SubFix/subfix_asr_transcribe.py#L2854) `subprocess.run` **无 timeout** | ffmpeg 卡死则整个生成永久挂起 |
| P9 | 字幕树无条件整棵 Clear/Add 重建（refresh_subtitles [L11458](file:///f:/SubFix/SubFix.lua#L11458)） | 数据未变也全量重绘 |
| P10 | 生成进度轮询每 tick 无条件 SetText 状态行/百分比行（core `update_generate_progress_window` 约 L3896） | 无效 Qt 重绘 |
| P11 | 生成面板 `refresh_embedded_context`（[L5657-L5693](file:///f:/SubFix/.subfix_support/subfix_generate_selection_core.lua#L5657-L5693)）每次全量重扫 In/Out+全部音频轨并重填轨道树 | 面板刷新/首屏有重复扫描 |
| P12 | `update_shared_status`（[L1368-L1373](file:///f:/SubFix/SubFix.lua#L1368-L1373)）同文本也 SetText | 无效重绘 |

关键事实：
- core 中 `subfix_winio.sleep` 已成功使用 kernel32 Sleep（L985-992），证明该 FFI 路径在本机 Resolve LuaJIT 安全可用，P1 直接复用同一模式。
- Python 端 `inspect_install`（[subfix_qwen_local_manager.py L246-L261](file:///f:/SubFix/.subfix_support/subfix_qwen_local_manager.py#L246-L261)）status 路径已不 import torch，只做文件/marker 存在性检查——P5 成本主要在 Python 进程冷启动，故缓存放在 Lua 侧、并做后台预热。
- 单条时间线音轨上片段互不重叠，P6 的重叠钳制循环正常情况迭代 0 次，可安全按切片批量拷贝。
- 终检已有现成的稳定 issue 哈希键 `pre_delivery_issue_key`（[L18245-L18255](file:///f:/SubFix/SubFix.lua#L18245-L18255)），P2 直接复用。

## 改动文件

- `f:\SubFix\SubFix.lua`：P1、P2、P3、P4、P9、P12
- `f:\SubFix\.subfix_support\subfix_generate_selection_core.lua`：P5、P10、P11
- `f:\SubFix\subfix_asr_transcribe.py`：P7
- `f:\SubFix\subfix_generate_v4.py`：P6
- `f:\SubFix\tests\test_v4_compose_track_audio.py`：**新增** P6 等价性测试
- 不改：豆包相关全部代码（P8 暂缓）、v5、UI 布局与样式、生成结果数据格式

## 实施步骤（按依赖排序，每项独立可验证）

### 阶段 A：Lua 低风险项先行

1. **P1 — kernel32.Sleep 替换忙等**（SubFix.lua）
   - 在 FFI 初始化区（ffi 已 require 处，L1007 user32 加载附近）pcall 增加 `ffi.cdef "void Sleep(uint32_t ms);"` + `ffi.load("kernel32")`，存为全局 `subfix_kernel32`，失败为 nil。
   - 改写 `subfix_sleep`：Windows 且 kernel32 可用时 `Sleep(max(1, round(sec*1000)))`；否则保留原忙等兜底；macOS 分支不变。
   - 不引入任何 cdata 数组（记取既往 ffi.copy 越界 1 字节的崩溃教训，本项只调用一个标量参数的系统函数）。

2. **P12 — 状态栏同文本去重**（SubFix.lua L1368）
   - `set_window_status_text`（或 update_shared_status 内）记录每个窗口上次文本，相同则直接 return；切换窗口目标时按窗口存键。

3. **P3 — 两处线性查找改二分**（SubFix.lua）
   - 新增通用 helper：对 start_frame 单调非降的行数组做二分定位；先做一次单调校验，不满足则**回退原线性逻辑**（保证异常数据零行为变化）。
   - `find_row_at_playhead`：二分找 start≤帧 的最后一行再验 end；abs/rel 双坐标语义保持。
   - 终检剪辑点匹配：`collect_visible_timeline_cut_frames` 返回排序后数组（实现时先验证其已排序；无序则就地 table.sort 并在 helper 内排序），`add_boundary_alignment_issue` 用二分只检查入位位置两侧邻居找容差内最近剪辑点；判断结果与现在完全等价。

4. **P2 — marker 哈希化增量同步**（SubFix.lua 终检段）
   - `run_pre_delivery_final_check`：开头**一次** GetMarkers 快照出现有 `subfix-pre-delivery|` customData 集合；用 issues 的 `pre_delivery_issue_key` 构造目标集合。
   - 已存在的 key：跳过（不再删+加+回读 3 次 API、不闪烁）。
   - 不存在的：走 AddMarker（去掉其内的"先删同 key"步骤在增量模式下的冗余调用——新增轻量内部参数 `skip_pre_clear`，旧函数签名行为对其它调用方不变）。
   - 快照中有、目标集合没有的：逐个 DeleteMarkerByCustomData（替代先全删）。
   - 手动"清除全部 marker"按钮仍调用原 `clear_all_pre_delivery_markers`，不动。

5. **P4 — 日志环形缓冲+防抖**（SubFix.lua LogMsg 段）
   - `workflow_log_buffer` 改为"数组存行+渲染时拼接"，保留最近 500 行（最新在前），消除前插 O(n²) 与无限增长。
   - 新增 200ms 单次防抖 timer（ui:Timer 模式，仿 search_debounce_timer）：LogMsg 只置 dirty，timer 里才 `update_log_window_view`；同一 tick 内只刷一次。
   - 落盘改为内存 pending + 1s timer 批量追加写（保持一次 io.open 写多行后 close）；pending≥64KB 立即冲；保留现有 2MB 截断重置逻辑与会话退出清理钩子（在现有退出/cleanup 调用点补一次 flush）。
   - `workflow_log_buffer` 对外仍是字符串属性（渲染前同步成同格式字符串），避免其它读取方受影响。

6. **P9 — 字幕树内容指纹去重**（SubFix.lua refresh_subtitles）
   - 计算指纹：行数 + 每行 `index|start|end|text` + 当前搜索词 + 目标窗口；指纹未变时跳过树 Clear/Add 重建（仍刷新轻量状态）。
   - 强制重建入口一律 bypass：拆分/合并/编辑保存、更新时间线后、切换备份、搜索、加载占位、rerender_tree_with_selected_row、首屏自动加载——给这些调用显式 force（实现时逐一核对全部调用点，默认保守：带 options 的现有入口默认 force，仅定时/轻量刷新路径走指纹判断）。

### 阶段 B：core（生成面板）三项

7. **P5 — Qwen 检测缓存 + 后台预热消冻结**（core）
   - 新增模块级缓存 `{result, checked_at, signature}`：signature = env python/ready marker/legacy marker 的存在性+mtime、模型目录命中名；TTL 8 秒；`inspect_local_qwen(paths, force)` 签名与 TTL 命中直接返回。
   - 面板 attach/首屏 context 刷新后，用现有后台启动链路（launch_background_windows / 现有 .done 模式）异步预热一次 status，写临时 JSON，完成回调入缓存；不弹控制台、失败静默（落回旧同步路径）。
   - 开始生成（L2900）：缓存有效直接用；无效时维持现同步路径（最坏情况与今天一致，不会更差）。
   - 安装流程成功返回后（install_qwen_and_generate_embedded L5650 一带、standalone 对应处）显式 invalidate 并 force 检测一次。
   - need_qwen 门控判定逻辑一字不改，只改"检测结果从哪来"。

8. **P11 — 生成面板选区指纹缓存**（core refresh_embedded_context）
   - 指纹 = timeline 标识对象 + fps + scope 起止帧 + 音频候选列表（文件路径/源起止/timeline 起止/轨号/折叠后选项签名）。
   - 指纹相同则跳过 set_context 重填轨道树（保留首次、In/Out 变化、轨道变化后的刷新）；提供 force 参数供"重新检测"按钮使用。

9. **P10 — 进度文本去重**（core update_generate_progress_window）
   - 记录 embedded 状态行与百分比行上次 Text，相同则不 SetText；完成/失败/重置等终态强制更新；standalone 字符进度条路径行为不变。

### 阶段 C：Python 两项

10. **P7 — cut_audio 超时**（subfix_asr_transcribe.py）
    - `subprocess.run(..., timeout=max(120, min(600, 60 + 10*duration)))`，duration=source_end-source_start（无 end 时按 60s 估）。
    - 捕获 `subprocess.TimeoutExpired` → 抛 RuntimeError 中文明确信息（含源文件名与区间），由现有错误上抛链路呈现；不改变成功路径输出。

11. **P6 — compose_track_audio 批量切片重写**（subfix_generate_v4.py）
    - 片段按 offset 排序：无重叠段直接 `array` 切片赋值批量落位（含尾部超界切片长度钳制）；仅相邻片段相交的小区间保留逐样本加法+钳制，`overlap_sample_count` 在重叠区内仍逐样本精确计数，保证返回的统计字典逐字段等价。
    - 校验、异常文案、total_samples 计算、补零、统计键全部不变。

## 依赖与注意事项

- Lua 5.1 主 chunk 200 local 上限：新变量尽量挂全局表或用 do...end 块（core 已有此模式）。
- QSS/布局一行不动；timer 全部走现有 register_ui_timer 路由（embedded/standalone 兼容）。
- P6 必须数值等价；P3 必须与原判定逐例等价；P2 以 customData 字符串集合为准。
- 不动 Python helper 版本号与诊断 JSON 既有键（P6/P7 只可能新增错误信息文本）。
- 豆包（P8）本批完全不碰。

## 验证

1. **Python**：`C:\Users\10840\AppData\Local\Programs\Python\Python313\python.exe -m pytest tests/`（含新增 compose 等价性测试：多段/含空洞/重叠/越界/单段，断言输出 WAV 采样与统计字典与参考实现完全一致；以及现有 v4/v5 全部测试）。
2. **Lua 语法**：重建临时 lua5.1.dll 语法校验器，校验 SubFix.lua 与 core 两个文件 rc==0。
3. **部署**：install_subfix.ps1；SHA256 源/部署逐对 MATCH；GetDiagnostics 0。
4. **真机复评清单**（用户完全重启达芬奇后）：
   - 本地 Qwen：开生成面板不再明显卡顿；同段重新生成结果与时间轴对齐不回退（重点复核上批 50/30/25fps 修复仍正常）；
   - 终检：重复执行两次终检，第二次无 marker 闪烁/数量一致；制造一个问题确认增量新增、删掉一个问题确认对应 marker 消失；
   - 播放跟随：暂停/拖动定位与高亮不回退；
   - 日志面板：高频生成时滚动正常、内容不缺、SubFix_debug.log 正常落盘；
   - 空闲时 CPU：等待轮询期间不再出现一个核 100%；
   - 豆包路径不回归（若方便，选中豆包跑一条短段）。

## 风险与兜底

- **P9 误跳树重建导致界面 stale（最高风险）**：所有数据变更入口默认 force，仅轻量/定时刷新走指纹；指纹粒度到每行四字段；真机清单第一项即验证拆分/编辑/更新时间线。
- **P4 崩溃丢尾日志**：1s/64KB/退出钩子三重 flush；缓冲仅用于视图与写盘节流，不改变日志内容。
- **P6 语义偏差**：新单测逐样本比对；重叠区保留原逐样本循环，统计数字保证一致。
- **P3 行序非单调**：每处先校验单调性，不满足自动回退原线性实现。
- **P5 缓存脏读**：signature 纳入 marker/mtime，安装完成强制 invalidate；TTL 仅 8 秒；同步兜底路径原样保留。
- **FFI 风险（P1）**：core 的 kernel32 Sleep 已在同机真机跑通；pcall 包裹、忙等兜底，加载失败零影响。
- 任一项真机异常可单独回退该项（11 项相互独立，无交叉依赖）。
