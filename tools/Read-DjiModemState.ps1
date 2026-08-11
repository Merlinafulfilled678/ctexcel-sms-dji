[CmdletBinding()]
param(
    [ValidatePattern('^COM\d+$')]
    [string]$PortName,

    [ValidateRange(1200, 921600)]
    [int]$BaudRate = 115200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'DjiDeviceDiscovery.ps1')
$PortName = Resolve-DjiAtPortName -PortName $PortName

# This allowlist intentionally contains query commands only. It excludes IMSI,
# ICCID, IMEI, phonebook, SMS-body, send, PDP-context and data-session commands.
$queries = @(
    'AT',
    'ATI',
    'AT+GMI',
    'AT+GMM',
    'AT+QGMR',
    'AT+CPIN?',
    'AT+QSIMSTAT?',
    'AT+CFUN?',
    'AT+CSQ',
    'AT+COPS?',
    'AT+CREG?',
    'AT+CGREG?',
    'AT+CEREG?',
    'AT+CIREG?',
    'AT+QCFG="usbcfg"',
    'AT+QCFG="ims"',
    'AT+QCFG="volte_disable"',
    'AT+QMBNCFG="AutoSel"',
    'AT+QMBNCFG="List"',
    'AT+QCFG="ltesms/format"',
    'AT+CMGF?',
    'AT+CNMI?',
    'AT+CPMS?',
    'AT+CSMS?',
    'AT+CGSMS?'
)

function Protect-AtResponse {
    param(
        [Parameter(Mandatory)]
        [string]$Command,

        [Parameter(Mandatory)]
        [string]$Response
    )

    $safe = $Response
    $safe = [regex]::Replace(
        $safe,
        '(?i)(IMEI|IMSI|ICCID|QCCID|CIMI)(\s*[:=]?\s*)\d{7,}',
        '$1$2<redacted>')
    $safe = [regex]::Replace($safe, '(?<!\d)\d{14,16}(?!\d)', '<redacted>')

    if ($Command -in @('AT+CREG?', 'AT+CGREG?', 'AT+CEREG?', 'AT+CIREG?')) {
        # Registration replies can include tracking-area and cell identifiers.
        $safe = [regex]::Replace($safe, '"[0-9A-Fa-f]{4,}"', '"<redacted>"')
    }

    return $safe.Trim()
}

function Read-AtResponse {
    param(
        [Parameter(Mandatory)]
        [System.IO.Ports.SerialPort]$Port,

        [ValidateRange(500, 30000)]
        [int]$TimeoutMilliseconds = 6000
    )

    $builder = [Text.StringBuilder]::new()
    $started = [DateTime]::UtcNow
    $lastData = $started

    while (([DateTime]::UtcNow - $started).TotalMilliseconds -lt $TimeoutMilliseconds) {
        Start-Sleep -Milliseconds 50
        $chunk = $Port.ReadExisting()
        if ($chunk.Length -gt 0) {
            [void]$builder.Append($chunk)
            $lastData = [DateTime]::UtcNow
        }

        $text = $builder.ToString()
        $hasTerminal = $text -match '(?m)^\s*(OK|ERROR|\+CME ERROR:.*|\+CMS ERROR:.*)\s*$'
        $quietMilliseconds = ([DateTime]::UtcNow - $lastData).TotalMilliseconds
        if ($hasTerminal -and $quietMilliseconds -ge 200) {
            return $text
        }
    }

    return $builder.ToString() + "`r`n<TIMEOUT>"
}

$availablePorts = [System.IO.Ports.SerialPort]::GetPortNames()
if ($PortName -notin $availablePorts) {
    throw "Serial port $PortName is not present. Present ports: $($availablePorts -join ', ')"
}

$serial = [System.IO.Ports.SerialPort]::new(
    $PortName,
    $BaudRate,
    [System.IO.Ports.Parity]::None,
    8,
    [System.IO.Ports.StopBits]::One)
$serial.Handshake = [System.IO.Ports.Handshake]::None
$serial.DtrEnable = $false
$serial.RtsEnable = $false
$serial.ReadTimeout = 250
$serial.WriteTimeout = 2000
$serial.Encoding = [Text.Encoding]::ASCII
$serial.NewLine = "`r`n"

try {
    $serial.Open()
    Write-Output "Port=$PortName"
    Write-Output "BaudRate=$BaudRate"
    Write-Output 'Mode=READ_ONLY_QUERY_ALLOWLIST'

    foreach ($command in $queries) {
        $serial.DiscardInBuffer()
        $serial.DiscardOutBuffer()
        $serial.Write($command + "`r")
        $response = Read-AtResponse -Port $serial
        $safeResponse = Protect-AtResponse -Command $command -Response $response

        Write-Output "--- $command ---"
        Write-Output $safeResponse
    }
}
finally {
    if ($serial.IsOpen) {
        $serial.Close()
    }
    $serial.Dispose()
}
