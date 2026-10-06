# Setup the SubFix ASR virtual environment on Windows (replaces setup_asr_env.sh).
# 在 Windows 上创建 SubFix 的 ASR 虚拟环境（替代 macOS 的 setup_asr_env.sh）。
#
# 该脚本通常位于 .subfix_support\ 下，因此默认把 venv 建在同级 .subfix_asr_env，
# 并使用同级的 runtime\python\python.exe（与 SubFix.lua 中 subfix_venv_python 的预期一致）。
# 可由 SubFix 调用，也可手动运行。
param(
    [string]$VenvDir,
    [string]$RuntimePython
)

$ErrorActionPreference = 'Stop'

$SupportDir = $PSScriptRoot
if (-not $RuntimePython) {
    $RuntimePython = Join-Path $SupportDir 'runtime\python\python.exe'
}
if (-not $VenvDir) {
    $VenvDir = Join-Path $SupportDir '.subfix_asr_env'
}

if (-not (Test-Path $RuntimePython)) {
    Write-Error "未找到 SubFix 内置 Python：$RuntimePython（请重新安装完整版 SubFix）"
    exit 1
}

Write-Host "使用 Python: $RuntimePython"
Write-Host "ASR venv: $VenvDir"

if (-not (Test-Path (Join-Path $VenvDir 'Scripts\python.exe'))) {
    & $RuntimePython -m venv $VenvDir
}

$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
& $VenvPython -m pip install --upgrade pip

if ($env:SUBFIX_INSTALL_QWEN_ASR -ne '0') {
    & $VenvPython -m pip install -U 'qwen-asr'
    Write-Host "Qwen3-ASR 已安装。首次生成会下载 Qwen/Qwen3-ASR-1.7B；字幕规整使用内置 Qwen3 Forced Aligner。"
} else {
    Write-Host "已按 SUBFIX_INSTALL_QWEN_ASR=0 跳过 Qwen3-ASR。v4 生成字幕将不可用。"
}

Write-Host "ASR 环境已安装。"
