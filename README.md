# SubFix v3.3.0

DaVinci Resolve 字幕插件。口播、现场、单段和批量字幕生成统一使用 v5。

## 安装

从 [GitHub Releases](https://github.com/HooperH/SubFix/releases/latest) 下载完整安装 ZIP，解压并打开 pkg。支持 Apple Silicon Mac，沿用未签名安装方式。首次使用本地 Qwen 识别仍需联网安装识别依赖和模型。

### Windows 安装

SubFix 同样支持 Windows 版达芬奇。代码已全面跨平台化（临时目录、用户目录、达芬奇脚本目录、venv Python、`curl` 下载、模型与环境缓存、打开外部链接等均改用 Windows 路径与命令）。

1. 下载 `SubFix_v*_Windows.zip` 并解压，得到 `SubFix/`、`install_subfix.ps1`、`uninstall_subfix.ps1` 等。
2. 右键 `install_subfix.bat`（或 `powershell -ExecutionPolicy Bypass -File install_subfix.ps1`）运行；普通用户部署到 `%APPDATA%`，以管理员运行（或加 `-AllUsers`）部署到 `%ProgramData%`。
3. 重新启动达芬奇，SubFix 出现在「工作区 ▸ 脚本」中。
4. 卸载：运行 `uninstall_subfix.ps1`（支持 `-DryRun` 预览、`UNINSTALL` 确认）。

Windows 与 macOS 的下载、AI 纠错、本地 Qwen 识别、更新功能使用同一套逻辑，路径分别落在 `%TEMP%`、`%LOCALAPPDATA%\SubFix`（本地 Qwen 环境与模型）、`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility`（当前用户插件本体，Windows 用户级比全用户多一层 `Support`）、`%ProgramData%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility`（全用户插件本体）下。注意：设置字幕目标轨的自动化（依赖 macOS 辅助功能）在 Windows 上不可用，请手动在达芬奇中选择目标轨。

## 源码与构建

本快照包含插件运行代码和安装包构建脚本。私人字幕、项目样本、开发记录和依赖这些样本的内部测试不在公开快照中。旧副本和平台缓存需要单独处理，不能保证已下载的资料被收回。

完整构建使用 `build_subfix_test_package.sh`，需要设置 `ALIGNER_MODEL` 为 Qwen 强制对齐 GGUF 模型路径，`QWEN_BUILD` 为编译好的 qwen3-asr.cpp 运行时目录，并设置 `VERSION=3.3.0`。脚本内置 Python 与 FFmpeg 下载、校验和打包流程。`build_pkg.sh` 仅生成轻量包。Windows 版构建使用 `build_pkg_win.ps1`（产出可解压部署的 ZIP，并附带 `install_subfix.ps1` / `uninstall_subfix.ps1`），通过 `-PythonRuntimeUrl`、`-QwenBuild`、`-FfmpegDir`、`-AlignerModel` 提供 Windows 平台专属二进制。

v5 复用了 `subfix_generate_v4.py` 中的基础算法；文件名不代表仍可选择旧引擎。公开配置不包含原始训练字幕，保留运行所需的模型权重。

## AI 工作台

任务统一为完整纠错、的地得专项检测、中英翻译和简繁转换，使用纯文字菜单。翻译和简繁转换根据字幕抽样自动判断方向，再对整批字幕应用同一方向；依据不足或检测失败时停止并保留原字幕。简繁转换只处理字形，不替换地区用语，不改变字幕时间。

## 本地 Qwen 下载

首次安装依赖使用清华 PyPI 镜像；识别模型优先从魔搭 ModelScope 下载，失败后尝试 Hugging Face 备用源。下载源显示在进度中，保留已下载文件。国内源的可用性仍取决于当前网络。已有完整环境和模型继续复用。镜像配置仅作用于 SubFix 安装命令，不修改系统 pip 或代理配置。

[Qwen 官方模型下载说明](https://github.com/QwenLM/Qwen3-ASR#released-models-description-and-download)推荐中国大陆用户使用 ModelScope。
