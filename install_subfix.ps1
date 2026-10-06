# Install SubFix into DaVinci Resolve's Fusion script directory on Windows.
# 把 SubFix 部署到达芬奇（Windows）Fusion 脚本目录。
# 用法：
#   powershell -ExecutionPolicy Bypass -File install_subfix.ps1
#   powershell -ExecutionPolicy Bypass -File install_subfix.ps1 -AllUsers
#   powershell -ExecutionPolicy Bypass -File install_subfix.ps1 -Source D:\SubFix-release
param(
    [string]$Source,
    [switch]$AllUsers
)

$ErrorActionPreference = 'Stop'

# 脚本所在目录即发布目录（包含 SubFix\ 与 .subfix_support\）。
$MyDir = Split-Path $MyInvocation.MyCommand.Definition -Parent
if (-not $Source) { $Source = $MyDir }

$SourceSubFix = Join-Path $Source 'SubFix'
$SourceSupport = Join-Path $Source '.subfix_support'

# 兼容两种布局：
#  1) 发布布局：SubFix\SubFix.lua + .subfix_support\（build_pkg_win.ps1 产出）
#  2) 仓库扁平布局：SubFix.lua 直接位于源根目录，.subfix_support\ 仍在源根
if (-not (Test-Path (Join-Path $SourceSubFix 'SubFix.lua'))) {
    if (Test-Path (Join-Path $Source 'SubFix.lua')) {
        $SourceSubFix = $Source
    }
}

if (-not (Test-Path $SourceSubFix)) {
    Write-Error "未找到发布目录中的 SubFix 子目录：$SourceSubFix"
    exit 1
}
if (-not (Test-Path $SourceSupport)) {
    Write-Error "未找到发布目录中的 .subfix_support 子目录：$SourceSupport"
    exit 1
}

# 用户级目录比全用户多一层 Support（Resolve 自身也只创建这一处）：
#   用户级：%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
#   全用户：%ProgramData%\Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility
$RelUser = 'Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility'
$RelSystem = 'Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility'
if ($AllUsers) {
    $Base = [Environment]::GetEnvironmentVariable('ProgramData')
    if (-not $Base) { $Base = 'C:\ProgramData' }
    $Rel = $RelSystem
} else {
    $Base = [Environment]::GetEnvironmentVariable('APPDATA')
    if (-not $Base) { $Base = Join-Path $env:USERPROFILE 'AppData\Roaming' }
    $Rel = $RelUser
}
$Target = Join-Path $Base $Rel

# 拒绝写入符号链接目录，避免误删系统其它文件。
if (Test-Path $Target) {
    $Item = Get-Item $Target
    if ($Item.LinkType) {
        Write-Error "拒绝写入符号链接目录：$Target"
        exit 1
    }
}

$DestSubFix = Join-Path $Target 'SubFix'
$DestSupport = Join-Path $Target '.subfix_support'

Write-Host "源目录：$Source"
Write-Host "目标目录：$Target"

New-Item -ItemType Directory -Force -Path $DestSubFix | Out-Null
New-Item -ItemType Directory -Force -Path $DestSupport | Out-Null

# -Force 合并复制，保留用户此前可能留下的私有 Qwen 模型、环境与配置。
if ($SourceSubFix -eq $Source) {
    # 仓库扁平布局：只复制两个 lua 入口，避免把仓库其它文件（README、脚本、隐藏目录等）带进去。
    foreach ($F in @('SubFix.lua', '生成选区字幕.lua')) {
        $Src = Join-Path $Source $F
        if (Test-Path $Src) { Copy-Item -Path $Src -Destination $DestSubFix -Force }
    }
    # 扁平布局下辅助脚本位于源根目录，按运行时期望的 .subfix_support 布局复制过去。
    foreach ($Py in (Get-ChildItem -Path $Source -Filter 'subfix_*.py' -File)) {
        Copy-Item -Path $Py.FullName -Destination $DestSupport -Force
    }
} else {
    Copy-Item -Path (Join-Path $SourceSubFix '*') -Destination $DestSubFix -Recurse -Force
}
Copy-Item -Path (Join-Path $SourceSupport '*') -Destination $DestSupport -Recurse -Force

# 让主脚本可被执行（Windows 不依赖 +x，但保持与 macOS 一致的部署动作）。
Write-Host "已部署 SubFix 运行时到：$Target"
Write-Host ""
Write-Host "请重新启动达芬奇以刷新 Fusion 脚本菜单。SubFix 将出现在『工作区 ▸ 脚本』中。"
