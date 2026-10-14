# SubFix v3.3.1

DaVinci Resolve 字幕插件。口播、现场、单段和批量字幕生成统一使用 v5。

## 版本更新

### v3.3.1（2026-10-14）

**新功能与体验**

- 全新三步式工作台：单一窗口内分为「选区识别 / 精修工具 / AI 工作台」，识别进度内嵌显示，不再弹出独立进度窗；无字幕时显示引导页，生成完成后自动跳转到精修。
- 界面整体改版：中性炭灰配色、微软雅黑字体、按钮按主次分级、语义化状态栏配色；移除 emoji 图标，降低大面积橙色，长时间使用更安静。
- 新增字幕拆分：在编辑框光标位置把一条字幕拆成两条，时间按字符比例自动折算，可撤回，拆分后自动选中后半句。
- 新增播放自动跟随（默认开启）：暂停播放或拖动播放头后约 1 秒内自动选中并滚动到当前字幕行；搜索、AI 处理、整轨写回期间自动暂停跟随。
- 新增退出未保存提示：有修改尚未写回时间线时，退出会询问「更新到时间线 / 取消修改」，避免误丢编辑成果。
- 编辑框停笔 400ms 自动保存到内存并刷新列表，切换行不丢字；仍需点「更新时间线」才会写入达芬奇。
- 精修工具恢复「敏感词替换 / 清空行 / 提取纯文本」三项功能。

**问题修复**

- 修复 Windows 上「检查更新」必然失败的问题（路径分隔符与安装目标目录错误），在线更新现已可用。
- 修复插件窗口默认打开时底部按钮被裁切、需要手动拖动才完整的问题。
- 修复批量替换的查找或替换内容含 % 时报错的问题，改为纯文本匹配。
- 修复中文数字转换把「三四五条、二三十」等概数误转为阿拉伯数字的问题。
- 修复反复执行最终交付检查时时间线标记闪烁、重复删打的问题。
- 修复空闲状态下一个 CPU 核心被轮询打满的问题。
- 修复打开生成面板时本地 Qwen 环境检测卡顿：检测结果缓存并后台预热。
- 修复 ffmpeg 切片可能永久挂起：按区间长度增加超时并给出明确错误提示。
- 修复取消生成后残留孤儿 python/ffmpeg 进程、关闭窗口未回收子进程树的问题。
- 修复豆包语音识别把 API Key 误当用户 ID 发送的问题。
- 修复 Windows 中文用户名、非 ANSI 路径下的文件读写与进程启动问题。
- 补全丢帧（Drop Frame）时间码换算规则；修复 v5 搜索窗口在非 30fps 工程下未按帧率缩放导致边界判定失准的问题。
- 修复识别页提示文字叠印到音频轨列表、轨道树失败误提示「已取消」等问题。
- 卸载程序新增删除结果复核与双击入口，检测到系统级安装时自动请求管理员权限。

**性能与稳定性**

- 长素材合轨改为批量内存拷贝，长音频处理明显加快；字幕树内容未变化时跳过重绘，消除刷新闪烁和选中丢失。
- 生成进度、状态栏文本去重，选区与音频候选结果指纹缓存，减少无谓重扫。
- 日志改为定长环形缓冲与批量落盘，生成高峰期显著降低 CPU 与磁盘占用。
- 运行时自动清理 7 天前遗留的临时目录；打包剔除无用调试符号文件。
- 大量历史死代码清理（净减约 400 行），时间线获取等重复逻辑统一封装，行为不变。
- 自动化测试 113 项通过。

## 安装

从 [GitHub Releases](https://github.com/CCY0603/SubFix/releases/latest) 下载完整安装 ZIP，解压并打开 pkg。支持 Apple Silicon Mac，沿用未签名安装方式。首次使用本地 Qwen 识别仍需联网安装识别依赖和模型。

### Windows 安装

SubFix 同样支持 Windows 版达芬奇。代码已全面跨平台化（临时目录、用户目录、达芬奇脚本目录、venv Python、`curl` 下载、模型与环境缓存、打开外部链接等均改用 Windows 路径与命令）。

1. 下载 `SubFix_v*_Windows.zip` 并解压，得到 `SubFix/`、`install_subfix.ps1`、`uninstall_subfix.ps1` 等。
2. 右键 `install_subfix.bat`（或 `powershell -ExecutionPolicy Bypass -File install_subfix.ps1`）运行；普通用户部署到 `%APPDATA%`，以管理员运行（或加 `-AllUsers`）部署到 `%ProgramData%`。
3. 重新启动达芬奇，SubFix 出现在「工作区 ▸ 脚本」中。
4. 卸载：运行 `uninstall_subfix.ps1`（支持 `-DryRun` 预览、`UNINSTALL` 确认）。

Windows 与 macOS 的下载、AI 纠错、本地 Qwen 识别、更新功能使用同一套逻辑，路径分别落在 `%TEMP%`、`%LOCALAPPDATA%\SubFix`（本地 Qwen 环境与模型）、`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility`（当前用户插件本体，Windows 用户级比全用户多一层 `Support`）、`%ProgramData%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility`（全用户插件本体）下。注意：设置字幕目标轨的自动化（依赖 macOS 辅助功能）在 Windows 上不可用，请手动在达芬奇中选择目标轨。

## 源码与构建

本快照包含插件运行代码和安装包构建脚本。私人字幕、项目样本、开发记录和依赖这些样本的内部测试不在公开快照中。旧副本和平台缓存需要单独处理，不能保证已下载的资料被收回。

完整构建使用 `build_subfix_test_package.sh`，需要设置 `ALIGNER_MODEL` 为 Qwen 强制对齐 GGUF 模型路径，`QWEN_BUILD` 为编译好的 qwen3-asr.cpp 运行时目录，并设置 `VERSION=3.3.1`。脚本内置 Python 与 FFmpeg 下载、校验和打包流程。`build_pkg.sh` 仅生成轻量包。Windows 版构建使用 `build_pkg_win.ps1`（产出可解压部署的 ZIP，并附带 `install_subfix.ps1` / `uninstall_subfix.ps1`），通过 `-PythonRuntimeUrl`、`-QwenBuild`、`-FfmpegDir`、`-AlignerModel` 提供 Windows 平台专属二进制。

v5 复用了 `subfix_generate_v4.py` 中的基础算法；文件名不代表仍可选择旧引擎。公开配置不包含原始训练字幕，保留运行所需的模型权重。

## AI 工作台

任务统一为完整纠错、的地得专项检测、中英翻译和简繁转换，使用纯文字菜单。翻译和简繁转换根据字幕抽样自动判断方向，再对整批字幕应用同一方向；依据不足或检测失败时停止并保留原字幕。简繁转换只处理字形，不替换地区用语，不改变字幕时间。

## 本地 Qwen 下载

首次安装依赖使用清华 PyPI 镜像；识别模型优先从魔搭 ModelScope 下载，失败后尝试 Hugging Face 备用源。下载源显示在进度中，保留已下载文件。国内源的可用性仍取决于当前网络。已有完整环境和模型继续复用。镜像配置仅作用于 SubFix 安装命令，不修改系统 pip 或代理配置。

[Qwen 官方模型下载说明](https://github.com/QwenLM/Qwen3-ASR#released-models-description-and-download)推荐中国大陆用户使用 ModelScope。
