[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$ServicePort = 7597,

    [switch]$SkipDevice,

    [switch]$SkipTelegramProxy,

    [switch]$AllowMissingBundledDriver
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'DjiDeviceDiscovery.ps1')

$checks = [Collections.Generic.List[object]]::new()
function Add-Check {
    param(
        [ValidateSet('PASS', 'WARN', 'FAIL')]
        [string]$Status,

        [string]$Name,

        [string]$Detail
    )

    $checks.Add([pscustomobject]@{
        Status = $Status
        Check = $Name
        Detail = $Detail
    })
}

if (-not $IsWindows) {
    Add-Check FAIL 'Operating system' 'This migration package supports Windows only.'
} else {
    $build = [Environment]::OSVersion.Version.Build
    if ([Environment]::Is64BitOperatingSystem -and $build -ge 22000) {
        Add-Check PASS 'Operating system' "Windows 11 x64 build $build"
    } elseif ([Environment]::Is64BitOperatingSystem) {
        Add-Check WARN 'Operating system' "Windows x64 build $build; only Windows 11 has been field-tested."
    } else {
        Add-Check FAIL 'Operating system' 'A 64-bit Windows installation is required by the bundled driver.'
    }
}

if ($PSVersionTable.PSVersion.Major -ge 7) {
    Add-Check PASS 'PowerShell' "PowerShell $($PSVersionTable.PSVersion)"
} else {
    Add-Check FAIL 'PowerShell' 'PowerShell 7 or newer is required.'
}

$requiredProjectFiles = @(
    'app.py',
    'modem_profile.py',
    'carrier_profile.py',
    'config_store.py',
    'tg_bot.py',
    'requirements.txt',
    '启动短信工具.bat',
    'static\index.html',
    'config.json',
    'archive.jsonl',
    'state.json',
    'tg_state.json'
)
$missingProjectFiles = @(
    $requiredProjectFiles | Where-Object {
        -not (Test-Path -LiteralPath (Join-Path $projectRoot $_) -PathType Leaf)
    }
)
if ($missingProjectFiles.Count -eq 0) {
    Add-Check PASS 'Project files' 'Application, configuration, archive and state files are present.'
} else {
    Add-Check FAIL 'Project files' ("Missing: " + ($missingProjectFiles -join ', '))
}

$runtimePythonFile = Join-Path $projectRoot 'runtime-python.txt'
$runtimePython = $null
if (Test-Path -LiteralPath $runtimePythonFile -PathType Leaf) {
    $candidate = (Get-Content -Raw -Encoding UTF8 -LiteralPath $runtimePythonFile).Trim()
    if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        $runtimePython = [IO.Path]::GetFullPath($candidate)
    } else {
        Add-Check FAIL 'Installed Python binding' 'runtime-python.txt does not point to an existing file.'
    }
}
$pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
$pythonSource = if ($runtimePython) { $runtimePython } elseif ($pathPython) { $pathPython.Source } else { $null }
if (-not $pythonSource) {
    Add-Check FAIL 'Python' 'Python is neither bound by the installer nor available in PATH.'
} else {
    $probeCode = @'
import importlib.metadata as metadata
import json
import sys

packages = {}
for name in ("Flask", "pyserial", "requests"):
    try:
        packages[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        packages[name] = None

print(json.dumps({"python": list(sys.version_info[:3]), "packages": packages}))
'@
    $probeOutput = @(& $pythonSource -c $probeCode 2>&1)
    $probeExitCode = $LASTEXITCODE
    if ($probeExitCode -ne 0) {
        Add-Check FAIL 'Python' "Python probe failed with exit code $probeExitCode."
    } else {
        try {
            $probe = ($probeOutput | Select-Object -Last 1) | ConvertFrom-Json
            $pythonVersion = ($probe.python -join '.')
            if ($probe.python[0] -eq 3 -and $probe.python[1] -eq 14) {
                Add-Check PASS 'Python' "Python $pythonVersion"
            } elseif ($probe.python[0] -eq 3 -and $probe.python[1] -ge 10) {
                Add-Check WARN 'Python' "Python $pythonVersion; validated production version is 3.14.5."
            } else {
                Add-Check FAIL 'Python' "Python $pythonVersion is not supported."
            }

            $expectedPackages = [ordered]@{
                Flask = '3.1.3'
                pyserial = '3.5'
                requests = '2.34.2'
            }
            foreach ($packageName in $expectedPackages.Keys) {
                $actualVersion = $probe.packages.$packageName
                if (-not $actualVersion) {
                    Add-Check FAIL "Python package $packageName" 'Not installed.'
                } elseif ($actualVersion -eq $expectedPackages[$packageName]) {
                    Add-Check PASS "Python package $packageName" $actualVersion
                } else {
                    Add-Check WARN "Python package $packageName" (
                        "$actualVersion installed; validated version is $($expectedPackages[$packageName])."
                    )
                }
            }
        } catch {
            Add-Check FAIL 'Python' 'Python probe returned invalid data.'
        }
    }
}

$boundPythonw = if ($runtimePython) {
    $candidate = Join-Path (Split-Path -Parent $runtimePython) 'pythonw.exe'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $candidate } else { $null }
} else {
    $null
}
$pythonw = Get-Command pythonw.exe -ErrorAction SilentlyContinue
$pyw = Get-Command pyw.exe -ErrorAction SilentlyContinue
if ($boundPythonw -or $pythonw -or $pyw) {
    Add-Check PASS 'Background Python launcher' 'The installer binding, pythonw.exe or pyw.exe is available.'
} else {
    Add-Check FAIL 'Background Python launcher' 'The startup script requires a valid installer binding, pythonw.exe or pyw.exe.'
}

$driverRoot = Join-Path $projectRoot 'drivers\Quectel-Ports-30.0.65.2'
$expectedDriverHashes = [ordered]@{
    'qcser.cat' = '84511642502CF1398C6B859303C1AD87FA1BEF6CCA65CDA2CCF1D741D1004F2D'
    'qcser.inf' = 'ECD9EBD5337D32B6ED9CB0AE5599BFA3DFD77EE375723F82ECC46EF732D5F037'
    'serial\amd64\qcusbser.sys' = '4FFB594F274B597740DBE1BC698492D4D447E294188339370BE12A2C764DBD9A'
}
$driverFailures = [Collections.Generic.List[string]]::new()
foreach ($relativePath in $expectedDriverHashes.Keys) {
    $filePath = Join-Path $driverRoot $relativePath
    if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
        $driverFailures.Add("missing $relativePath")
        continue
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath).Hash
    if ($actualHash -ne $expectedDriverHashes[$relativePath]) {
        $driverFailures.Add("hash mismatch $relativePath")
    }
}
if ($driverFailures.Count -eq 0) {
    $catalogPath = Join-Path $driverRoot 'qcser.cat'
    $signature = Get-AuthenticodeSignature -LiteralPath $catalogPath
    if (
        $signature.Status -eq [System.Management.Automation.SignatureStatus]::Valid -and
        $signature.SignerCertificate.Subject -match 'Microsoft Windows Hardware Compatibility Publisher'
    ) {
        Add-Check PASS 'Bundled AT driver' 'Quectel Ports 30.0.65.2 hashes and Microsoft WHCP signature are valid.'
    } else {
        Add-Check FAIL 'Bundled AT driver' 'The driver catalog signature is not valid Microsoft WHCP.'
    }
} else {
    if ($AllowMissingBundledDriver) {
        Add-Check WARN 'Bundled AT driver' 'The public package does not redistribute the Quectel driver; install an authorized copy separately if needed.'
    } else {
        Add-Check FAIL 'Bundled AT driver' ($driverFailures -join '; ')
    }
}

if ($SkipDevice) {
    Add-Check WARN 'DJI MI_02 device' 'Device check skipped by request.'
} else {
    try {
        $device = Get-DjiMi02Device
        if ($device.IsQuectelAtPort -and $device.PortName) {
            Add-Check PASS 'DJI MI_02 device' "Quectel USB AT Port is ready on $($device.PortName)."
        } else {
            Add-Check FAIL 'DJI MI_02 device' 'Device is connected but the Quectel AT driver is not bound.'
        }

        if ($device.DriverName) {
            $pnputil = Join-Path $env:SystemRoot 'System32\pnputil.exe'
            $driverOutput = @(& $pnputil /enum-drivers /class Ports 2>&1)
            if ($LASTEXITCODE -eq 0) {
                $driverBlocks = [regex]::Split(($driverOutput -join "`n"), '(?:\r?\n){2,}')
                $installedBlock = @(
                    $driverBlocks | Where-Object { $_ -match [regex]::Escape($device.DriverName) }
                ) | Select-Object -First 1
                if (
                    $installedBlock -match '(?i)Quectel Incorporated' -and
                    $installedBlock -match '30\.0\.65\.2'
                ) {
                    Add-Check PASS 'Installed AT driver' 'Quectel Ports 30.0.65.2 is active.'
                } else {
                    Add-Check WARN 'Installed AT driver' 'The active driver is not the validated Quectel 30.0.65.2 package.'
                }
            } else {
                Add-Check WARN 'Installed AT driver' 'Could not enumerate installed Ports drivers.'
            }
        }
    } catch {
        Add-Check FAIL 'DJI MI_02 device' $_.Exception.Message
    }
}

try {
    $pnputil = Join-Path $env:SystemRoot 'System32\pnputil.exe'
    $netOutput = @(& $pnputil /enum-devices /connected /class Net 2>&1)
    if ($LASTEXITCODE -ne 0) {
        Add-Check WARN 'WWAN safety' 'Could not enumerate connected Windows Net devices.'
    } elseif (($netOutput -join "`n") -match '(?i)DJI|Quectel|QDC507') {
        Add-Check FAIL 'WWAN safety' 'A matching cellular network adapter is present. Do not start until it is removed or safely disabled.'
    } else {
        Add-Check PASS 'WWAN safety' 'No DJI or Quectel Windows network adapter is present.'
    }
} catch {
    Add-Check WARN 'WWAN safety' 'WWAN device check failed.'
}

$serviceUri = "http://127.0.0.1:$ServicePort/api/status"
try {
    $status = Invoke-RestMethod -Uri $serviceUri -TimeoutSec 2
    if ($status.carrier.key -eq 'ctexcel' -and $status.modem.key -eq 'dji_qdc507') {
        Add-Check PASS 'Service port' "The DJI service is already running on 127.0.0.1:$ServicePort."
    } else {
        Add-Check FAIL 'Service port' "Port $ServicePort is occupied by a different HTTP service."
    }
} catch {
    $listenerPattern = "^\s*TCP\s+\S+:$ServicePort\s+\S+\s+LISTENING\s+\d+\s*$"
    $listener = netstat -ano -p tcp | Where-Object { $_ -match $listenerPattern } | Select-Object -First 1
    if ($listener) {
        Add-Check FAIL 'Service port' "Port $ServicePort is occupied by another process."
    } else {
        Add-Check PASS 'Service port' "Port $ServicePort is free."
    }
}

$configPath = Join-Path $projectRoot 'config.json'
if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    try {
        $config = Get-Content -Raw -Encoding UTF8 -LiteralPath $configPath | ConvertFrom-Json
        $telegram = $config.telegram
        if ($telegram -and $telegram.token -and $null -ne $telegram.chat_id) {
            Add-Check PASS 'Telegram configuration' 'Token and owner binding are present; secret values were not displayed.'
        } else {
            Add-Check WARN 'Telegram configuration' 'Telegram is not fully configured; Web and local SMS can still work.'
        }

        if ($telegram -and $telegram.proxy) {
            try {
                $proxyUri = [uri]$telegram.proxy
                if (-not $proxyUri.IsLoopback) {
                    Add-Check WARN 'Telegram proxy' 'The configured proxy is not loopback; review it on the new computer.'
                } elseif ($SkipTelegramProxy) {
                    Add-Check WARN 'Telegram proxy' 'Loopback proxy connectivity check skipped by request.'
                } else {
                    $proxyPort = if ($proxyUri.IsDefaultPort) { 80 } else { $proxyUri.Port }
                    $client = [Net.Sockets.TcpClient]::new()
                    try {
                        $connectTask = $client.ConnectAsync($proxyUri.Host, $proxyPort)
                        $connected = $connectTask.Wait(1000) -and $client.Connected
                    } catch {
                        $connected = $false
                    } finally {
                        $client.Dispose()
                    }
                    if ($connected) {
                        Add-Check PASS 'Telegram proxy' 'The configured loopback proxy is accepting connections.'
                    } else {
                        Add-Check WARN 'Telegram proxy' 'The configured loopback proxy is not running; Telegram will retry without affecting SMS/Web.'
                    }
                }
            } catch {
                Add-Check FAIL 'Telegram proxy' 'The proxy value in config.json is not a valid URI.'
            }
        } else {
            Add-Check WARN 'Telegram proxy' 'No proxy is configured; Telegram will be disabled.'
        }
    } catch {
        Add-Check FAIL 'Telegram configuration' 'config.json is not valid UTF-8 JSON.'
    }
}

$checks | Format-Table Status, Check, Detail -AutoSize -Wrap
$passCount = @($checks | Where-Object { $_.Status -eq 'PASS' }).Count
$warnCount = @($checks | Where-Object { $_.Status -eq 'WARN' }).Count
$failCount = @($checks | Where-Object { $_.Status -eq 'FAIL' }).Count
Write-Output "SUMMARY PASS=$passCount WARN=$warnCount FAIL=$failCount"

if ($failCount -gt 0) {
    exit 2
}
exit 0
