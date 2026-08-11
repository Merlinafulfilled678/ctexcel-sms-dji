Set-StrictMode -Version Latest

function Get-DjiMi02Device {
    [CmdletBinding()]
    param()

    if (-not $IsWindows) {
        throw 'DJI device discovery is supported only on Windows.'
    }

    $pnputil = Join-Path $env:SystemRoot 'System32\pnputil.exe'
    if (-not (Test-Path -LiteralPath $pnputil -PathType Leaf)) {
        throw "pnputil.exe not found: $pnputil"
    }

    $output = @(& $pnputil /enum-devices /connected /deviceids 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "pnputil device query failed with exit code $exitCode."
    }

    $text = $output -join "`n"
    $instancePattern = '(?im)\bUSB\\VID_2CA3&PID_4006&MI_02\\[^\s]+'
    $instanceIds = @(
        [regex]::Matches($text, $instancePattern) |
            ForEach-Object { $_.Value.Trim() } |
            Sort-Object -Unique
    )
    if ($instanceIds.Count -ne 1) {
        throw "Expected exactly one connected DJI MI_02 interface; found $($instanceIds.Count)."
    }

    $instanceId = $instanceIds[0]
    $blocks = [regex]::Split($text, '(?:\r?\n){2,}')
    $block = @(
        $blocks | Where-Object { $_ -match [regex]::Escape($instanceId) }
    ) | Select-Object -First 1
    if (-not $block) {
        throw 'DJI MI_02 device details were not present in pnputil output.'
    }

    $portMatch = [regex]::Match($block, '(?i)\((COM\d+)\)')
    $driverMatch = [regex]::Match($block, '(?i)\boem\d+\.inf\b')
    [pscustomobject]@{
        InstanceId = $instanceId
        PortName = if ($portMatch.Success) { $portMatch.Groups[1].Value.ToUpperInvariant() } else { $null }
        DriverName = if ($driverMatch.Success) { $driverMatch.Value.ToLowerInvariant() } else { $null }
        IsQuectelAtPort = $block -match '(?i)Quectel USB AT Port'
    }
}

function Resolve-DjiAtPortName {
    [CmdletBinding()]
    param(
        [ValidatePattern('^COM\d+$')]
        [string]$PortName
    )

    if ($PortName) {
        return $PortName.ToUpperInvariant()
    }

    $device = Get-DjiMi02Device
    if (-not $device.IsQuectelAtPort -or -not $device.PortName) {
        throw 'DJI MI_02 is connected but is not bound to Quectel USB AT Port. Install the bundled AT driver first.'
    }
    return $device.PortName
}
