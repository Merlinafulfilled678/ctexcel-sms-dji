[CmdletBinding()]
param(
    [switch]$DescribePayload,

    [string]$StagePayloadDirectory,

    [string]$ValidatePayloadDirectory,

    [string]$OutputDirectory,

    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$')]
    [string]$Version = '0.9.3-beta',

    [switch]$SkipSelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $projectRoot 'dist'
}

$publicPayloadFiles = @(
    'CONTRIBUTING.md',
    'DJI-QDC507-CTEXCEL.md',
    'LICENSE',
    'README.md',
    'SECURITY.md',
    'THIRD_PARTY_NOTICES.md',
    'app.py',
    'carrier_profile.py',
    'config.example.json',
    'config_store.py',
    'diag_readonly.py',
    'drivers\README.md',
    'modem_profile.py',
    'requirements.txt',
    'static\index.html',
    'tg_bot.py',
    'tools\DjiDeviceDiscovery.ps1',
    'tools\Test-NewPcReadiness.ps1',
    'tools\Uninstall-CtExcelSmsDji.ps1',
    '卸载短信工具.bat',
    '启动短信工具.bat'
)

function Get-PublicPayloadDescription {
    [ordered]@{
        package_kind = 'public'
        includes_private_data = $false
        includes_driver = $false
        payload_files = $publicPayloadFiles
    }
}

function New-PublicPayload {
    param(
        [Parameter(Mandatory)]
        [string]$Destination
    )

    $stageRoot = [IO.Path]::GetFullPath($Destination)
    if (Test-Path -LiteralPath $stageRoot) {
        if (-not (Test-Path -LiteralPath $stageRoot -PathType Container)) {
            throw "Public payload staging target is not a directory: $stageRoot"
        }
        if (@(Get-ChildItem -LiteralPath $stageRoot -Force).Count -ne 0) {
            throw "Public payload staging directory must be empty: $stageRoot"
        }
    } else {
        New-Item -ItemType Directory -Path $stageRoot | Out-Null
    }

    foreach ($relativePath in $publicPayloadFiles) {
        $source = Join-Path $projectRoot $relativePath
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Declared public payload file is missing: $relativePath"
        }
        $target = Join-Path $stageRoot $relativePath
        $targetParent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetParent -PathType Container)) {
            New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
        }
        Copy-Item -LiteralPath $source -Destination $target
    }
}

function Test-PublicPayload {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    $payloadRoot = [IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $payloadRoot -PathType Container)) {
        throw "Public payload directory not found: $payloadRoot"
    }
    $expectedFiles = @($publicPayloadFiles | ForEach-Object { $_.Replace('\', '/') })
    $actualFiles = @(
        Get-ChildItem -LiteralPath $payloadRoot -Recurse -Force -File | ForEach-Object {
            [IO.Path]::GetRelativePath($payloadRoot, $_.FullName).Replace('\', '/')
        }
    )
    $missing = @($expectedFiles | Where-Object { $_ -notin $actualFiles })
    $unexpected = @($actualFiles | Where-Object { $_ -notin $expectedFiles })
    if ($missing.Count -ne 0 -or $unexpected.Count -ne 0) {
        throw "Public payload file set mismatch. Missing=$($missing -join ','); Unexpected=$($unexpected -join ',')"
    }

    $telegramTokenPattern = '(?<![A-Za-z0-9_-])[0-9]{6,12}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])'
    foreach ($relativePath in $actualFiles) {
        $filePath = Join-Path $payloadRoot $relativePath
        $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $filePath
        if ($content -match $telegramTokenPattern) {
            throw "Potential Telegram token detected in public payload file: $relativePath"
        }
    }
}

if ($DescribePayload) {
    Get-PublicPayloadDescription | ConvertTo-Json -Depth 3
    exit 0
}

if ($StagePayloadDirectory) {
    New-PublicPayload -Destination $StagePayloadDirectory
    Test-PublicPayload -Path $StagePayloadDirectory
    Get-PublicPayloadDescription | ConvertTo-Json -Depth 3
    exit 0
}

if ($ValidatePayloadDirectory) {
    Test-PublicPayload -Path $ValidatePayloadDirectory
    Get-PublicPayloadDescription | ConvertTo-Json -Depth 3
    exit 0
}

$buildId = [Guid]::NewGuid().ToString('N')
$stageRoot = Join-Path ([IO.Path]::GetTempPath()) "CTExcel-SMS-DJI-public-stage-$buildId"
try {
    New-PublicPayload -Destination $stageRoot
    Test-PublicPayload -Path $stageRoot
    Import-Module (Join-Path $PSScriptRoot 'InstallerPackaging.psm1') -Force
    $outputPath = Join-Path ([IO.Path]::GetFullPath($OutputDirectory)) "CTExcel-SMS-DJI-Setup-v$Version.exe"
    $result = New-CtExcelInstallerExecutable `
        -PackageKind public `
        -ProjectDirectory $stageRoot `
        -OutputPath $outputPath `
        -IncludesPrivateData $false `
        -IncludesDriver $false `
        -SkipSelfTest:$SkipSelfTest
    $sumPath = Join-Path ([IO.Path]::GetFullPath($OutputDirectory)) 'SHA256SUMS.txt'
    $sumLine = "$($result.sha256)  $([IO.Path]::GetFileName($result.path))`n"
    [IO.File]::WriteAllText($sumPath, $sumLine, [Text.UTF8Encoding]::new($false))
    $result | ConvertTo-Json -Depth 3
} finally {
    $resolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
    if (
        $resolvedStage.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and
        ([IO.Path]::GetFileName($resolvedStage)).StartsWith('CTExcel-SMS-DJI-public-stage-', [StringComparison]::Ordinal)
    ) {
        if (Test-Path -LiteralPath $resolvedStage) {
            Remove-Item -LiteralPath $resolvedStage -Recurse -Force
        }
    } else {
        Write-Warning "Refusing to remove unexpected public staging path: $resolvedStage"
    }
}
