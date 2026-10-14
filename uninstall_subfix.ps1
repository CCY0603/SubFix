# Uninstall SubFix from DaVinci Resolve's Fusion script directory on Windows.
# 从达芬奇（Windows）Fusion 脚本目录移除 SubFix 插件及其内置运行环境。
# 用法：
#   powershell -ExecutionPolicy Bypass -File uninstall_subfix.ps1
#   powershell -ExecutionPolicy Bypass -File uninstall_subfix.ps1 -DryRun
#   powershell -ExecutionPolicy Bypass -File uninstall_subfix.ps1 -NeedsAdmin
#   powershell -ExecutionPolicy Bypass -File uninstall_subfix.ps1 -AllUsers
#   powershell -ExecutionPolicy Bypass -File uninstall_subfix.ps1 -NoElevate
param(
    [switch]$DryRun,
    [switch]$NeedsAdmin,
    [switch]$AllUsers,
    [switch]$NoElevate
)

$ErrorActionPreference = 'Stop'

# 用户级目录比全用户多一层 Support，两种都要清理。
$RelUser = 'Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility'
$RelSystem = 'Blackmagic Design\DaVinci Resolve\Fusion\Scripts\Utility'
$ProgData = [Environment]::GetEnvironmentVariable('ProgramData')
if (-not $ProgData) { $ProgData = 'C:\ProgramData' }

$UserRoots = @()
$SystemRoots = @()
if ($AllUsers) {
    $SystemRoots += (Join-Path $ProgData $RelSystem)
} else {
    # 默认同时清理当前用户与全用户两处，避免达芬奇从任一脚本树加载旧副本。
    $AppData = [Environment]::GetEnvironmentVariable('APPDATA')
    if ($AppData) { $UserRoots += (Join-Path $AppData $RelUser) }
    $SystemRoots += (Join-Path $ProgData $RelSystem)
}

$LocalAppData = [Environment]::GetEnvironmentVariable('LOCALAPPDATA')
if (-not $LocalAppData) { $LocalAppData = Join-Path $env:USERPROFILE 'AppData\Local' }

function Test-IsAdministrator {
    try {
        $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
        return $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

$UserTargets = @()
$SystemTargets = @()
foreach ($Root in $UserRoots) {
    foreach ($Name in @('SubFix', '.subfix_support', 'SubFix.lua', 'SubFix_GenerateSelectionSubtitles.lua')) {
        $Path = Join-Path $Root $Name
        if (Test-Path -LiteralPath $Path) { $UserTargets += $Path }
    }
}
foreach ($Root in $SystemRoots) {
    foreach ($Name in @('SubFix', '.subfix_support', 'SubFix.lua', 'SubFix_GenerateSelectionSubtitles.lua')) {
        $Path = Join-Path $Root $Name
        if (Test-Path -LiteralPath $Path) { $SystemTargets += $Path }
    }
}
$Targets = @($UserTargets + $SystemTargets)

# Qwen 依赖与运行数据位于 Resolve 脚本树之外，单独清理。
$QwenTargets = @()
foreach ($Name in @('envs\qwen-local', '.subfix-qwen-local-ready.json')) {
    $Path = Join-Path (Join-Path $LocalAppData 'SubFix') $Name
    if (Test-Path -LiteralPath $Path) { $QwenTargets += $Path }
}
$Targets += $QwenTargets

# 纯只读探测：在改动前判断是否需要管理员权限（系统目录安装存在时）。
if ($NeedsAdmin) {
    if ($SystemTargets.Count -gt 0) {
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

if ($DryRun) {
    if ($SystemTargets.Count -gt 0 -and -not (Test-IsAdministrator)) {
        Write-Host "注意：系统目录中的副本需要管理员权限才能删除。"
    }
    exit 0
}

Write-Host "请先关闭 SubFix 窗口。输入 UNINSTALL 确认卸载，其他输入均取消："
$Confirmation = Read-Host
if ($Confirmation -ne 'UNINSTALL') {
    Write-Host "已取消，未删除文件。"
    exit 0
}

# 系统目录存在而当前会话非管理员时，自动以提升权限重新运行自身（弹 UAC）；
# -NoElevate 用于无法交互提权的环境，此时仅给出操作指引。
if ($SystemTargets.Count -gt 0 -and -not (Test-IsAdministrator) -and -not $NoElevate) {
    Write-Host "检测到系统目录安装，需要管理员权限，正在请求提权..."
    $RelaunchArgs = @('-ExecutionPolicy', 'Bypass', '-File', $MyInvocation.MyCommand.Definition)
    if ($AllUsers) { $RelaunchArgs += '-AllUsers' }
    $RelaunchArgs += '-NoElevate'
    try {
        $Elevated = Start-Process -FilePath 'powershell.exe' -ArgumentList $RelaunchArgs -Verb RunAs -Wait -PassThru
        exit $Elevated.ExitCode
    } catch {
        Write-Warning "未能获得管理员权限，系统目录副本将保留。可右键 PowerShell 选择「以管理员身份运行」后重试本脚本。"
        # 继续删除当前用户部分（若有），最后按残留情况返回非零。
    }
}

$Failures = @()
foreach ($Path in $Targets) {
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $Path) {
            throw "删除后路径仍存在"
        }
        Write-Host "已删除：$Path"
    } catch {
        $Failures += $Path
        Write-Warning "删除失败：$Path（$($_.Exception.Message)）"
    }
}

if ($Failures.Count -gt 0) {
    Write-Host ""
    Write-Host "部分文件未能删除，请关闭达芬奇或以管理员身份重试："
    $Failures | ForEach-Object { Write-Host "  $_" }
    exit 1
}

Write-Host "SubFix 卸载完成，请重新打开达芬奇以刷新脚本菜单。"
exit 0
