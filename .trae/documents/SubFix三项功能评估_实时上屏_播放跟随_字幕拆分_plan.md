# SubFix 三项功能评估与实现方案

## 仓库调研结论（基于现有代码机制）

### 现状关键事实
- **字幕写回时间线只有一条通道**：[update_timeline](file:///f:/SubFix/SubFix.lua#L11561) 把内存 `current_rows` 全量导出 SRT → 解锁/隔离所有字幕轨 → **清空目标轨全部片段** → `mediaPool:ImportMedia` → `AppendToTimeline` → 校验快照 → 恢复播放头（选区模式走 `update_timeline_selection_scope`，同样是重导入）。
- Resolve 脚本 API **不提供单条字幕文本的 setter**（TimelineItem 可写属性不含字幕文本，全项目无 SetProperty 用法）——这正是本插件只能整轨 SRT 重导入的原因。
- 编辑区 [EditSubtitleText](file:///f:/SubFix/SubFix.lua#L15473) 是单行 `LineEdit`；点"保存修改"只改内存 row（[save_preview_edit_dialog_changes](file:///f:/SubFix/SubFix.lua#L5245)），列表即时刷新，时间线不动。
- 已有 `ui:Timer` 轮询基础设施（[register_ui_timer](file:///f:/SubFix/SubFix.lua#L6900)，AI/生成流程在用 50–150ms 定时器）。
- 播放头可读：`timeline:GetCurrentTimecode()`；正反向时间码↔帧换算齐全（[timecode_to_frame_count](file:///f:/SubFix/SubFix.lua#L11041)、[resolve_subtitle_navigation_timecode](file:///f:/SubFix/SubFix.lua#L11092)）；row 存的是含时间线起点的绝对帧。
- 树选中已有多通道兜底 [set_tree_current_item](file:///f:/SubFix/SubFix.lua#L4473)；row→树节点映射 `subtitle_row_id_node_map` 常驻。
- 内存行增删改有成熟基建：[rebuild_tree_from_rows](file:///f:/SubFix/SubFix.lua#L10864)（自动排序/重编号/重建 id/刷新树）、撤回快照 `prepare_mutation_snapshot/commit_mutation_snapshot`、UTF-8 字数工具 `count_utf8_chars`。
- Resolve API 无"是否正在播放"查询；可用"相邻两次采样时间码是否前进"自行判定。

---

## 功能 1：编辑字幕后实时上屏到字幕轨

### 评估结论：不建议做"逐字实时"；可提供"保存后自动写回"折中档
- **逐键上屏不可行**：每次按键都触发整轨清空+SRT 重导入，Resolve 侧单次约 0.3–1.5s 同步阻塞 UI 线程；播放大概率被打断、字幕轨闪烁、Resolve 撤销栈被写爆、媒体池/轨道锁定切换抖动。脚本侧开销可忽略，成本几乎全在 Resolve 重导入。
- 真正的"实时"需要 API 支持单条改字，当前 Resolve 版本不具备（实现前可用 `TimelineItem:SetProperty("Subtitle Text"/"Text", …)` 做一次真机探针确认，但预期不支持）。

### 可选折中实现（推荐档位）
**方案 A（推荐）：编辑框"即时保存到内存"**
- 监听 `EditSubtitleText.TextChanged`（去抖 ~400ms）：自动把文字写入当前 row 并刷新列表，零 Resolve 开销；用户只需最后点一次"更新时间线"。
- 成本极低、零风险；解决"列表里看不到改动"的割裂感。

**方案 B：开关式自动写回（暂停时、停止输入 1.2s 后）**
- 新增"自动上屏"复选框；仅在检测到时间线暂停 + 编辑停止去抖 1.2s 后调用现有 `update_timeline`（选区模式走选区版），写回期间禁用编辑框、失败回退手动按钮。
- 复用现有备份/校验/播放头恢复逻辑；需要真机验证：播放中写回是否打断播放、重导入闪烁程度。
- 不做逐键、不做播放中自动写。

### 改动文件
- `SubFix.lua`：编辑区加复选框/去抖 Timer（可复用 `search_debounce_timer` 模式）；TextChanged 接线；写回互斥标志（更新时间线/生成进行中时跳过）。

### 风险
- 播放中断/闪烁：用"仅暂停 + 去抖 + 开关默认关"规避；真机确认。
- 频繁备份膨胀：自动写回复用按钮的备份策略，必要时自动写回不额外做 DoBackup。

---

## 功能 2：列表跟随时间线播放自动跳转

### 评估结论：可实现，性能影响极小，风险低（首选实现）
- 一个常驻 `ui:Timer`（建议 150–200ms）：读 `GetCurrentTimecode()` → 换算绝对帧 → 在已排序的 current_rows 里线性/二分找 `start_frame <= 帧 < end_frame` 的 row → 命中变化时 `set_tree_current_item` 选中并自动滚动。
- 单次 tick 只有 1 次只读 API + 52~几百行的扫描，CPU 可忽略；不产生项目状态修改。
- "播放中"判定：连续两次采样时间码前进（差 ≈ interval×fps）即播放中；相同即暂停——**跟随只在播放中触发**，暂停时把列表控制权还给用户，不抢手动选择。
- 保护：更新时间线/生成/弹窗/搜索过滤视图/正在编辑时暂停跟随；切时间线/换轨时重置基准。

### 改动点（均在 SubFix.lua）
1. 新增全局 `playhead_follow_timer`（ui:Timer，Interval 180ms）+ 上次时间码/上次选中 row_id 状态。
2. 新增 `playhead_timecode_to_frame(timeline)`：复用 `timecode_to_frame_count` 与 `GetStartTimecode/GetStartFrame` 做反算（go_to_subtitle 的逆运算）。
3. 新增 `find_row_at_frame(frame)`：current_rows 已排序，顺序扫描即可（规模小）。
4. tick 处理：播放中且 row 变化 → 取节点映射 → `set_tree_current_item`；不重渲染整树、不改编辑框内容、不触发 ItemClicked 副作用。
5. UI：字幕树标题栏或工具栏加开关按钮"▶ 跟随播放"（默认开或默认关待定，建议默认关，避免意外）。
6. 互斥：`update_timeline` 前后、core 生成期间、弹窗模态时置 `follow_suspended=true`。

### 真机验证点
- `SetCurrentItem` 是否自动滚动到目标行（Qt 通常会；若不会，再查 Tree 是否有 ScrollToItem 类方法兜底）。
- 180ms 间隔在 500+ 行时间线上是否足够跟手（可降到 120ms）。

---

## 功能 3：一条字幕拆分为两条

### 评估结论：实现方便，纯内存操作 + 既有写回链路，风险低
- 不动 Resolve：拆分只改 `current_rows`，之后用户点"更新时间线"自然以两条写轨（导出逻辑天然支持任意行数）。
- 复用 `rebuild_tree_from_rows`（自动排序、重编号、重建 id、刷新树）和撤回快照，天然支持"撤回"。

### 交互与算法
1. 编辑按钮行新增"✂ 拆分"按钮，选中行且时长 ≥ 2 帧时可用。
2. **拆分时间点**：播放头若落在该行 [start, end) 内，用播放头帧（用户把播放头停在哪就从哪拆，所见即所得）；否则取中点帧。
3. **拆分文字**：按拆分时间占比算字符位置（UTF-8 安全，复用 `count_utf8_chars` 与现有 UTF-8 模式），并在 ±2 字内吸附到最近的标点/空格边界；两半 trim 后任一为空则中止并提示。
4. 产出：A = 原行 text 前半、[start, split)；B = 新行 text 后半、[split, end)；插入 current_rows 原位置；快照入撤回栈；rebuild；选中 B 行装入编辑框；状态提示"已拆分，点更新时间线写回"。
5. 不改 fps/轨道等字段；搜索缓存随 rebuild 自动失效。

### 改动文件
- `SubFix.lua`：编辑区加按钮（[SubtitleEditButtons](file:///f:/SubFix/SubFix.lua#L15480) 行）、`split_current_subtitle_row()` 函数（含播放头取帧、时长守卫、UTF-8 拆分+标点吸附）、按钮事件绑定、按钮启用状态随选中行刷新。

### 风险
- 文字按比例拆可能切断词：用标点/空格吸附 + 拆分后立即装入编辑框方便微调，可接受；未来可加"合并相邻行"作为配套（本次不做）。
- 选区模式下拆分产生的行必须留在选区范围内：拆分点天然在行区间内，无越界问题。

---

## 建议实施顺序与组合
1. **功能 3（拆分）**：独立、最简单、立刻可用。
2. **功能 2（播放跟随）**：独立、低风险，体验提升大。
3. **功能 1 取方案 A（编辑即存内存）**：零风险消除"改了没反应"的割裂；方案 B（自动上屏）作为开关项后续真机评估。

## 验证
- Lua 解析 rc=0（ctypes 直驱 lua5.1.dll）、`pytest tests -q` 基线 87 passed/27 skipped、GetDiagnostics=0。
- install_subfix.ps1 部署后 SHA-256 核验 SubFix.lua。
- 真机：②播放跟随与滚动、③拆分/撤回/写回轨后为两条、①A 编辑即时反映到列表。

## 待你确认
- 三个功能做哪几个（建议 3 + 2 + 1A）。
- 功能 2 跟随开关默认开还是默认关（建议默认关）。
- 功能 1 是否需要方案 B（暂停时自动写回时间线）。
