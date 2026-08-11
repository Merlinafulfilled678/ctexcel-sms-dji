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

$assetRoot = Join-Path $projectRoot 'installer\assets'
$bootstrapSource = Join-Path $projectRoot 'installer\bootstrap\InstallerBootstrap.cs'
$bootstrapManifest = Join-Path $projectRoot 'installer\bootstrap\installer.manifest'
$pythonInstaller = Join-Path $assetRoot 'python-3.14.5-amd64.exe'
$powerShellInstaller = Join-Path $assetRoot 'PowerShell-7.6.4-win-x64.msi'
$wheelRoot = Join-Path $assetRoot 'wheels'
$csc = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

function Assert-FileHash {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$ExpectedHash,

        [Parameter(Mandatory)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
    if ($actual -ne $ExpectedHash) {
        throw "$Label SHA-256 mismatch. Expected $ExpectedHash, got $actual."
    }
}

function Assert-AuthenticodeSigner {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$ExpectedSubject,

        [Parameter(Mandatory)]
        [string]$Label
    )

    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [Management.Automation.SignatureStatus]::Valid) {
        throw "$Label Authenticode signature is not valid: $($signature.Status)."
    }
    if (-not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notmatch [regex]::Escape($ExpectedSubject)) {
        throw "$Label signer does not contain '$ExpectedSubject'."
    }
}

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
        'tools\Build-MigrationInstaller.ps1'
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
        [string]$Destination,
        [string]$ZipPath
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

    [IO.Compression.ZipFile]::CreateFromDirectory(
        $Destination,
        $ZipPath,
        [IO.Compression.CompressionLevel]::Optimal,
        $false)
    return $files.Count
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw 'The migration installer can only be built on 64-bit Windows.'
}
if (-not (Test-Path -LiteralPath $csc -PathType Leaf)) {
    throw ".NET Framework x64 C# compiler not found: $csc"
}
if (-not (Test-Path -LiteralPath $bootstrapSource -PathType Leaf)) {
    throw "Installer bootstrap source not found: $bootstrapSource"
}
if (-not (Test-Path -LiteralPath $bootstrapManifest -PathType Leaf)) {
    throw "Installer manifest not found: $bootstrapManifest"
}

Assert-FileHash -Path $pythonInstaller -ExpectedHash 'F9C09F5ED6F796FD1A8BC5DDFA41715A494B453C4781F0E35D5077CF9FA58F6D' -Label 'Python 3.14.5 installer'
Assert-AuthenticodeSigner -Path $pythonInstaller -ExpectedSubject 'Python Software Foundation' -Label 'Python 3.14.5 installer'
Assert-FileHash -Path $powerShellInstaller -ExpectedHash 'D11942DF52FD12470169797ABFA4781D9480EFDC81000BA4FA55A5B921ED8DD0' -Label 'PowerShell 7.6.4 MSI'
Assert-AuthenticodeSigner -Path $powerShellInstaller -ExpectedSubject 'Microsoft Corporation' -Label 'PowerShell 7.6.4 MSI'

$lockPath = Join-Path $wheelRoot 'requirements-lock.txt'
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw "Offline Python lock file not found: $lockPath"
}
Assert-FileHash -Path $lockPath -ExpectedHash 'DEFC42BE24A12A9C36722F9EF34723C740C3A45C2CA60568D76817D231A036E7' -Label 'Offline Python lock file'
$wheelFiles = @(Get-ChildItem -LiteralPath $wheelRoot -Filter '*.whl' -File)
if ($wheelFiles.Count -ne 14) {
    throw "Expected 14 offline wheels, found $($wheelFiles.Count)."
}
$wheelHashManifest = Join-Path $wheelRoot 'SHA256SUMS.txt'
if (-not (Test-Path -LiteralPath $wheelHashManifest -PathType Leaf)) {
    throw "Offline wheel hash manifest not found: $wheelHashManifest"
}
$manifestEntries = @(
    Get-Content -Encoding UTF8 -LiteralPath $wheelHashManifest | ForEach-Object {
        if ($_ -notmatch '^([A-F0-9]{64})  (.+\.whl)$') {
            throw "Invalid wheel hash manifest line: $_"
        }
        [pscustomobject]@{ Hash = $Matches[1]; Name = $Matches[2] }
    }
)
if ($manifestEntries.Count -ne 14) {
    throw "Expected 14 wheel hash entries, found $($manifestEntries.Count)."
}
foreach ($entry in $manifestEntries) {
    $wheelPath = Join-Path $wheelRoot $entry.Name
    Assert-FileHash -Path $wheelPath -ExpectedHash $entry.Hash -Label "Offline wheel $($entry.Name)"
}

[void](Stop-VerifiedDjiServiceForSnapshot)

Add-Type -AssemblyName System.IO.Compression.FileSystem
$buildId = [Guid]::NewGuid().ToString('N')
$stageRoot = Join-Path ([IO.Path]::GetTempPath()) "CTExcel-SMS-DJI-build-$buildId"
$stageProject = Join-Path $stageRoot 'project'
$projectZip = Join-Path $stageRoot 'project.zip'
$wheelsZip = Join-Path $stageRoot 'wheels.zip'
$payloadInfoSource = Join-Path $stageRoot 'PayloadInfo.cs'
$selfTestResult = Join-Path $stageRoot 'self-test.txt'

try {
    New-Item -ItemType Directory -Force -Path $stageRoot | Out-Null
    $projectFileCount = New-ProjectSnapshot -Destination $stageProject -ZipPath $projectZip
    [IO.Compression.ZipFile]::CreateFromDirectory(
        $wheelRoot,
        $wheelsZip,
        [IO.Compression.CompressionLevel]::Optimal,
        $false)

    $projectHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $projectZip).Hash
    $wheelsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $wheelsZip).Hash
    $builtUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    $payloadInfo = @"
namespace CTExcelSmsDjiInstaller
{
    internal static class PayloadInfo
    {
        internal const string PythonFileName = "python-3.14.5-amd64.exe";
        internal const string PythonSha256 = "F9C09F5ED6F796FD1A8BC5DDFA41715A494B453C4781F0E35D5077CF9FA58F6D";
        internal const string PowerShellFileName = "PowerShell-7.6.4-win-x64.msi";
        internal const string PowerShellSha256 = "D11942DF52FD12470169797ABFA4781D9480EFDC81000BA4FA55A5B921ED8DD0";
        internal const string ProjectSha256 = "$projectHash";
        internal const string WheelsSha256 = "$wheelsHash";
        internal const string BuiltUtc = "$builtUtc";
        internal const int ProjectFileCount = $projectFileCount;
        internal const int WheelCount = 14;
    }
}
"@
    [IO.File]::WriteAllText($payloadInfoSource, $payloadInfo, [Text.UTF8Encoding]::new($false))

    New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
    $outputName = 'CTExcel-SMS-DJI-Migration-{0}.exe' -f (Get-Date -Format 'yyyyMMdd-HHmmss')
    $outputPath = Join-Path $OutputDirectory $outputName
    $compilerArguments = @(
        '/nologo',
        '/target:winexe',
        '/platform:x64',
        '/optimize+',
        '/codepage:65001',
        "/win32manifest:$bootstrapManifest",
        '/reference:System.dll',
        '/reference:System.Core.dll',
        '/reference:System.Drawing.dll',
        '/reference:System.IO.Compression.dll',
        '/reference:System.IO.Compression.FileSystem.dll',
        '/reference:System.Security.dll',
        '/reference:System.Windows.Forms.dll',
        "/resource:$projectZip,CTExcel.ProjectZip",
        "/resource:$wheelsZip,CTExcel.WheelsZip",
        "/resource:$pythonInstaller,CTExcel.PythonInstaller",
        "/resource:$powerShellInstaller,CTExcel.PowerShellInstaller",
        "/out:$outputPath",
        $bootstrapSource,
        $payloadInfoSource
    )
    $compilerOutput = @(& $csc @compilerArguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "C# compiler failed with exit code $LASTEXITCODE.`n$($compilerOutput -join "`n")"
    }
    if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
        throw "Compiler reported success but output EXE is missing: $outputPath"
    }

    if (-not $SkipSelfTest) {
        $selfTest = Start-Process -FilePath $outputPath -ArgumentList @('--self-test', "--result=$selfTestResult") -WindowStyle Hidden -Wait -PassThru
        if ($selfTest.ExitCode -ne 0) {
            $detail = if (Test-Path -LiteralPath $selfTestResult) {
                Get-Content -Raw -Encoding UTF8 -LiteralPath $selfTestResult
            } else {
                'Self-test did not produce a result file.'
            }
            throw "Built EXE self-test failed with exit code $($selfTest.ExitCode): $detail"
        }
        $selfTestText = Get-Content -Raw -Encoding UTF8 -LiteralPath $selfTestResult
        if ($selfTestText -notmatch '^SELF_TEST_OK') {
            throw "Built EXE self-test returned an unexpected result: $selfTestText"
        }
    }

    $outputItem = Get-Item -LiteralPath $outputPath
    $outputHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $outputPath).Hash
    Write-Host ''
    Write-Host 'PRIVATE SINGLE-EXE MIGRATION PACKAGE BUILT'
    Write-Host "Path=$($outputItem.FullName)"
    Write-Host "Bytes=$($outputItem.Length)"
    Write-Host "SHA256=$outputHash"
    Write-Host "SnapshotFiles=$projectFileCount"
    Write-Host "BuiltUtc=$builtUtc"
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
