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
$AppDataBase = [Environment]::GetEnvironmentVariable('APPDATA')
if (-not $AppDataBase) { $AppDataBase = Join-Path $env:USERPROFILE 'AppData\Roaming' }
$ProgramDataBase = [Environment]::GetEnvironmentVariable('ProgramData')
if (-not $ProgramDataBase) { $ProgramDataBase = 'C:\ProgramData' }
if ($AllUsers) {
    $Base = $ProgramDataBase
    $Rel = $RelSystem
} else {
    $Base = $AppDataBase
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
    # 仓库扁平布局：只复制唯一的用户菜单入口，避免把仓库其它文件（README、脚本、隐藏目录等）带进去。
    # 独立的「生成选区字幕.lua」仅保留在仓库中作为手动调试入口，不再面向用户部署。
    $Src = Join-Path $Source 'SubFix.lua'
    if (Test-Path $Src) { Copy-Item -Path $Src -Destination $DestSubFix -Force }
    # 扁平布局下辅助脚本位于源根目录，按运行时期望的 .subfix_support 布局复制过去。
    foreach ($Py in (Get-ChildItem -Path $Source -Filter 'subfix_*.py' -File)) {
        Copy-Item -Path $Py.FullName -Destination $DestSupport -Force
    }
} else {
    Copy-Item -Path (Join-Path $SourceSubFix '*') -Destination $DestSubFix -Recurse -Force
}
Copy-Item -Path (Join-Path $SourceSupport '*') -Destination $DestSupport -Recurse -Force

# 旧版本曾把独立的「生成选区字幕」菜单复制进 SubFix 目录或 Utility 根目录；
# 合并为单一菜单后这些副本必须清掉。绝不动现役的 SubFix\SubFix.lua。
function Remove-LegacySubFixMenu {
    param([string]$Root)
    if (-not $Root -or -not (Test-Path -LiteralPath $Root)) { return }
    foreach ($Legacy in @(
        (Join-Path $Root 'SubFix\生成选区字幕.lua'),
        (Join-Path $Root 'SubFix\SubFix_GenerateSelectionSubtitles.lua'),
        (Join-Path $Root 'SubFix_GenerateSelectionSubtitles.lua'),
        (Join-Path $Root 'SubFix.lua')
    )) {
        try {
            if (Test-Path -LiteralPath $Legacy) {
                Remove-Item -LiteralPath $Legacy -Force -ErrorAction Stop
                Write-Host "已清理旧版菜单副本：$Legacy"
            }
        } catch {
            Write-Warning "未能删除旧版菜单副本：$Legacy（$($_.Exception.Message)）"
        }
    }
}

Remove-LegacySubFixMenu -Root $Target
# 部署到一个根时，顺带尽力清理另一个根里的旧副本（系统级可能需要管理员，失败仅警告）。
if ($AllUsers) {
    Remove-LegacySubFixMenu -Root (Join-Path $AppDataBase $RelUser)
} else {
    Remove-LegacySubFixMenu -Root (Join-Path $ProgramDataBase $RelSystem)
}

# 让主脚本可被执行（Windows 不依赖 +x，但保持与 macOS 一致的部署动作）。
Write-Host "已部署 SubFix 运行时到：$Target"
Write-Host ""
Write-Host "请重新启动达芬奇以刷新 Fusion 脚本菜单。SubFix 将出现在『工作区 ▸ 脚本』中。"
