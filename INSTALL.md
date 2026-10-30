# SubFix v3.3.2 安装说明（Windows）

本发行包为**轻量版**：包含插件本体（Lua 入口 + Python 助手脚本）与安装/卸载脚本。
Qwen 强制对齐模型与本地识别依赖**在首次使用时自动下载**（魔搭 ModelScope 优先，Hugging Face 兜底），无需手动放置。

## 一、前提条件

- 已安装 **DaVinci Resolve**（达芬奇）或 **AviUtl**，并能加载外部脚本/插件。
- 系统已安装 **Python 3.12 / 3.13**，安装时请勾选「Add Python to PATH」。
  - 插件首次运行会用系统 Python 在 `%LOCALAPPDATA%\SubFix\envs\qwen-local` 自动创建虚拟环境并安装依赖。
- 首次使用需要**联网**（用于下载 Python 依赖与 Qwen 强制对齐模型）。

## 二、安装步骤

1. 从 Releases 下载 `SubFix_v3.3.2_Windows.zip` 并解压。
2. 进入解压出的 `SubFix_v3.3.2_Windows` 文件夹，**右键 `install_subfix.ps1` → 用 PowerShell 运行**
   （普通用户安装到当前用户；以管理员运行则安装到全用户目录）。
3. 重新启动 DaVinci Resolve（或 AviUtl）以刷新脚本菜单。
   - DaVinci Resolve：插件出现在「工作区 ▸ 脚本」中。
   - AviUtl：按对应方式加载 `SubFix\SubFix.lua`。
4. 首次使用「文稿匹配 / 本地 Qwen 识别」时，插件会联网自动：
   - 用清华 PyPI 镜像安装 Qwen 依赖；
   - 下载 Qwen3 强制对齐模型（约几百 MB）到 `%LOCALAPPDATA%\SubFix\models`。
   进度会显示在插件界面，下载源与已下载文件都会保留。

## 三、卸载

运行同目录下的 `uninstall_subfix.ps1` 即可移除插件本体。
（已下载的依赖与模型位于 `%LOCALAPPDATA%\SubFix`，可手动清理。）

## 四、关于「完整包」

本仓库的 Windows 标准发行本为轻量版，不内置 Python 运行时 / FFmpeg / qwen3-asr-cli 二进制；
这些由首次使用时的自动下载流程补齐，因此本包即可**全新安装直接使用**。
若需要离线完整包（内置上述二进制），请用仓库 `build_pkg_win.ps1` 并提供对应二进制后自行构建。

## 五、Qwen 模型说明

- 强制对齐模型：`qwen3-forced-aligner-0.6b-f16.gguf` 等，由插件自动下载。
- 官方模型说明与下载：[Qwen3-ASR Released Models](https://github.com/QwenLM/Qwen3-ASR#released-models-description-and-download)
  （推荐中国大陆用户使用 ModelScope 镜像，插件已内置该回退逻辑）。

## 六、故障排查

- 安装后脚本菜单看不到 SubFix：确认以管理员/普通用户身份与 `install_subfix.ps1` 目标目录一致，并重启达芬奇。
- 首次识别卡在下载：检查网络与国内镜像可用性；已下载文件会缓存，可重试。
- 仍不可用：查看 `%USERPROFILE%\subfix_remap_debug.log` 或插件内日志，按提示安装 Python 依赖。
