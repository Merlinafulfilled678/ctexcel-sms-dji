[CmdletBinding()]
param(
    [ValidatePattern('^COM\d+$')]
    [string]$PortName,

    [string]$ExpectedBody = 'DJI-TEST-0807',

    [ValidateRange(30, 900)]
    [int]$TimeoutSeconds = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'DjiDeviceDiscovery.ps1')
$PortName = Resolve-DjiAtPortName -PortName $PortName

function New-ProbePort {
    $port = [System.IO.Ports.SerialPort]::new(
        $PortName,
        115200,
        [System.IO.Ports.Parity]::None,
        8,
        [System.IO.Ports.StopBits]::One)
    $port.Handshake = [System.IO.Ports.Handshake]::None
    $port.DtrEnable = $false
    $port.RtsEnable = $false
    $port.ReadTimeout = 250
    $port.WriteTimeout = 2000
    $port.Encoding = [Text.Encoding]::ASCII
    return $port
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
        if ($hasTerminal -and ([DateTime]::UtcNow - $lastData).TotalMilliseconds -ge 200) {
            return $text.Trim()
        }
    }

    return ($builder.ToString() + "`r`n<TIMEOUT>").Trim()
}

function Invoke-At {
    param(
        [Parameter(Mandatory)]
        [System.IO.Ports.SerialPort]$Port,

        [Parameter(Mandatory)]
        [string]$Command,

        [ValidateRange(500, 30000)]
        [int]$TimeoutMilliseconds = 6000
    )

    $Port.DiscardInBuffer()
    $Port.DiscardOutBuffer()
    $Port.Write($Command + "`r")
    $response = Read-AtResponse -Port $Port -TimeoutMilliseconds $TimeoutMilliseconds
    if ($response -match '(?m)^\s*(ERROR|\+CME ERROR:.*|\+CMS ERROR:.*)\s*$' -or
        $response -notmatch '(?m)^\s*OK\s*$') {
        throw "AT command failed: $Command`n$response"
    }
    return $response
}

function Get-StoredCount {
    param(
        [Parameter(Mandatory)]
        [string]$CpmsResponse
    )

    $match = [regex]::Match($CpmsResponse, '\+CPMS:\s*"[^"]+",(\d+),')
    if (-not $match.Success) {
        throw 'Unable to parse the current SMS storage count.'
    }
    return [int]$match.Groups[1].Value
}

function Split-AtCsvFields {
    param(
        [Parameter(Mandatory)]
        [string]$Value
    )

    $fields = [Collections.Generic.List[string]]::new()
    $builder = [Text.StringBuilder]::new()
    $quoted = $false

    for ($index = 0; $index -lt $Value.Length; $index++) {
        $character = $Value[$index]
        if ($character -eq '"') {
            if ($quoted -and $index + 1 -lt $Value.Length -and $Value[$index + 1] -eq '"') {
                [void]$builder.Append('"')
                $index++
            }
            else {
                $quoted = -not $quoted
            }
            continue
        }
        if ($character -eq ',' -and -not $quoted) {
            [void]$fields.Add($builder.ToString().Trim())
            [void]$builder.Clear()
            continue
        }
        [void]$builder.Append($character)
    }
    [void]$fields.Add($builder.ToString().Trim())
    return $fields.ToArray()
}

function Convert-HexToBytes {
    param(
        [Parameter(Mandatory)]
        [string]$Hex
    )

    $bytes = [byte[]]::new($Hex.Length / 2)
    for ($index = 0; $index -lt $bytes.Length; $index++) {
        $bytes[$index] = [Convert]::ToByte($Hex.Substring($index * 2, 2), 16)
    }
    return $bytes
}

function Convert-SmsBody {
    param(
        [Parameter(Mandatory)]
        [string]$Body,

        [AllowNull()]
        [Nullable[int]]$Dcs
    )

    $compact = -join ($Body -split '\s+')
    $isEvenHex = $compact.Length -gt 0 -and
        $compact.Length % 2 -eq 0 -and
        $compact -match '^[0-9A-Fa-f]+$'
    $isUcs2 = $null -ne $Dcs -and ((([int]$Dcs) -band 0x0C) -eq 0x08)
    if ($isUcs2 -and $isEvenHex -and $compact.Length % 4 -eq 0) {
        try {
            return [Text.Encoding]::BigEndianUnicode.GetString(
                (Convert-HexToBytes -Hex $compact)).Trim([char]0)
        }
        catch {
            return $Body
        }
    }
    return $Body
}

function Get-TextBodyFromCmgr {
    param(
        [Parameter(Mandatory)]
        [string]$Response
    )

    $lines = @($Response.Replace("`r", '').Split("`n"))
    $headerIndex = -1
    $dcs = $null
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match '^\s*\+CMGR:\s*(?<fields>.*)$') {
            $headerIndex = $index
            $fields = @(Split-AtCsvFields -Value $Matches['fields'])
            if ($fields.Count -gt 7) {
                $parsedDcs = 0
                if ([int]::TryParse($fields[7], [ref]$parsedDcs)) {
                    $dcs = $parsedDcs
                }
            }
            break
        }
    }
    if ($headerIndex -lt 0) {
        return $null
    }

    $bodyLines = [Collections.Generic.List[string]]::new()
    for ($index = $headerIndex + 1; $index -lt $lines.Count; $index++) {
        $line = $lines[$index]
        if ($line.Trim() -eq 'OK') {
            break
        }
        if ($line.TrimStart().StartsWith('+', [StringComparison]::Ordinal)) {
            continue
        }
        [void]$bodyLines.Add($line)
    }

    $body = (($bodyLines -join "`n").Trim())
    return Convert-SmsBody -Body $body -Dcs $dcs
}

function Test-ResponseContainsExpectedBody {
    param(
        [Parameter(Mandatory)]
        [string]$Response,

        [Parameter(Mandatory)]
        [string]$Expected
    )

    if ($Response.Contains($Expected, [StringComparison]::Ordinal)) {
        return $true
    }
    $compactResponse = -join ($Response -split '\s+')
    $expectedUcs2 = [Convert]::ToHexString([Text.Encoding]::BigEndianUnicode.GetBytes($Expected))
    return $compactResponse.Contains($expectedUcs2, [StringComparison]::OrdinalIgnoreCase)
}

function Write-SafeResult {
    param(
        [Parameter(Mandatory)]
        [string]$DeliveryMode,

        [string]$Storage,

        [int]$Index = -1,

        [AllowNull()]
        [string]$Body
    )

    if ($null -eq $Body) {
        $Body = ''
    }
    $matchesExpected = $Body -ceq $ExpectedBody
    Write-Output 'SmsReceived=True'
    Write-Output "DeliveryMode=$DeliveryMode"
    if ($Storage) {
        Write-Output "Storage=$Storage"
    }
    if ($Index -ge 0) {
        Write-Output "Index=$Index"
    }
    Write-Output 'Sender=<redacted>'
    Write-Output "BodyLength=$($Body.Length)"
    Write-Output "ExpectedBodyMatch=$matchesExpected"
    if ($matchesExpected) {
        Write-Output "Body=$ExpectedBody"
    }
    else {
        Write-Output 'Body=<redacted-unexpected-message>'
    }
}

if ($PortName -notin [System.IO.Ports.SerialPort]::GetPortNames()) {
    throw "Serial port is not present: $PortName"
}

$serial = New-ProbePort
try {
    $serial.Open()
    [void](Invoke-At -Port $serial -Command 'AT')
    [void](Invoke-At -Port $serial -Command 'AT+CMGF=1')
    [void](Invoke-At -Port $serial -Command 'AT+CSDH=1')
    [void](Invoke-At -Port $serial -Command 'AT+CPMS="ME","ME","ME"')
    [void](Invoke-At -Port $serial -Command 'AT+CNMI=2,1,0,0,0')

    $ims = Invoke-At -Port $serial -Command 'AT+QCFG="ims"'
    if ($ims -notmatch '\+QCFG:\s*"ims",1,1') {
        throw "IMS is not registered immediately before the SMS probe: $ims"
    }
    $registration = Invoke-At -Port $serial -Command 'AT+CEREG?'
    if ($registration -notmatch '\+CEREG:\s*\d+,(1|5)(?:\D|$)') {
        throw "EPS is not registered immediately before the SMS probe: $registration"
    }

    $baselineCpms = Invoke-At -Port $serial -Command 'AT+CPMS?'
    $baselineCount = Get-StoredCount -CpmsResponse $baselineCpms
    Write-Output "ProbeReady=True"
    Write-Output "Port=$PortName"
    Write-Output "BaselineStoredCount=$baselineCount"
    Write-Output "TimeoutSeconds=$TimeoutSeconds"
    Write-Output 'Privacy=sender-and-unexpected-body-redacted'

    # Catch a test message that may have arrived just before the probe opened.
    if ($baselineCount -gt 0) {
        $unread = Invoke-At -Port $serial -Command 'AT+CMGL="REC UNREAD"' -TimeoutMilliseconds 10000
        if (Test-ResponseContainsExpectedBody -Response $unread -Expected $ExpectedBody) {
            $indexMatch = [regex]::Match($unread, '(?m)^\+CMGL:\s*(\d+),')
            $storedIndex = if ($indexMatch.Success) { [int]$indexMatch.Groups[1].Value } else { -1 }
            Write-SafeResult -DeliveryMode 'PREEXISTING_UNREAD' -Storage 'ME' -Index $storedIndex -Body $ExpectedBody
            exit 0
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $buffer = [Text.StringBuilder]::new()
    $nextStoragePoll = [DateTime]::UtcNow.AddSeconds(5)
    $unmatchedCount = 0

    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
        $chunk = $serial.ReadExisting()
        if ($chunk.Length -gt 0) {
            [void]$buffer.Append($chunk)
        }

        $text = $buffer.ToString()
        $cmti = [regex]::Match($text, '\+CMTI:\s*"(?<storage>[^"]+)",\s*(?<index>\d+)')
        if ($cmti.Success) {
            $storage = $cmti.Groups['storage'].Value
            $messageIndex = [int]$cmti.Groups['index'].Value
            $cmgr = Invoke-At -Port $serial -Command ("AT+CMGR=$messageIndex") -TimeoutMilliseconds 10000
            $body = Get-TextBodyFromCmgr -Response $cmgr
            if ($body -ceq $ExpectedBody) {
                Write-SafeResult -DeliveryMode 'CMTI' -Storage $storage -Index $messageIndex -Body $body
                exit 0
            }

            $unmatchedCount++
            Write-Output "UnmatchedSmsObserved=$unmatchedCount"
            Write-Output "UnmatchedSms[$unmatchedCount].Storage=$storage"
            Write-Output "UnmatchedSms[$unmatchedCount].Index=$messageIndex"
            Write-Output "UnmatchedSms[$unmatchedCount].Body=<redacted-unexpected-message>"
            $currentCpms = Invoke-At -Port $serial -Command 'AT+CPMS?'
            $baselineCount = Get-StoredCount -CpmsResponse $currentCpms
            [void]$buffer.Clear()
            $nextStoragePoll = [DateTime]::UtcNow.AddSeconds(5)
            continue
        }

        if ([DateTime]::UtcNow -ge $nextStoragePoll) {
            $cpms = Invoke-At -Port $serial -Command 'AT+CPMS?'
            $currentCount = Get-StoredCount -CpmsResponse $cpms
            if ($currentCount -gt $baselineCount) {
                $unread = Invoke-At -Port $serial -Command 'AT+CMGL="REC UNREAD"' -TimeoutMilliseconds 10000
                if (Test-ResponseContainsExpectedBody -Response $unread -Expected $ExpectedBody) {
                    $indexMatch = [regex]::Match($unread, '(?m)^\+CMGL:\s*(\d+),')
                    $storedIndex = if ($indexMatch.Success) { [int]$indexMatch.Groups[1].Value } else { -1 }
                    Write-SafeResult -DeliveryMode 'STORAGE_POLL' -Storage 'ME' -Index $storedIndex -Body $ExpectedBody
                    exit 0
                }
            }
            $nextStoragePoll = [DateTime]::UtcNow.AddSeconds(5)
            [void]$buffer.Clear()
        }
    }

    $finalCpms = Invoke-At -Port $serial -Command 'AT+CPMS?'
    $finalCount = Get-StoredCount -CpmsResponse $finalCpms
    Write-Output 'SmsReceived=False'
    Write-Output "FinalStoredCount=$finalCount"
    Write-Output 'Result=TIMEOUT'
    exit 2
}
finally {
    if ($serial.IsOpen) {
        $serial.Close()
    }
    $serial.Dispose()
}
