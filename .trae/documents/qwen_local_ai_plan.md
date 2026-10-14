# AI 工作台一键切换本地 Qwen 实施计划

## 需求（用户反馈后修订）
在 AI 工作台加**一个按钮**，一键切换到本地安装的 Qwen 模型，**用户不做任何配置**（不填 URL、模型名、API Key）；再点可切回云端。

## 调研结论（Repository Research）

### 可复用的现成机制（全部在 [SubFix.lua](file:///f:/SubFix/SubFix.lua)）
- 4 个功能（完整纠错 / 的地得 / 中译英 / 简繁转换）共用 OpenAI 兼容通道：curl POST `/v1/chat/completions`，[execute_ai_request L14593-L14692](file:///f:/SubFix/SubFix.lua#L14593-L14692)。
- Provider 表 [AI_PROVIDER_DEFS L1788-L1834](file:///f:/SubFix/SubFix.lua#L1788-L1834)，切换函数 [switch_ai_provider L4215-L4240](file:///f:/SubFix/SubFix.lua#L4215-L4240)（保存当前配置→换 id→同步下拉→回填设置窗），可直接调用。
- 配置存取 `SaveProviderConfig` / `LoadConfig`，启动时 `current_ai_provider_id = LoadActiveProviderId()`（[L19094](file:///f:/SubFix/SubFix.lua#L19094)）。
- JSON 解析器 [decode_json_text L1527](file:///f:/SubFix/SubFix.lua#L1527)，可解析模型清单接口返回。
- Ollama 与 LM Studio 均提供 OpenAI 兼容的模型清单接口 `GET /v1/models`（返回 `{data:[{id:"qwen2.5:14b",...}]}`），可用现有 curl + 临时文件模式探测。

### 需要改的两个硬编码
1. API Key 强制非空：[L12753-L12757](file:///f:/SubFix/SubFix.lua#L12753-L12757) —— 本地服务不需要 key。
2. curl `--max-time 180`：[L14691](file:///f:/SubFix/SubFix.lua#L14691) —— 本地大模型冷启动（首次加载 14B/32B 权重）需要放宽。

## 实现方案

仅改 [SubFix.lua](file:///f:/SubFix/SubFix.lua) 一个文件。

### 1. 新增内置 provider（约 L1826 的 AI_PROVIDER_DEFS 表内）
```lua
{ id="local_qwen", label="本地 Qwen（一键）", api_url="", default_model="",
  is_custom=false, protocol="openai_compatible",
  local_no_key=true, local_timeout=600 }
```
（`local_no_key` / `local_timeout` 为新增字段，老 provider 不带这两个字段，行为不变。）

### 2. 新增一键按钮（AI 工作台 tab，现有 API 设置/任务执行按钮同区）
- 未激活时文字：`🖥 一键本地 Qwen`；当前已是本地 provider 时文字：`☁ 切回云端`；启动时按 `current_ai_provider_id` 初始化状态。
- Tooltip：`需启动 Ollama（推荐 ollama pull qwen2.5:14b）或 LM Studio 本地服务；自动探测，无需填写任何配置`。

### 3. 点击逻辑 `activate_local_qwen()`（新函数）
1. **探测**：curl GET 依次尝试 `http://localhost:11434/v1/models`、`http://localhost:1234/v1/models`（`--connect-timeout 2 --max-time 5`，结果写临时文件，复用现有 shell_quote/临时文件模式）。
2. **选模型**：`decode_json_text` 解析 `data[].id`，筛名字含 `qwen` 且排除 `embed`/`vl`/`audio`；按参数量评分选最优：32b > 14b > 7b > 其他，同参数优先带 `instruct` 的。
3. **命中** → `SaveProviderConfig("local_qwen", {api_url="<端点>/chat/completions", model="<选中模型>", api_key="local"})` → `switch_ai_provider("local_qwen")` → 状态栏提示 `已切换本地 Qwen：qwen2.5:14b（Ollama），4 个 AI 功能均将走本地`；同时把切换前的云端 provider 记到全局 `provider_before_local_qwen`（仅本次会话有效）。
4. **服务在线但无 Qwen 模型** → 不切换，状态栏：`已连接 Ollama，但未找到 Qwen 模型，请先执行 ollama pull qwen2.5:14b`。
5. **两个端点都不通** → 不切换，状态栏：`未检测到本地模型服务，请先启动 Ollama 或 LM Studio（推荐模型 qwen2.5:14b）`。
6. **切回云端** → `switch_ai_provider(provider_before_local_qwen or "siliconflow")`，按钮文字复位。

### 4. 运行 AI 任务路径的配套放行
- API Key 校验（L12753）：`provider_def.local_no_key` 为真时跳过非空校验；curl 的 Authorization 头空 key 时回退 `Bearer local`（Ollama/LM Studio 均忽略）。
- 超时：curl 的 `--max-time` 取 `provider_def.local_timeout or 180`；`--connect-timeout` 本地取 5。
- 兜底：若用户手动在设置里选了「本地 Qwen（一键）」但从未探测过（api_url 为空），点执行 AI 时自动先跑一次第 3 步探测；探测失败给出同样的明确提示，不发空请求。
- 本地请求若遇连接失败（HTTP 000），错误文案提示"本地模型服务未运行或模型未加载"，与云端鉴权/网络错误区分开。

### 5. 不做的事
- 不捆绑安装 Ollama/LM Studio，不自动下载模型（14B 约 9GB，不经脚本静默拉取）；只给明确指引。
- 不改 4 个功能的提示词、批处理、结果解析与状态机。
- 不新增端口、外部进程或 Python 辅助文件；探测与请求仍走现有 curl。

## 前置条件说明（交付时告知用户）
- 本机需安装并启动 **Ollama**（`ollama pull qwen2.5:14b`，约 10GB 显存体验最佳；8GB 以下显存可选 `qwen2.5:7b`，简繁/的地得足够，纠错/翻译质量弱于 14B），或使用 **LM Studio** 加载 Qwen2.5 GGUF 并开启 Local Server。
- 模型选择为自动择优：拉了哪个 Qwen 就用哪个，32b>14b>7b。

## 验证（Validation）
1. Lua 语法校验（TEMP 下 lua5.1.dll luaL_loadfilex 临时脚本）→ `install_subfix.ps1` 部署 → SHA256 MATCH → GetDiagnostics 0。
2. 真机（用户配合，Ollama 已装）：
   - 点按钮：正确探测到已 pull 的 Qwen 并切换，状态栏显示模型名与服务类型；
   - 4 个功能各跑 5~10 条小批：返回行数一致、结果正常写回、橙条流程不受影响；
   - 关掉 Ollama 再点按钮/跑任务：提示文案准确，不写坏字幕；
   - 点"切回云端"：provider 下拉与设置窗恢复原云端配置，云端跑批回归正常；
   - 重启达芬奇：本地选择持久化，按钮状态正确，无需重新探测（配置已保存）。
3. 回归：云端各 provider 默认超时 180s、Key 校验、Gemini 分支均不受影响。

## 风险与对策
- **Ollama 首次加载模型耗时长**：本地超时已放到 600s；加载后模型常驻显存，后续请求正常。
- **只 pull 了 7B，完整纠错红线遵循弱于云端**：不报错但可能多改/少改——状态栏与 tooltip 已说明推荐 14B；解析器对行数不符本就有校验，不会写坏字幕。
- **端点/模型 id 解析差异**：Ollama 的 OpenAI 兼容层与 LM Studio 均返回标准 `data[].id`；评分函数只取 token 中的数字+b 后缀，异常 id 兜底取第一个含 qwen 的模型。
- **模型名含冒号（qwen2.5:14b）**：仅作为 JSON 字符串，现有 `escape_json` 已覆盖。
