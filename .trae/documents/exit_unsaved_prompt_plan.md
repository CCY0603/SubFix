# 退出前「未应用修改」提醒 实施计划

## 需求
点击主窗口「退出」按钮**或**右上角 X 时，若存在已修改但尚未写回时间线的字幕，弹出确认窗，提供两个选项：
- **更新到时间线**（主按钮，满橙）→ 执行写回后退出；
- **取消修改**（幽灵按钮）→ 放弃内存中的全部修改，直接退出。
弹窗视觉与 Quiet Pro 改版后的主界面一致。

## 调研结论（Repository Research）

### 退出链路（现状：无确认直接退）
- 「退出」按钮：[ForceQuitBtn L16104](file:///f:/SubFix/SubFix.lua#L16104) → [win.On.ForceQuitBtn.Clicked L19011](file:///f:/SubFix/SubFix.lua#L19011) → [force_quit_subfix() L18925](file:///f:/SubFix/SubFix.lua#L18925)（停轮询/定时器、取消 AI、kill curl、关子弹窗、win:Hide、5×ExitLoop）。
- 右上角 X：[win.On.HooperAI_v2_compact_narrow500_final_h960.Close L19006](file:///f:/SubFix/SubFix.lua#L19006) → [handle_main_window_close() L18914](file:///f:/SubFix/SubFix.lua#L18914) → 同一个 force_quit_subfix。
- **关键便利点**：X 触发 Close 后窗口会自行隐藏，而两个选项最终都要退出，因此**无需拦截/否决关闭事件**——在 Close 回调里直接弹独立确认窗即可，不存在"取消关闭"的时序难题。

### 修改数据流
- 一切编辑只改内存 `current_rows`，时间线在点「更新时间线」前不变（所以"取消修改"无需回滚 Resolve，直接退出即可）。
- 备份只在更新流程内发生（[persist_timeline_update_backup 调用点 L3051 附近/UpdateBtn L18828](file:///f:/SubFix/SubFix.lua#L18828)），放弃修改不会留下多余备份。
- 写回：[update_timeline L12423](file:///f:/SubFix/SubFix.lua#L12423)（选区模式转 `update_timeline_selection_scope` L12375 同构路径），不依赖主窗口可见（直接操作 Resolve timeline 对象）。
- 重新载入：[refresh_subtitles L11334](file:///f:/SubFix/SubFix.lua#L11334)，启动时自动加载走同一套。
- `PendingChanges`（[L1131](file:///f:/SubFix/SubFix.lua#L1131)）只覆盖 AI 待审核建议；已批准建议在 update 内部经 `apply_approved_pending_changes_to_rows` 合并。脏标记需要更广，单独建。

### 内存修改入口（脏标记埋点位置）
1. 手动编辑保存：[save_subtitle_edit_box L5526](file:///f:/SubFix/SubFix.lua#L5526)（文本确实变化时）。
2. 拆分：[split_current_subtitle_row L5814](file:///f:/SubFix/SubFix.lua#L5814)。
3. 批量替换：[win.On.BatchReplaceBtn.Clicked L16902](file:///f:/SubFix/SubFix.lua#L16902)（实际发生替换时）。
4. 精修工具按钮（在确认并实际改行后埋点）：BtnStep3 规整长度 L17634、BtnStep2 中文数字 L17480、BtnStep1 英文排版 L17831、BtnStep6 敏感词 L18387、BtnStep7 清空行 L18549；BtnStep4 最终交付检查、BtnStep8 提取纯文本为只读检查，不埋点。
5. AI 处理完成：存在自动应用行或待审核建议时置脏（批量完成回调处；待审核建议在「更新时间线」时才合并，所以 AI 只要产出过建议就算脏，防止用户批准后退出不提醒）。

### 可复用弹窗范式
[show_subfix_update_confirm L18736-L18752](file:///f:/SubFix/SubFix.lua#L18736-L18752)：`dispatcher:AddWindow` + 按钮回调 `Hide + ExitLoop` + `dialog:Show(); dispatcher:RunLoop()` 返回用户选择。新弹窗照此实现，挂 `SUBFIX_MAIN_ROOT_STYLESHEET`，按钮用 `SUBFIX_BTN_ACCENT` / `SUBFIX_BTN_SECONDARY`，Geometry 用 `SUBFIX_WINDOW_GEOMETRY.centered_geometry`。

## 改动文件
仅 [SubFix.lua](file:///f:/SubFix/SubFix.lua)。

## 实施步骤

1. **脏状态全局变量与辅助函数**（全局声明区，约 L1131 附近；函数放在 force_quit 之前）：
   - `subfix_timeline_dirty = false`；
   - `mark_subfix_timeline_dirty()` / `clear_subfix_timeline_dirty()`（带 pcall、print 一行日志便于真机验证）。

2. **埋点**：
   - `clear`：`refresh_subtitles` 成功完成处、启动自动加载完成处、`update_timeline` 与 `update_timeline_selection_scope` 成功写回末尾；
   - `mark`：上述 5 类修改入口（只在确有改动时置位；AI 为批次完成且有产出时置位）。

3. **确认弹窗** `show_subfix_unsaved_changes_dialog()`（仿 L18736 范式）：
   - 尺寸约 440×230，WindowTitle「未应用的修改」，根样式 `SUBFIX_MAIN_ROOT_STYLESHEET`；
   - 文案：标题「有字幕修改尚未更新到时间线」+ 说明「退出后这些修改将丢失（时间线保持原样）。可立即更新到时间线，或放弃修改退出。」；
   - 两个按钮：`更新到时间线`（`SUBFIX_BTN_ACCENT`，Weight 1）、`取消修改`（`SUBFIX_BTN_SECONDARY`，Weight 1，Tooltip「放弃全部未写回修改并退出」）；
   - 返回值 `"apply"` / `"discard"`；**弹窗自身的 X/Close 返回 `"cancel"`**（保护性默认：误关弹窗=回到插件，不增加第三个按钮）。

4. **退出闸门** `request_subfix_exit()`：
   - 忙时（`AI_RUNNING`、选区生成进行中经 core 查询、Normalize 长任务进行中）或 `not subfix_timeline_dirty` → 直接 `force_quit_subfix()`（保持现状）；
   - 脏且空闲 → 弹窗：
     - `apply`：先 `win:Show()`（让用户看到状态栏进度），`update_timeline(win)`，随后 `force_quit_subfix()`；
     - `discard`：直接 `force_quit_subfix()`；
     - `cancel`：不做任何事（主窗已被 X 隐藏则 `win:Show()` 恢复）。
   - 加重入保护：`subfix_exit_prompt_open` 标志，避免 Close 与按钮重复触发叠窗。

5. **接线**：`ForceQuitBtn.Clicked` 与 `handle_main_window_close` 改调 `request_subfix_exit()`。

## 依赖与注意
- 不改 Resolve 侧任何行为；"取消修改"依赖"编辑只在内存"这一既有事实，无需回滚 API。
- AI 待审核建议：选"更新到时间线"走现有合并逻辑（仅已批准项生效，与现在手动点更新完全一致）；选"取消修改"随进程退出丢弃。
- 弹窗用嵌套 RunLoop，而 force_quit 已连续 5 次 ExitLoop，能正确弹出嵌套层；`apply` 分支先 Hide 弹窗并 ExitLoop 一次再执行更新，避免更新过程中弹窗仍挂着。

## 验证
1. Lua 语法校验（TEMP 下 lua5.1.dll luaL_loadfilex）→ install_subfix.ps1 部署 → SHA256 MATCH → GetDiagnostics 0。
2. 真机四场景：
   - 无修改时点退出/X：直接退出，无弹窗（回归现状）；
   - 改一条字幕后点退出：弹窗出现 →「更新到时间线」后重启插件，修改已在轨道上且 dirty 已清；
   - 改一条字幕后点 X：弹窗 →「取消修改」→ 重启插件，时间线为旧内容；
   - 弹窗 X（误触）：主窗恢复，不退出；
   - AI 跑批中点退出：不弹窗、立即终止（现状不回归）。
3. 同步更新 HTML 效果图中无涉及（弹窗可在实现后补一张静态稿，非必须）。

## 风险
- **埋点遗漏导致该提醒不出现**：精修工具各按钮均有独立 handler，已逐一列出；真机按第 2 节清单逐功能点一次确认脏标记日志。
- **update_timeline 提前 return（无项目/无数据）**：apply 分支更新失败仍退出会丢修改——对策：apply 分支主窗已重新 Show 可见，错误会出现在状态栏与控制台；实现时让 update 成功路径清 dirty，退出前再判一次 dirty，仍脏则不退出（留在主窗），把失败交给用户看到。
- 选区模式写回路径单独清 dirty，已在步骤 2 覆盖。
