[CmdletBinding()]
param(
    [string]$OutputDirectory,

    [switch]$StopRunningService,

    [switch]$SkipSelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $projectRoot 'dist'
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)

function Get-Port7597Pid {
    $pids = @(
        Get-NetTCPConnection -LocalPort 7597 -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
    if ($pids.Count -eq 0) {
        $matches = @(
            netstat -ano -p tcp |
                Where-Object { $_ -match '^\s*TCP\s+\S+:7597\s+\S+\s+LISTENING\s+(\d+)\s*$' } |
                ForEach-Object { [int]$Matches[1] } |
                Sort-Object -Unique
        )
        $pids = $matches
    }
    if ($pids.Count -gt 1) {
        throw "More than one listener was reported for port 7597: $($pids -join ', ')."
    }
    if ($pids.Count -eq 1) {
        return [int]$pids[0]
    }
    return $null
}

function Stop-VerifiedDjiServiceForSnapshot {
    $listenerPid = Get-Port7597Pid
    if (-not $listenerPid) {
        return $false
    }

    try {
        $status = Invoke-RestMethod -Uri 'http://127.0.0.1:7597/api/status' -TimeoutSec 2
    } catch {
        throw "Port 7597 is occupied by PID $listenerPid, but it is not a responsive DJI SMS service. Stop it manually."
    }
    if ($status.carrier.key -ne 'ctexcel' -or $status.modem.key -ne 'dji_qdc507') {
        throw "Port 7597 is occupied by PID $listenerPid, but its identity is not ctexcel + dji_qdc507."
    }
    $fingerprintBytes = [Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant())
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $expectedFingerprint = [Convert]::ToHexString($sha256.ComputeHash($fingerprintBytes)).ToLowerInvariant().Substring(0, 32)
    } finally {
        $sha256.Dispose()
    }
    $serviceInstanceProperty = $status.PSObject.Properties['service_instance']
    $serviceInstance = if ($serviceInstanceProperty) { $serviceInstanceProperty.Value } else { $null }
    if (
        -not $serviceInstance -or
        $serviceInstance.entrypoint -ne 'app.py' -or
        $serviceInstance.root_fingerprint -ne $expectedFingerprint
    ) {
        throw "Port 7597 is a DJI service, but it was not launched from this project directory. Stop it manually before building."
    }
    if (-not $StopRunningService) {
        throw "The DJI SMS service is running as PID $listenerPid. Stop it first, or rerun with -StopRunningService for a consistent private snapshot."
    }

    $process = Get-Process -Id $listenerPid -ErrorAction Stop
    if (-not $process) {
        throw "Could not inspect the verified DJI service process PID $listenerPid."
    }
    if ($process.ProcessName -notmatch '^pythonw?$') {
        throw "The verified service PID $listenerPid is not a Python process. Stop it manually before building."
    }

    Write-Host "Stopping verified DJI SMS service PID $listenerPid for a consistent snapshot..."
    Stop-Process -Id $listenerPid
    $deadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 250
        $remainingPid = Get-Port7597Pid
    } while ($remainingPid -and [DateTime]::UtcNow -lt $deadline)
    if ($remainingPid) {
        throw "PID $listenerPid did not release port 7597 within 15 seconds."
    }
    Write-Host 'The old-computer service is stopped and will remain stopped after the build.'
    return $true
}

function Test-ExcludedSnapshotPath {
    param([string]$RelativePath)

    $normalized = $RelativePath.Replace('/', '\')
    $topLevel = ($normalized -split '\\', 2)[0]
    if ($topLevel -in @('installer', 'dist', '__pycache__', '.git', '.agents', '.codex', '.pytest_cache')) {
        return $true
    }
    if ($normalized -in @(
        'app.log',
        'runtime-python.txt',
        'runtime-pythonw.txt',
        '生成迁移安装包.bat',
        'tools\Build-MigrationInstaller.ps1',
        'tools\Build-PublicInstaller.ps1',
        'tools\InstallerPackaging.psm1'
    )) {
        return $true
    }
    if ($normalized.EndsWith('.pyc', [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $false
}

function New-ProjectSnapshot {
    param(
        [string]$Destination
    )

    $requiredPrivateFiles = @('config.json', 'archive.jsonl', 'state.json', 'tg_state.json')
    foreach ($relativePath in $requiredPrivateFiles) {
        $path = Join-Path $projectRoot $relativePath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Private migration file is missing: $relativePath"
        }
    }
    foreach ($jsonName in @('config.json', 'state.json', 'tg_state.json')) {
        try {
            Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $projectRoot $jsonName) | ConvertFrom-Json | Out-Null
        } catch {
            throw "$jsonName is not valid UTF-8 JSON."
        }
    }

    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $files = @(
        Get-ChildItem -LiteralPath $projectRoot -Recurse -Force -File |
            Where-Object {
                $relative = [IO.Path]::GetRelativePath($projectRoot, $_.FullName)
                -not (Test-ExcludedSnapshotPath -RelativePath $relative)
            }
    )
    foreach ($file in $files) {
        $relative = [IO.Path]::GetRelativePath($projectRoot, $file.FullName)
        $target = Join-Path $Destination $relative
        $targetParent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetParent -PathType Container)) {
            New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
        }
        Copy-Item -LiteralPath $file.FullName -Destination $target
    }

    foreach ($required in @('app.py', 'static\index.html', 'tools\Bind-DjiAtPort.ps1', 'drivers\Quectel-Ports-30.0.65.2\qcser.inf')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Destination $required) -PathType Leaf)) {
            throw "Snapshot is missing required file: $required"
        }
    }

    return $files.Count
}

[void](Stop-VerifiedDjiServiceForSnapshot)

$buildId = [Guid]::NewGuid().ToString('N')
$stageRoot = Join-Path ([IO.Path]::GetTempPath()) "CTExcel-SMS-DJI-build-$buildId"
$stageProject = Join-Path $stageRoot 'project'

try {
    New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null
    $projectFileCount = New-ProjectSnapshot -Destination $stageProject
    New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
    $outputName = 'CTExcel-SMS-DJI-Migration-{0}.exe' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    $outputPath = Join-Path $OutputDirectory $outputName
    Import-Module (Join-Path $PSScriptRoot 'InstallerPackaging.psm1') -Force
    $result = New-CtExcelInstallerExecutable `
        -PackageKind migration `
        -ProjectDirectory $stageProject `
        -OutputPath $outputPath `
        -IncludesPrivateData $true `
        -IncludesDriver $true `
        -SkipSelfTest:$SkipSelfTest
    Write-Host ''
    Write-Host 'PRIVATE SINGLE-EXE MIGRATION PACKAGE BUILT'
    Write-Host "Path=$($result.path)"
    Write-Host "Bytes=$($result.bytes)"
    Write-Host "SHA256=$($result.sha256)"
    Write-Host "SnapshotFiles=$projectFileCount"
    Write-Host "BuiltUtc=$($result.built_utc)"
    Write-Host 'The package contains Telegram credentials and SMS history. Do not share it.'
    Write-Host 'No autostart entry was created. The old-computer SMS service remains stopped.'
} finally {
    $resolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
    if ($resolvedStage.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and
        ([IO.Path]::GetFileName($resolvedStage)).StartsWith('CTExcel-SMS-DJI-build-', [StringComparison]::Ordinal)) {
        if (Test-Path -LiteralPath $resolvedStage) {
            Remove-Item -LiteralPath $resolvedStage -Recurse -Force
        }
    } else {
        Write-Warning "Refusing to remove unexpected staging path: $resolvedStage"
    }
}
