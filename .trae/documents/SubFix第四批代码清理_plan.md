# SubFix 第四批代码清理实施计划

> 批次目标：**不影响任何现有功能**。只删"零引用 + 无动态调用 + 无字符串反射"的死代码；
> timeline 样板抽取逐点保持错误文案/返回极性/抛错语义完全等价；print 总闸默认开启，行为零变化；
> 打包排除 .pdb，并清扫 TEMP 陈旧残留。
>
> 用户已拍板的三项决策：
> 1. **print：加总闸、默认开启**（232 个调用点不动，行为与现状一致）；
> 2. **TEMP 本机残留：全部删除**（约 191MB，达芬奇完全关闭后执行）；
> 3. **timeline 样板抽取：本批一起做**（已查明实际为 12 个替换点，逐点等价替换）。

---

## 一、调研结论（均已全仓库 grep + 逐行读码确认，行号基于当前文件）

### 1. 死函数（SubFix.lua，共 8 个 ≈ 285 行）

| 函数 | 行号 | 行数 | 确认依据 |
|---|---|---|---|
| `get_fusion_time_raw` | 5721–5757（含 3 行头注释） | 37 | 全仓库仅定义行，零调用 |
| `probe_timeline_playing_state` | 5759–5785 | 27 | 零调用 |
| `playhead_follow_update_estimator` | 5787–5890 | 104 | 零调用（注释自述"旧播放中实时跟随，勿再接线"） |
| `playhead_frame_in_row` | 5892–5901 | 10 | 零调用 |
| `resolve_playhead_poller_paths` | 6163–6212 | 50 | 仅被下面已死的 start 调用（L6216），start 删除后即为死函数 |
| `start_playhead_poller` | 6214–6247 | 34 | 零调用 |
| `read_playhead_poller_sample` | 6262–6276 | 15 | 零调用 |
| `report_playhead_poller_error_once` | 6278–6290 | 13 | 零调用 |

**必须保留（活代码，已核实调用点）**：
- `stop_playhead_poller(quick)` L6249–6260：现役调用点 L6482（关闭跟随）、L19647（退出钩子）。
  作用是清理 `.alive`/`.stop` 信号文件，**函数体一行不动**。
- `playhead_follow_alive_file`、`playhead_follow_tick_count`：L6361–6365 诊断心跳、L6445/6452/6480/L19651 在用。
- `playhead_poller_stop_file`（L1204）、`playhead_poller_started`（L1206）：被保留的 stop 函数体引用（L6251、L6259），声明保留。
- `SUBFIX_VERSION`（L48，更新检查在用）、`SUBFIX_SCRIPT_BUILD`（L1131，LogMsg 在用）。

### 2. 死变量 / 死常量 / 死函数簇（SubFix.lua）

| 目标 | 位置 | 说明 |
|---|---|---|
| `timeline_offset` | L1136 | 仅声明零引用，删整行 |
| `playhead_follow_last_frame` | L1176 | 仅声明零引用 |
| `playhead_follow_last_node` | L1184 | 只写不读：写入点 L6309、清空点 L6444，三处一起删 |
| 旧插值状态机变量块 | L1189–1200（含注释，12 行） | 仅被已死的 estimator 使用，随函数一起删 |
| 轮询器死变量 | L1203、L1205、L1207–1211 | `out_file`/`err_file`/`err_reported`/`last_seq`/`last_seq_sec`/`start_wall`/`no_data_warned` 仅被死的 start/read/report 使用；**保留 L1204、L1206**，并改写 L1201–1202 注释 |
| `refresh_preview_windows` | 声明 L1231 + 定义 L5387–5405 | 零调用，连声明带 19 行函数体删 |
| `switch_stack_page` 簇 | 声明 L1352–1353 + 定义 L4255–4296 | `set_stack_page_active`（L4255–4270）只被同簇死函数 L4294 调用，三者整簇 42 行全删 |
| `history_window` / `history_window_items` | L2345–2346 | 仅声明零引用 |
| `PRE_DELIVERY_SPEECH_*`（15 个常量） | L18284–18298 | 全仓库零引用（旧口播一致性终检链）；**L18279–18283 五个 `PRE_DELIVERY_*` 常量是活的**（L18553–18559 在用），保留 |

### 3. timeline 获取样板（实际比体检列举的更多，已逐一读码分类）

全仓库共 18 处 `GetCurrentTimeline()`，分三类：

- **A 类：14 行标准块 ×9（SubFix.lua 字幕轨族，逐字相同）**，错误文案均为
  "无法获取 Resolve"/"无法获取 ProjectManager"/"没有打开的项目"/"没有时间线"，
  均支持 `timeline_override` 透传，且块后函数体只使用 `timeline`（已 grep 确认无
  resolve/pm/project 尾引用）：
  `get_subtitle_track_items`(8595–8608,返回 nil,err)、
  `ensure_subtitle_track_exists`(8655–8668,nil,err)、
  `get_subtitle_track_state_snapshot`(8686–8699,nil,err)、
  `unlock_all_subtitle_tracks`(8733–8746,false,err)、
  `lock_non_target_subtitle_tracks`(8771–8784,false,err)、
  `isolate_subtitle_target_track`(8809–8822,false,err)、
  `restore_subtitle_track_enabled_states`(8865–8878,false,err)、
  `clear_subtitle_track_clips`(8904–8917,false,0,0,err)、
  `snapshot_subtitle_tracks`(8971–8984,nil,err)。
- **B 类：`refresh_subtitles` L11653–11675（23 行）**：同样 4 条文案，但每步带
  `print("[Hooper AI 2.0] ...")` + `fail_refresh(...)`，替换后 print/状态栏文案逐字保留。
- **C 类：core ×2**（L5425–5430 `generate_selection_subtitles`、L5908–5913
  `refresh_embedded_context`）：6 行防御式链 + `error()`，两处逐字相同
  （"无法获取 Resolve" / "没有打开的时间线"）。

**明确不抽取（异形，强抽会改变用户可见文案或行为）**：
`get_playhead_frame_info` L5689–5696（3 条专属文案 "resolve 未注入"/"ProjectManager 不可用"/"没有当前时间线"）、
报告窗口缓存块 L7502–7513（pcall 内缓存+预热异形）、
L11198–11201（防御式、返回 4 值）、L12386–12400 与 L12819–12834（文案"没有项目"）、
L12731–12739（兼取 mediaPool、fail_selection_update）、L18816–18818（复用外层 resolve）。

### 4. print / 日志现状

- print：SubFix.lua 190 处、core 42 处，输出到 Fusion 控制台，多数带 `[SubFix]`/`[Hooper AI 2.0]` 前缀。
- `LogMsg`（L12212）只写 500 行环形缓冲 + pending 批量落盘，**内部不调用 print** → 控制台总闸与日志面板/文件互不影响。
- core 经 `loadfile`+`pcall(chunk)`（L11857/11875）在同进程加载；Fusion 沙箱下主 chunk 对 `print` 的接管是否波及 core 不能静态确定，但**默认开启时两种情况都等于现状**，无回归可能。

### 5. 打包 / TEMP

- `build_pkg_win.ps1`：L83–86 `Copy-TreeIfExists` 与 L97/L100 下载的 runtime 均整树递归拷贝，
  **无任何 .pdb 过滤**；.pdb 由此进包。
- `build_pkg.sh`（mac）：只显式 cp 源码文件，不拷 runtime/bin/models 树 → 无需改。
- `install_subfix.ps1`：从仓库扁平部署只拷 SubFix.lua + `subfix_*.py` + `.subfix_support\*`；
  仓库 `.subfix_support` 内无 .pdb（Glob 全仓库零 .pdb）→ 无需改。
- Python 临时目录全部用 `tempfile.TemporaryDirectory(prefix="subfix_...")`（7 处），
  正常退出自动清理；进程被强杀/断电才泄漏。main() 位于 subfix_asr_transcribe.py L5646。
- TEMP 实测残留（2026-10-14 实列）：

| 目录 | 大小 | 最后修改 |
|---|---|---|
| `%LOCALAPPDATA%\Temp\SubFixDumps` | 145.6MB | 10/9（ffi 崩溃排查转储，代码无引用） |
| `%LOCALAPPDATA%\Temp\SubFixProcdump` | 4MB | 10/9 |
| `%LOCALAPPDATA%\Temp\subfix_generate_v4_2089314w` | 19.7MB | 10/9（泄漏工作目录） |
| `%LOCALAPPDATA%\Temp\subfix_generate_v4_7ov11qlg` | 19.7MB | 10/9（泄漏工作目录） |
| `%LOCALAPPDATA%\Temp\subfix_backup` | 1.1MB | 10/8 |
| `%LOCALAPPDATA%\Temp\SubFix_GenerateSelectionSubtitles` | 0.7MB | 10/13（core L118 的产品临时根，可自动重建） |
| `%LOCALAPPDATA%\Temp\subfix-update-package-test` | 0.5MB | 10/8 |
| `SubFixV2_测试` / `SubFixWideTest_ascii` / `SubFixWideTest_测试` / `subfix_diag` / `subfix_luacheck` | 0MB | 旧手动测试目录 |

合计约 **191.4MB**。SubFixDumps/SubFixProcdump 在仓库代码中零引用（grep 已确认）。

---

## 二、改动文件清单

- `f:\SubFix\SubFix.lua`：删 8 个死函数 + 死变量/常量/死簇 + 两处死写入；新增 1 个 timeline helper 并替换 10 处；顶部加 print 总闸。
- `f:\SubFix\.subfix_support\subfix_generate_selection_core.lua`：新增 1 个 local helper 并替换 2 处。
- `f:\SubFix\subfix_asr_transcribe.py`：新增 TEMP 7 天陈旧目录清扫函数，main() 开头尽力调用一次。
- `f:\SubFix\tests\test_temp_sweep.py`：新增清扫器单元测试。
- `f:\SubFix\build_pkg_win.ps1`：拷贝完成后递归删除载荷内 `*.pdb` 并打印数量/大小。
- TEMP 一次性删除（上表 11 个目录，用 DeleteFile 工具）。
- **不改**：dist/（历史发布快照）、QSS/布局、豆包流式 P8 链路、任何 Python 识别逻辑、install 脚本、mac 打包脚本。

---

## 三、实施步骤（依赖有序，每步独立可回退）

### 阶段 A：死代码清理（SubFix.lua）

1. 删除连续死函数段 L5721–5901（4 个函数 + 各自注释），保留前后各一个空行，
   后面紧跟的活函数 `get_lineedit_cursor_position`（L5903 注释起）不动。
2. 删除 `resolve_playhead_poller_paths`（L6163–6212）与 `start_playhead_poller`（L6214–6247）；
   **保留** `stop_playhead_poller`（L6249–6260）；删除
   `read_playhead_poller_sample`（L6262–6276）、`report_playhead_poller_error_once`（L6278–6290）；
   把 L6160–6162 段头注释改写为"仅保留 stop 用于写 .stop 清理信号"。
3. 删死变量：L1136；L1176；L1184 + L6309 + L6444 两处死写入；L1189–1200 整块；
   L1203/L1205/L1207–1211（保留 L1204、L1206，更新 L1201–1202 注释）。
4. 删 `refresh_preview_windows`：L1231 声明 + L5386–5406（含前导空行）函数体。
5. 删 switch 簇：L1352–1354 声明 + L4253–4296 整簇定义。
6. 删 L2345–2346；删 L18284–18298（15 个 SPEECH 常量，保留 L18279–18283 与 L18299 空行）。
7. 每删一个符号立即 grep 复核零残留（排除 dist/）。

### 阶段 B：timeline helper 抽取（等价重构）

8. SubFix.lua 在 `get_resolve`（L8565–8571）之后插入全局函数（不占主 chunk local）：

```lua
-- 统一「resolve → ProjectManager → Project → 当前 Timeline」获取链。
-- 错误文案与各历史调用点逐字一致；timeline_override 非空时原样透传。
-- 与旧代码一样不对中间调用加 pcall：异常按原路径向上抛。
function subfix_get_active_timeline(timeline_override)
    if timeline_override then return timeline_override end
    local resolve_obj = get_resolve()
    if not resolve_obj then return nil, "无法获取 Resolve" end
    local pm = resolve_obj:GetProjectManager()
    if not pm then return nil, "无法获取 ProjectManager" end
    local project = pm:GetCurrentProject()
    if not project then return nil, "没有打开的项目" end
    local timeline = project:GetCurrentTimeline()
    if not timeline then return nil, "没有时间线" end
    return timeline
end
```

9. A 类 9 处 14 行块统一替换为（返回极性按各函数原样）：

```lua
local timeline, active_tl_err = subfix_get_active_timeline(timeline_override)
if not timeline then return <原样极性>, active_tl_err end
```

极性：7 处 `nil`/`false` 按原表；`clear_subtitle_track_clips` 为 `false, 0, 0, active_tl_err`。
10. B 类 refresh_subtitles：L11653–11675 替换为 helper + 失败时
    `print("[Hooper AI 2.0] " .. active_tl_err)` + `return fail_refresh(active_tl_err)`，
    四条文案与旧逻辑逐字一致、print 时序不变（在"开始刷新字幕列表..."之后）。
11. core：在 L5423 函数前插入 local helper，替换 L5425–5430、L5908–5913 两处 6 行块：

```lua
-- 统一当前时间线获取链；失败按本模块惯例 error()，由外层 pcall 捕获。
local function get_active_timeline_or_throw()
    local resolve_obj = get_resolve()
    if not resolve_obj then error("无法获取 Resolve") end
    local pm = resolve_obj:GetProjectManager()
    local project = pm and pm:GetCurrentProject()
    local timeline = project and project:GetCurrentTimeline()
    if not project or not timeline then error("没有打开的时间线") end
    return timeline
end
```

等价性要点：pm 为 nil 时 project 为 nil → 与旧代码同样落到"没有打开的时间线"；
无 pcall、error 文案逐字一致；后续 fps/scope 逻辑不动。

### 阶段 C：print 控制台总闸（方案 A，默认开）

12. SubFix.lua 在 JIT 关闭块（L36–43）之后、任何 print 调用之前插入：

```lua
-- 控制台日志总闸：默认开启 = 与历史行为完全一致；需要控制台安静时改为 false 后重启达芬奇。
-- 日志面板环形缓冲与文件落盘（LogMsg）是独立链路，不受本开关影响。
SUBFIX_CONSOLE_LOG_ENABLED = true
do
    local native_print = print
    local function gated_print(...)
        if SUBFIX_CONSOLE_LOG_ENABLED then
            return native_print(...)
        end
    end
    _G.print = gated_print
    print = gated_print
end
```

232 个 print 调用点一律不动。core 是否随沙箱继承接管不影响默认行为，真机记录即可。

### 阶段 D：打包与 TEMP

13. `build_pkg_win.ps1`：在所有二进制拷贝完成后（L125 之后、菜单校验之前）加：

```powershell
# 发布载荷排除调试符号：嵌入式 runtime/bin 自带的 .pdb 体积大且用户不可用。
$PdbFiles = @(Get-ChildItem -Path $CoreSupport -Recurse -File -Filter '*.pdb' -ErrorAction SilentlyContinue)
if ($PdbFiles.Count -gt 0) {
    $PdbBytes = ($PdbFiles | Measure-Object Length -Sum).Sum
    $PdbFiles | Remove-Item -Force
    Write-Host ("已排除调试符号 {0} 个，释放 {1:N1} MB" -f $PdbFiles.Count, ($PdbBytes / 1MB))
}
```

14. `subfix_asr_transcribe.py` 新增（纯标准库、任何异常都不外抛）：
    - `sweep_stale_temp_dirs(root=None, now=None, max_age_days=7)`：扫描系统 TEMP 下
      名字以 `subfix_` 开头的**目录**（小写前缀；SubFixDumps 等大写目录与 Lua 管理的
      `SubFix_GenerateSelectionSubtitles` 不在自动扫描范围），mtime 超过 7 天才删，
      `shutil.rmtree(..., ignore_errors=True)`，返回删除数量；非目录/新目录/非本产品目录一律不动。
    - 在 `main()`（L5646）开头 `try/except Exception: pass` 包一层调用一次；
      清扫绝不能影响识别任务本身。
15. 新增 `tests/test_temp_sweep.py`：tmp_path 造 7 天前的 `subfix_old_*`、刚创建的
    `subfix_new_*`、非本前缀目录与一个 `subfix_file` 文件，断言仅旧目录被删；
    再测一次"全部在阈值内返回 0"。
16. 用户确认达芬奇已完全退出、无相关 python 子进程后，用 DeleteFile 删除调研结论第 5 节
    列出的 11 个 TEMP 目录；若个别项被锁定则跳过并在结果中报告，不强制。

### 阶段 E：验证与部署

17. `python -m pytest tests/`：基线 108 passed / 27 skipped，新增测试后应为 109+ passed。
18. 重建临时 Lua 语法校验器（%TEMP%\subfix_lua_syntax_check.py，ctypes 加载
    Resolve 自带 lua5.1.dll，`luaL_loadfilex` rc==0），校验 SubFix.lua 与 core 两个文件。
    注意：本批新增均为全局函数/do 块，不增加主 chunk local，但删除后仍必须复跑校验。
19. 死符号 grep 终检零残留；GetDiagnostics 0。
20. 运行 `install_subfix.ps1` 部署；逐对 SHA256 MATCH（SubFix.lua、core、py）；删除临时校验器。
21. 提供真机回归清单（见下），用户完全重启达芬奇复评。

---

## 四、真机回归清单（请逐项点一遍）

1. 面板启动正常；字幕列表加载/刷新（refresh_subtitles 新路径，失败文案可在无时间线工程里看一眼）。
2. 选区识别面板打开、「重新检测」、生成选区字幕全流程（core helper 两处）。
3. 字幕轨写回链路：新建字幕轨、独占/锁定/解锁/恢复、清空轨、快照（9 个 A 类函数）。
4. 播放跟随：开关跟随、暂停/拖动后自动定位；退出达芬奇无报错（stop_playhead_poller 路径）。
5. Fusion 控制台日志照常滚动（总闸默认开）；日志面板内容与 SubFix_debug.log 正常。
6. 终检/时长规整等读取时间线的既有入口表现不变。

---

## 五、风险与兜底

- **helper 抽取是本批唯一行为面改动**：文案逐字保留、返回极性逐点保持、无 pcall 的抛错
  路径保持一致、块后无 resolve/pm/project 尾引用（已 grep）；每个站点可独立回退
  （helper 留着不影响其他站点）。
- **死代码误删**：每个符号均经"定义行 + 全仓库零调用 + 无字符串反射"三重确认；
  事件 ID、ui_timer_handlers 路由、find_window_item 字符串访问一律视为活代码；
  stop_playhead_poller 等活符号已显式保留。
- **Lua 5.1 主 chunk 200 local 上限**：新增函数全部用全局函数或 do 块，不新增主 chunk local。
- **print 总闸**：默认 true，等价于现状；LogMsg 不经 print，日志面板/落盘不受影响。
- **TEMP 删除**：达芬奇完全退出后执行；锁定项跳过；`SubFix_GenerateSelectionSubtitles`
  会在下次生成时自动重建。
- 不 git commit；不建文档（本计划文件除外）；不碰 dist/、QSS/布局与豆包 P8。
- 预计 SubFix.lua 净减约 290 行，core 净减约 8 行。
