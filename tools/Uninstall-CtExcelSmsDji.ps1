[CmdletBinding()]
param(
    [string]$InstallPath,

    [switch]$DescribePlan
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $InstallPath) {
    $InstallPath = Split-Path -Parent $PSScriptRoot
}
$resolvedInstall = [IO.Path]::GetFullPath($InstallPath).TrimEnd('\')
$installLeaf = [IO.Path]::GetFileName($resolvedInstall)
$pathRoot = [IO.Path]::GetPathRoot($resolvedInstall).TrimEnd('\')
$markerPath = Join-Path $resolvedInstall '.ctexcel-dji-install.json'

if ($installLeaf -ne 'CTExcel-SMS-DJI') {
    throw "Refusing to uninstall an unexpected directory name: $resolvedInstall"
}
if ($resolvedInstall -eq $pathRoot) {
    throw "Refusing to uninstall a drive root: $resolvedInstall"
}
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
    throw "Installer marker is missing; refusing to remove this directory: $resolvedInstall"
}

$privateFileNames = @('config.json', 'archive.jsonl', 'state.json', 'tg_state.json', 'app.log')
$privateFilesPresent = @(
    $privateFileNames | Where-Object {
        Test-Path -LiteralPath (Join-Path $resolvedInstall $_) -PathType Leaf
    }
)
$backupRoot = Join-Path $env:LOCALAPPDATA (
    'CTExcel-SMS-DJI-Backups\{0}' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
)
$desktopShortcut = Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'CTExcel短信工具-DJI.lnk'

$plan = [ordered]@{
    install_path = $resolvedInstall
    private_files_present = $privateFilesPresent
    default_backup_path = $backupRoot
    will_backup_private_data = $true
    will_remove_install_directory = $true
    will_remove_desktop_shortcut = (Test-Path -LiteralPath $desktopShortcut -PathType Leaf)
    leaves_shared_python_and_powershell = $true
}
if ($DescribePlan) {
    $plan | ConvertTo-Json -Depth 3
    exit 0
}

$listener = @(
    Get-NetTCPConnection -LocalPort 7597 -State Listen -ErrorAction SilentlyContinue
)
if ($listener.Count -ne 0) {
    throw 'CTExcel 短信工具仍在运行并占用 7597 端口。请先关闭程序，再重新运行卸载。'
}

Write-Host ''
Write-Host 'CTExcel SMS DJI 卸载'
Write-Host "安装目录: $resolvedInstall"
Write-Host '卸载只删除本工具；共享的 Python 和 PowerShell 运行环境会保留。'
Write-Host ''
Write-Host '[1] 卸载程序并备份私人数据（推荐）'
Write-Host '[2] 卸载程序并永久删除私人数据'
Write-Host '[Q] 取消'
$choice = (Read-Host '请选择').Trim().ToUpperInvariant()

$keepData = $false
switch ($choice) {
    '1' { $keepData = $true }
    '2' {
        $confirmation = Read-Host '此操作会永久删除配置和短信存档。请输入 DELETE 确认'
        if ($confirmation -cne 'DELETE') {
            Write-Host '确认文字不匹配，卸载已取消。'
            exit 2
        }
    }
    'Q' {
        Write-Host '卸载已取消。'
        exit 0
    }
    default {
        Write-Host '无效选择，卸载已取消。'
        exit 2
    }
}

if ($keepData -and $privateFilesPresent.Count -ne 0) {
    New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
    foreach ($name in $privateFilesPresent) {
        Copy-Item -LiteralPath (Join-Path $resolvedInstall $name) -Destination (Join-Path $backupRoot $name)
    }
    Write-Host "私人数据已备份到: $backupRoot"
}

$cleanupPath = Join-Path ([IO.Path]::GetTempPath()) (
    'CTExcel-SMS-DJI-uninstall-{0}.ps1' -f ([Guid]::NewGuid().ToString('N'))
)
$cleanupSource = @'
param(
    [Parameter(Mandatory)]
    [string]$Target,
    [string]$Shortcut
)
$ErrorActionPreference = 'Stop'
try {
    Start-Sleep -Milliseconds 1200
    $resolved = [IO.Path]::GetFullPath($Target).TrimEnd('\')
    $leaf = [IO.Path]::GetFileName($resolved)
    $root = [IO.Path]::GetPathRoot($resolved).TrimEnd('\')
    $marker = Join-Path $resolved '.ctexcel-dji-install.json'
    if ($leaf -ne 'CTExcel-SMS-DJI' -or $resolved -eq $root -or -not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw "卸载目标复核失败，未删除: $resolved"
    }
    if ($Shortcut -and (Test-Path -LiteralPath $Shortcut -PathType Leaf)) {
        Remove-Item -LiteralPath $Shortcut -Force
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
    Write-Host 'CTExcel 短信工具已卸载。'
    Start-Sleep -Seconds 2
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
} catch {
    Write-Host "卸载失败: $($_.Exception.Message)" -ForegroundColor Red
    Read-Host '按 Enter 关闭'
    exit 1
}
'@
[IO.File]::WriteAllText($cleanupPath, $cleanupSource, [Text.UTF8Encoding]::new($false))

$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
$cleanupArguments = '-NoProfile -File "{0}" -Target "{1}" -Shortcut "{2}"' -f (
    $cleanupPath.Replace('"', '\"'),
    $resolvedInstall.Replace('"', '\"'),
    $desktopShortcut.Replace('"', '\"')
)
Start-Process -FilePath $pwsh -ArgumentList $cleanupArguments -WindowStyle Normal | Out-Null
Write-Host '卸载清理程序已启动。当前窗口可以关闭。'
