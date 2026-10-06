---
name: 修复 SubFix Lua 诊断告警
overview: 工作区"问题"面板共 752 条 Lua 诊断，经核查全部为静态分析噪声、不影响达芬奇中的正常运行。本计划用零运行风险的配置 + 清理残留备份 + 两处无害微调，将告警降至约 0，并重新部署使微调生效。
todos:
  - id: add-luarc-json
    content: 新增 .luarc.json 声明 Lua 5.1 与全局（用 [subagent:code-explorer] 枚举全局名单）
    status: completed
  - id: delete-backups
    content: 删除 SubFix.lua.bak 与 SubFix.lua.bak_top_locals 残留备份
    status: completed
  - id: tweak-core
    content: 微调 core 文件：math.pow 改 ^ 运算符、4716 行加诊断抑制注解
    status: completed
  - id: redeploy
    content: 重新部署 core 文件到 Resolve 插件目录
    status: completed
    dependencies:
      - tweak-core
  - id: verify
    content: 重启语言服务器确认告警清零并可选语法校验
    status: completed
    dependencies:
      - add-luarc-json
      - delete-backups
      - tweak-core
      - redeploy
---

## 用户需求

- 判断工作区「问题」面板中 752 条 Lua 诊断是否影响插件在达芬奇中的正常使用。
- 拟定并实施修复计划，消除告警。

## 核查结论（是否影响正常使用）

经逐条核查，752 条诊断全部为 IDE 静态分析噪声，不影响运行：

- 约 749 条 `undefined-global`（`fu`/`fusion`/`bmd`/`resolve`/`res`/`app` 等）：达芬奇/Fusion 在运行时注入这些宿主全局，Lua 语言服务器不认识，属误报。
- 2 条 `lowercase-global`（`subfix_generate_selection_core.lua:4716`）：有意使用全局变量，规避 Lua 5.1 的 200 个 local 上限，属设计意图。
- 1 条 `deprecated`（`subfix_generate_selection_core.lua:2006` 的 `math.pow`）：Lua 5.1 仍支持，仅过时写法。
- 1 条 `duplicate-set-field`（`SubFix.lua:459`）：由残留备份 `SubFix.lua.bak_top_locals` 重复定义同一全局字段被一并索引所致。

## 核心特性（修复动作）

- 新增 Lua 语言服务器配置声明全局变量，清零 `undefined-global`。
- 删除两个残留备份文件，清零 `duplicate-set-field`。
- 对 core 文件做两处无害微调（`math.pow`→`^`、有意全局加诊断抑制注解）。
- 重新部署并重启达芬奇使微调生效，验证告警清零。

## 技术栈

- 运行环境：DaVinci Resolve / Fusion 内置 Lua 5.1（宿主注入 `fu`/`fusion`/`bmd`/`resolve` 等全局）。
- 静态分析：Lua Language Server（sumneko），通过项目根 `.luarc.json` 配置。
- 部署：PowerShell 复制 core 文件到 `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Comp\SubFix\`。

## 实现方案

- 新增 `.luarc.json`：设置 `"runtime": { "version": "Lua 5.1" }`；`"diagnostics": { "globals": [...] }` 列出全部宿主全局（`fusion`/`bmd`/`resolve`/`res`/`fu`/`app`/`comp`/`composition`/`ui`/`disp` 等）与项目自定义全局（`SUBFIX_WINDOW_GEOMETRY`/`SUBFIX_IS_WINDOWS`/`DEFAULT_ASR_BACKEND` 等），消除 `undefined-global`。
- 删除残留备份文件，消除 `duplicate-set-field`。
- 微调 core 文件：第 2006 行 `math.pow(10, x)` 改为 `10 ^ x`（语义不变）；第 4716 行上方加 `---@diagnostic disable-next-line: lowercase-global`（该行为有意全局赋值，跨循环持久化）。

## 实现细节

- 全局名单由 `[subagent:code-explorer]` 扫描两个主 Lua 文件顶层非 local 赋值/宿主调用枚举补全，避免遗漏真实 `undefined-global`。
- `math.pow` 改动触达部署文件，需重新复制 core 并重启达芬奇；`.luarc.json` 与删备份仅影响 IDE，不改变运行行为。
- 严格保留现有控件 ID 与业务逻辑，仅做静态分析与两处无害写法调整。

## 目录结构

```
f:\SubFix-main\SubFix-main\
├── .luarc.json                     # [NEW] Lua 语言服务器配置：runtime.version=Lua 5.1；diagnostics.globals 列出宿主与自定义全局
├── SubFix.lua.bak                  # [DELETE] 残留备份，删除以清除 duplicate-set-field
├── SubFix.lua.bak_top_locals       # [DELETE] 残留备份，删除以清除 duplicate-set-field
└── .subfix_support\
    └── subfix_generate_selection_core.lua  # [MODIFY] 第2006行 math.pow(10,x)→10^x；第4716行上方加 ---@diagnostic disable-next-line: lowercase-global
```

## Agent Extensions

### SubAgent

- **code-explorer**
- Purpose: 扫描 `SubFix.lua` 与 `subfix_generate_selection_core.lua`，枚举所有顶层非 local 赋值与宿主全局引用，补全 `.luarc.json` 的 `globals` 名单，避免遗漏真实 `undefined-global` 告警。
- Expected outcome: 输出一份完整的全局变量清单，供 `.luarc.json` 精确配置，使重启语言服务器后 `undefined-global` 告警清零。