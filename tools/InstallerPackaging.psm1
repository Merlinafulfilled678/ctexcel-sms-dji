Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-CtExcelFileHash {
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

function Assert-CtExcelAuthenticodeSigner {
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
    if (
        -not $signature.SignerCertificate -or
        $signature.SignerCertificate.Subject -notmatch [regex]::Escape($ExpectedSubject)
    ) {
        throw "$Label signer does not contain '$ExpectedSubject'."
    }
}

function New-CtExcelInstallerExecutable {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateSet('public', 'migration')]
        [string]$PackageKind,

        [Parameter(Mandatory)]
        [string]$ProjectDirectory,

        [Parameter(Mandatory)]
        [string]$OutputPath,

        [Parameter(Mandatory)]
        [bool]$IncludesPrivateData,

        [Parameter(Mandatory)]
        [bool]$IncludesDriver,

        [switch]$SkipSelfTest
    )

    $projectRoot = Split-Path -Parent $PSScriptRoot
    $assetRoot = Join-Path $projectRoot 'installer\assets'
    $bootstrapSource = Join-Path $projectRoot 'installer\bootstrap\InstallerBootstrap.cs'
    $bootstrapManifest = Join-Path $projectRoot 'installer\bootstrap\installer.manifest'
    $pythonInstaller = Join-Path $assetRoot 'python-3.14.5-amd64.exe'
    $powerShellInstaller = Join-Path $assetRoot 'PowerShell-7.6.4-win-x64.msi'
    $wheelRoot = Join-Path $assetRoot 'wheels'
    $csc = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

    $resolvedProject = [IO.Path]::GetFullPath($ProjectDirectory)
    if (-not (Test-Path -LiteralPath $resolvedProject -PathType Container)) {
        throw "Project payload directory not found: $resolvedProject"
    }
    $projectFiles = @(Get-ChildItem -LiteralPath $resolvedProject -Recurse -Force -File)
    if ($projectFiles.Count -eq 0) {
        throw "Project payload directory is empty: $resolvedProject"
    }
    if ($PackageKind -eq 'public' -and $IncludesPrivateData) {
        throw 'A public package cannot declare private data.'
    }
    if ($PackageKind -eq 'public' -and $IncludesDriver) {
        throw 'The public package cannot include the driver until redistribution permission is documented.'
    }

    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'The installer can only be built on 64-bit Windows.'
    }
    foreach ($requiredPath in @($csc, $bootstrapSource, $bootstrapManifest)) {
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Installer build input not found: $requiredPath"
        }
    }

    Assert-CtExcelFileHash -Path $pythonInstaller -ExpectedHash 'F9C09F5ED6F796FD1A8BC5DDFA41715A494B453C4781F0E35D5077CF9FA58F6D' -Label 'Python 3.14.5 installer'
    Assert-CtExcelAuthenticodeSigner -Path $pythonInstaller -ExpectedSubject 'Python Software Foundation' -Label 'Python 3.14.5 installer'
    Assert-CtExcelFileHash -Path $powerShellInstaller -ExpectedHash 'D11942DF52FD12470169797ABFA4781D9480EFDC81000BA4FA55A5B921ED8DD0' -Label 'PowerShell 7.6.4 MSI'
    Assert-CtExcelAuthenticodeSigner -Path $powerShellInstaller -ExpectedSubject 'Microsoft Corporation' -Label 'PowerShell 7.6.4 MSI'

    $lockPath = Join-Path $wheelRoot 'requirements-lock.txt'
    Assert-CtExcelFileHash -Path $lockPath -ExpectedHash 'DEFC42BE24A12A9C36722F9EF34723C740C3A45C2CA60568D76817D231A036E7' -Label 'Offline Python lock file'
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
        Assert-CtExcelFileHash -Path (Join-Path $wheelRoot $entry.Name) -ExpectedHash $entry.Hash -Label "Offline wheel $($entry.Name)"
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $buildId = [Guid]::NewGuid().ToString('N')
    $stageRoot = Join-Path ([IO.Path]::GetTempPath()) "CTExcel-SMS-DJI-package-$buildId"
    $projectZip = Join-Path $stageRoot 'project.zip'
    $wheelsZip = Join-Path $stageRoot 'wheels.zip'
    $payloadInfoSource = Join-Path $stageRoot 'PayloadInfo.cs'
    $selfTestResult = Join-Path $stageRoot 'self-test.txt'
    $resolvedOutput = [IO.Path]::GetFullPath($OutputPath)

    try {
        New-Item -ItemType Directory -Path $stageRoot | Out-Null
        [IO.Compression.ZipFile]::CreateFromDirectory(
            $resolvedProject,
            $projectZip,
            [IO.Compression.CompressionLevel]::Optimal,
            $false)
        [IO.Compression.ZipFile]::CreateFromDirectory(
            $wheelRoot,
            $wheelsZip,
            [IO.Compression.CompressionLevel]::Optimal,
            $false)

        $projectHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $projectZip).Hash
        $wheelsHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $wheelsZip).Hash
        $builtUtc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        $privateLiteral = if ($IncludesPrivateData) { 'true' } else { 'false' }
        $driverLiteral = if ($IncludesDriver) { 'true' } else { 'false' }
        $payloadInfo = @"
namespace CTExcelSmsDjiInstaller
{
    internal static class PayloadInfo
    {
        internal const string PackageKind = "$PackageKind";
        internal const bool IncludesPrivateData = $privateLiteral;
        internal const bool IncludesDriver = $driverLiteral;
        internal const string PythonFileName = "python-3.14.5-amd64.exe";
        internal const string PythonSha256 = "F9C09F5ED6F796FD1A8BC5DDFA41715A494B453C4781F0E35D5077CF9FA58F6D";
        internal const string PowerShellFileName = "PowerShell-7.6.4-win-x64.msi";
        internal const string PowerShellSha256 = "D11942DF52FD12470169797ABFA4781D9480EFDC81000BA4FA55A5B921ED8DD0";
        internal const string ProjectSha256 = "$projectHash";
        internal const string WheelsSha256 = "$wheelsHash";
        internal const string BuiltUtc = "$builtUtc";
        internal const int ProjectFileCount = $($projectFiles.Count);
        internal const int WheelCount = 14;
    }
}
"@
        [IO.File]::WriteAllText($payloadInfoSource, $payloadInfo, [Text.UTF8Encoding]::new($false))

        $outputParent = Split-Path -Parent $resolvedOutput
        if (-not (Test-Path -LiteralPath $outputParent -PathType Container)) {
            New-Item -ItemType Directory -Force -Path $outputParent | Out-Null
        }
        if (Test-Path -LiteralPath $resolvedOutput) {
            throw "Refusing to overwrite an existing installer: $resolvedOutput"
        }
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
            "/out:$resolvedOutput",
            $bootstrapSource,
            $payloadInfoSource
        )
        $compilerOutput = @(& $csc @compilerArguments 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "C# compiler failed with exit code $LASTEXITCODE.`n$($compilerOutput -join "`n")"
        }
        if (-not (Test-Path -LiteralPath $resolvedOutput -PathType Leaf)) {
            throw "Compiler reported success but output EXE is missing: $resolvedOutput"
        }

        if (-not $SkipSelfTest) {
            $selfTest = Start-Process -FilePath $resolvedOutput -ArgumentList @('--self-test', "--result=$selfTestResult") -WindowStyle Hidden -Wait -PassThru
            if ($selfTest.ExitCode -ne 0) {
                $detail = if (Test-Path -LiteralPath $selfTestResult) {
                    Get-Content -Raw -Encoding UTF8 -LiteralPath $selfTestResult
                } else {
                    'Self-test did not produce a result file.'
                }
                throw "Built EXE self-test failed with exit code $($selfTest.ExitCode): $detail"
            }
            $selfTestText = Get-Content -Raw -Encoding UTF8 -LiteralPath $selfTestResult
            if ($selfTestText -notmatch "^SELF_TEST_OK $PackageKind ") {
                throw "Built EXE self-test returned an unexpected result: $selfTestText"
            }
        }

        $outputItem = Get-Item -LiteralPath $resolvedOutput
        [pscustomobject]@{
            path = $outputItem.FullName
            bytes = $outputItem.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedOutput).Hash
            package_kind = $PackageKind
            includes_private_data = $IncludesPrivateData
            includes_driver = $IncludesDriver
            project_files = $projectFiles.Count
            built_utc = $builtUtc
        }
    } finally {
        $resolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
        if (
            $resolvedStage.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and
            ([IO.Path]::GetFileName($resolvedStage)).StartsWith('CTExcel-SMS-DJI-package-', [StringComparison]::Ordinal)
        ) {
            if (Test-Path -LiteralPath $resolvedStage) {
                Remove-Item -LiteralPath $resolvedStage -Recurse -Force
            }
        } else {
            Write-Warning "Refusing to remove unexpected packaging path: $resolvedStage"
        }
    }
}

Export-ModuleMember -Function New-CtExcelInstallerExecutable
