# Uninstall SubFix from DaVinci Resolve's Fusion script directory on Windows.
# 从达芬奇（Windows）Fusion 脚本目录移除 SubFix 插件及其内置运行环境。
# 用法：
#   powershell -ExecutionPolicy Bypass -File uninstall_subfix.ps1
#   powershell -ExecutionPolicy Bypass -File uninstall_subfix.ps1 -DryRun
#   powershell -ExecutionPolicy Bypass -File uninstall_subfix.ps1 -NeedsAdmin
#   powershell -ExecutionPolicy Bypass -File uninstall_subfix.ps1 -AllUsers
param(
    [switch]$DryRun,
    [switch]$NeedsAdmin,
    [switch]$AllUsers
)

$ErrorActionPreference = 'Stop'

# 用户级目录比全用户多一层 Support，两种都要清理。
$RelUser = 'Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility'
$RelSystem = 'Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility'
$TargetRoots = @()
$ProgData = [Environment]::GetEnvironmentVariable('ProgramData')
if ($AllUsers) {
    if ($ProgData) { $TargetRoots += (Join-Path $ProgData $RelSystem) }
} else {
    $AppData = [Environment]::GetEnvironmentVariable('APPDATA')
    if ($AppData) { $TargetRoots += (Join-Path $AppData $RelUser) }
    if ($ProgData) { $TargetRoots += (Join-Path $ProgData $RelSystem) }
}

$LocalAppData = [Environment]::GetEnvironmentVariable('LOCALAPPDATA')
if (-not $LocalAppData) { $LocalAppData = Join-Path $env:USERPROFILE 'AppData\Local' }

$Targets = @()
foreach ($Root in $TargetRoots) {
    foreach ($Name in @('SubFix', '.subfix_support', 'SubFix.lua', 'SubFix_GenerateSelectionSubtitles.lua')) {
        $Path = Join-Path $Root $Name
        if (Test-Path $Path) { $Targets += $Path }
    }
}
# Qwen 依赖与运行数据位于 Resolve 脚本树之外，单独清理。
foreach ($Name in @('envs\qwen-local', '.subfix-qwen-local-ready.json')) {
    $Path = Join-Path (Join-Path $LocalAppData 'SubFix') $Name
    if (Test-Path $Path) { $Targets += $Path }
}

# 纯只读探测：在改动前判断是否需要管理员权限（系统目录安装存在时）。
if ($NeedsAdmin) {
    if ($Targets | Where-Object { $_ -like (Join-Path $ProgData '*') }) {
        Write-Output 'yes'
    } else {
        Write-Output 'no'
    }
    exit 0
}

if ($Targets.Count -eq 0) {
    Write-Host "没有发现已安装的 SubFix。"
    exit 0
}

Write-Host "将删除以下 SubFix 插件文件及其内置运行环境："
$Targets | ForEach-Object { Write-Host "  $_" }
Write-Host "保留达芬奇项目，以及用户数据目录中的字幕备份和识别模型。"

if ($DryRun) { exit 0 }

Write-Host "请先关闭 SubFix 窗口。输入 UNINSTALL 确认卸载，其他输入均取消："
$Confirmation = Read-Host
if ($Confirmation -ne 'UNINSTALL') {
    Write-Host "已取消，未删除文件。"
    exit 0
}

foreach ($Path in $Targets) {
    Remove-Item -Path $Path -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "已删除：$Path"
}

Write-Host "SubFix 卸载完成，请重新打开达芬奇以刷新脚本菜单。"
