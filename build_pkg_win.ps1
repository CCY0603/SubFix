# Build the distributable Windows package for SubFix (replaces build_pkg.sh / build_subfix_test_package.sh).
# 生成 SubFix 的 Windows 可分发包（替代 macOS 的 pkgbuild/ditto 流程）。
#
# 与 macOS 版本不同：Windows 没有 .pkg 与公证流程，这里产出可直接解压部署的 ZIP，
# 并附带 install_subfix.ps1 / uninstall_subfix.ps1。
#
# 平台专属二进制（内置 Python runtime、qwen3-asr-cli、ffmpeg、对齐模型）需 Windows 构建：
#   -PythonRuntimeUrl 下载并解包到 .subfix_support/runtime/python
#   -QwenBuild          指定含 qwen3-asr-cli[.exe] 的目录，复制到 .subfix_support/bin
#   -FfmpegDir          指定含 ffmpeg[.exe] 的目录，复制到 .subfix_support/bin
#   -AlignerModel       指定对齐模型 .gguf，复制到 .subfix_support/models
# 若不提供上述参数，则尝试从 SourceDir/.subfix_support 下已有的 runtime/bin/models 复制。
param(
    [string]$Version = '3.0-beta',
    [string]$SourceDir,
    [string]$ReleaseRoot,
    [string]$PythonRuntimeUrl,
    [string]$QwenBuild,
    [string]$FfmpegDir,
    [string]$AlignerModel
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path $MyInvocation.MyCommand.Definition -Parent
if (-not $SourceDir) { $SourceDir = $ScriptDir }
if (-not $ReleaseRoot) { $ReleaseRoot = Join-Path $ScriptDir 'dist' }

$ReleaseName = "SubFix_v${Version}_Windows"
$ReleaseDir = Join-Path $ReleaseRoot $ReleaseName
$BuildRoot = Join-Path $ScriptDir 'build\subfix-win'

$PkgName = 'SubFix'
$SourceFile = Join-Path $SourceDir 'SubFix.lua'
$GeneratorFile = Join-Path $SourceDir '生成选区字幕.lua'
$GenerateCore = Join-Path $SourceDir '.subfix_support\subfix_generate_selection_core.lua'
$UpdateHelper = Join-Path $SourceDir '.subfix_support\subfix_update.py'
$QwenManager = Join-Path $SourceDir '.subfix_support\subfix_qwen_local_manager.py'
$ProcessGroup = Join-Path $SourceDir '.subfix_support\subfix_process_group.py'
$AsrHelper = Join-Path $SourceDir 'subfix_asr_transcribe.py'
$GenV4 = Join-Path $SourceDir 'subfix_generate_v4.py'
$GenV5 = Join-Path $SourceDir 'subfix_generate_v5.py'
$GenTextnorm = Join-Path $SourceDir 'subfix_generate_textnorm.py'
$SetupEnv = Join-Path (Join-Path $SourceDir '.subfix_support') 'setup_asr_env.ps1'
$Profile = Join-Path $SourceDir '.subfix_support\segmentation_profile.json'
$ProfileV3 = Join-Path $SourceDir '.subfix_support\segmentation_profile_v3.json'
$ProfileV4 = Join-Path $SourceDir '.subfix_support\segmentation_profile_v4.json'

$Required = @($SourceFile, $GeneratorFile, $GenerateCore, $UpdateHelper, $QwenManager, $ProcessGroup, $AsrHelper, $GenV4, $GenV5, $GenTextnorm, $SetupEnv, $Profile, $ProfileV3, $ProfileV4)
foreach ($F in $Required) {
    if (-not (Test-Path $F)) {
        Write-Error "缺少发行文件：$F"
        exit 1
    }
}

Write-Host "开始构建 $PkgName v$Version (Windows) ..."

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $BuildRoot, $ReleaseDir
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ReleaseDir 'SubFix') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ReleaseDir '.subfix_support') | Out-Null

Write-Host "拷贝源文件 ..."
Copy-Item $SourceFile (Join-Path $ReleaseDir 'SubFix\SubFix.lua')
Copy-Item $GeneratorFile (Join-Path $ReleaseDir 'SubFix\生成选区字幕.lua')
$CoreSupport = Join-Path $ReleaseDir '.subfix_support'
foreach ($H in @($GenerateCore, $UpdateHelper, $QwenManager, $ProcessGroup, $Profile, $ProfileV3, $ProfileV4)) {
    Copy-Item $H $CoreSupport
}
foreach ($H in @($AsrHelper, $GenV4, $GenV5, $GenTextnorm, $SetupEnv)) {
    Copy-Item $H $CoreSupport
}

# ---- 平台专属二进制 ----
function Copy-TreeIfExists($From, $To) {
    if (Test-Path $From) {
        New-Item -ItemType Directory -Force -Path $To | Out-Null
        Copy-Item (Join-Path $From '*') $To -Recurse -Force
        Write-Host "已打包：$To"
    }
}

Copy-TreeIfExists (Join-Path $SourceDir '.subfix_support\runtime') (Join-Path $CoreSupport 'runtime')
Copy-TreeIfExists (Join-Path $SourceDir '.subfix_support\bin') (Join-Path $CoreSupport 'bin')
Copy-TreeIfExists (Join-Path $SourceDir '.subfix_support\models') (Join-Path $CoreSupport 'models')
Copy-TreeIfExists (Join-Path $SourceDir '.subfix_support\licenses') (Join-Path $CoreSupport 'licenses')

if ($PythonRuntimeUrl) {
    Write-Host "下载内置 Python runtime ..."
    $RtStage = Join-Path $BuildRoot 'runtime_stage'
    New-Item -ItemType Directory -Force -Path $RtStage | Out-Null
    $RtArchive = Join-Path $BuildRoot 'python.zip'
    Invoke-WebRequest -Uri $PythonRuntimeUrl -OutFile $RtArchive
    if ($RtArchive -like '*.tar.gz') {
        tar.exe -xzf $RtArchive -C $RtStage
        $Extracted = Get-ChildItem $RtStage -Directory | Select-Object -First 1
        Copy-Item (Join-Path $Extracted.FullName 'python') (Join-Path $CoreSupport 'runtime\python') -Recurse -Force
    } else {
        Expand-Archive -Path $RtArchive -DestinationPath $RtStage -Force
        Copy-Item (Join-Path $RtStage 'python') (Join-Path $CoreSupport 'runtime\python') -Recurse -Force
    }
}

if ($QwenBuild) {
    New-Item -ItemType Directory -Force -Path (Join-Path $CoreSupport 'bin') | Out-Null
    foreach ($Bin in @('qwen3-asr-cli.exe', 'qwen3-asr-cli')) {
        if (Test-Path (Join-Path $QwenBuild $Bin)) {
            Copy-Item (Join-Path $QwenBuild $Bin) (Join-Path $CoreSupport "bin\$Bin") -Force
        }
    }
}

if ($FfmpegDir) {
    New-Item -ItemType Directory -Force -Path (Join-Path $CoreSupport 'bin') | Out-Null
    foreach ($Bin in @('ffmpeg.exe', 'ffmpeg')) {
        if (Test-Path (Join-Path $FfmpegDir $Bin)) {
            Copy-Item (Join-Path $FfmpegDir $Bin) (Join-Path $CoreSupport "bin\$Bin") -Force
        }
    }
}

if ($AlignerModel -and (Test-Path $AlignerModel)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $CoreSupport 'models') | Out-Null
    Copy-Item $AlignerModel (Join-Path $CoreSupport 'models\qwen3-forced-aligner-0.6b-f16.gguf') -Force
}

# ---- 安装 / 卸载程序 ----
Copy-Item (Join-Path $ScriptDir 'install_subfix.ps1') $ReleaseDir
Copy-Item (Join-Path $ScriptDir 'install_subfix.bat') $ReleaseDir
Copy-Item (Join-Path $ScriptDir 'uninstall_subfix.ps1') $ReleaseDir

# ---- 生成 ZIP ----
$ZipPath = Join-Path $ReleaseRoot "$ReleaseName.zip"
Remove-Item -Force -ErrorAction SilentlyContinue $ZipPath
Compress-Archive -Path (Join-Path $ReleaseDir '*') -DestinationPath $ZipPath

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $BuildRoot

Write-Host ""
Write-Host "已生成分发目录：$ReleaseDir"
Write-Host "已生成分发 ZIP：$ZipPath"
Write-Host ""
Write-Host "分发目录包含："
Write-Host "  - 安装程序：install_subfix.ps1 / install_subfix.bat"
Write-Host "  - 卸载程序：uninstall_subfix.ps1"
Write-Host "  - 主入口：SubFix\SubFix.lua"
Write-Host "  - 生成入口：SubFix\生成选区字幕.lua"
Write-Host "  - 支持文件：.subfix_support\"
