[CmdletBinding()]
param(
    [ValidatePattern('^COM\d+$')]
    [string]$PortName,

    [string]$ExpectedBody = 'DJI-TEST-0807'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'DjiDeviceDiscovery.ps1')
$PortName = Resolve-DjiAtPortName -PortName $PortName

function New-InspectionPort {
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
        [int]$TimeoutMilliseconds = 6000,

        [switch]$AllowCmsError
    )

    $Port.DiscardInBuffer()
    $Port.DiscardOutBuffer()
    $Port.Write($Command + "`r")
    $response = Read-AtResponse -Port $Port -TimeoutMilliseconds $TimeoutMilliseconds

    if ($response -match '(?m)^\s*<TIMEOUT>\s*$') {
        throw "AT command timed out: $Command"
    }
    if ($response -match '(?m)^\s*(ERROR|\+CME ERROR:.*|\+CMS ERROR:.*)\s*$') {
        if ($AllowCmsError) {
            return $null
        }
        throw "AT command failed: $Command (response redacted)"
    }
    if ($response -notmatch '(?m)^\s*OK\s*$') {
        throw "AT command was not acknowledged: $Command (response redacted)"
    }
    return $response
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

function Get-CpmsState {
    param(
        [Parameter(Mandatory)]
        [string]$Response
    )

    $match = [regex]::Match($Response, '\+CPMS:\s*"[^"]+",(?<used>\d+),(?<total>\d+)')
    if (-not $match.Success) {
        throw 'Unable to parse SMS storage metadata (response redacted).'
    }
    return [pscustomobject]@{
        Used = [int]$match.Groups['used'].Value
        Total = [int]$match.Groups['total'].Value
    }
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

function Get-DecodingAssessment {
    param(
        [Parameter(Mandatory)]
        [string]$Body,

        [Parameter(Mandatory)]
        [string]$Expected
    )

    $trimmed = $Body.Trim()
    $compact = -join ($trimmed -split '\s+')
    $isHex = $compact.Length -gt 0 -and
        $compact.Length % 2 -eq 0 -and
        $compact -match '^[0-9A-Fa-f]+$'
    $exactCandidates = [Collections.Generic.List[string]]::new()
    $containsCandidates = [Collections.Generic.List[string]]::new()

    $candidateMap = [ordered]@{
        Raw = $Body
        Trimmed = $trimmed
    }

    if ($isHex) {
        $bytes = Convert-HexToBytes -Hex $compact
        $candidateMap['HexAscii'] = [Text.Encoding]::ASCII.GetString($bytes).Trim([char]0).Trim()
        $candidateMap['HexUtf8'] = [Text.Encoding]::UTF8.GetString($bytes).Trim([char]0).Trim()
        if ($bytes.Length % 2 -eq 0) {
            $candidateMap['HexUtf16Be'] = [Text.Encoding]::BigEndianUnicode.GetString($bytes).Trim([char]0).Trim()
            $candidateMap['HexUtf16Le'] = [Text.Encoding]::Unicode.GetString($bytes).Trim([char]0).Trim()
        }
    }

    foreach ($entry in $candidateMap.GetEnumerator()) {
        if ($entry.Value -ceq $Expected) {
            [void]$exactCandidates.Add($entry.Key)
        }
        if ($entry.Value.Contains($Expected, [StringComparison]::Ordinal)) {
            [void]$containsCandidates.Add($entry.Key)
        }
    }

    if ($isHex) {
        $expectedAsciiHex = [Convert]::ToHexString([Text.Encoding]::ASCII.GetBytes($Expected))
        $expectedUcs2Hex = [Convert]::ToHexString([Text.Encoding]::BigEndianUnicode.GetBytes($Expected))
        if ($compact.Contains($expectedAsciiHex, [StringComparison]::OrdinalIgnoreCase)) {
            [void]$containsCandidates.Add('HexContainsAsciiBytes')
        }
        if ($compact.Contains($expectedUcs2Hex, [StringComparison]::OrdinalIgnoreCase)) {
            [void]$containsCandidates.Add('HexContainsUtf16BeBytes')
        }
    }

    return [pscustomobject]@{
        TrimmedLength = $trimmed.Length
        IsHex = $isHex
        HexByteLength = if ($isHex) { $compact.Length / 2 } else { 0 }
        ExactCandidates = @($exactCandidates | Select-Object -Unique)
        ContainsCandidates = @($containsCandidates | Select-Object -Unique)
    }
}

function Read-CmgrMetadata {
    param(
        [Parameter(Mandatory)]
        [string]$Response,

        [Parameter(Mandatory)]
        [int]$Index,

        [Parameter(Mandatory)]
        [string]$Expected
    )

    $lines = @($Response.Replace("`r", '').Split("`n"))
    $headerIndex = -1
    for ($position = 0; $position -lt $lines.Count; $position++) {
        if ($lines[$position] -match '^\s*\+CMGR:\s*(?<fields>.*)$') {
            $headerIndex = $position
            $fieldText = $Matches['fields']
            break
        }
    }
    if ($headerIndex -lt 0) {
        throw "CMGR metadata is missing for index $Index (response redacted)."
    }

    $fields = @(Split-AtCsvFields -Value $fieldText)
    $status = if ($fields.Count -gt 0) { $fields[0] } else { '' }
    $dcs = $null
    if ($fields.Count -gt 7) {
        $parsedDcs = 0
        if ([int]::TryParse($fields[7], [ref]$parsedDcs)) {
            $dcs = $parsedDcs
        }
    }

    $lastContentIndex = $lines.Count - 1
    while ($lastContentIndex -gt $headerIndex -and [string]::IsNullOrWhiteSpace($lines[$lastContentIndex])) {
        $lastContentIndex--
    }
    if ($lastContentIndex -gt $headerIndex -and $lines[$lastContentIndex].Trim() -eq 'OK') {
        $lastContentIndex--
    }

    $bodyLines = [Collections.Generic.List[string]]::new()
    for ($position = $headerIndex + 1; $position -le $lastContentIndex; $position++) {
        $line = $lines[$position]
        if ($line -match '^\s*\+CMTI:\s*"[^"]+",\s*\d+\s*$') {
            continue
        }
        [void]$bodyLines.Add($line)
    }
    $body = ($bodyLines -join "`n").Trim("`r", "`n")
    $assessment = Get-DecodingAssessment -Body $body -Expected $Expected

    return [pscustomobject]@{
        Index = $Index
        Status = $status
        HeaderFieldCount = $fields.Count
        Dcs = $dcs
        BodyLineCount = $bodyLines.Count
        RawBodyLength = $body.Length
        TrimmedBodyLength = $assessment.TrimmedLength
        RawBodyIsHex = $assessment.IsHex
        HexByteLength = $assessment.HexByteLength
        ExactCandidates = $assessment.ExactCandidates
        ContainsCandidates = $assessment.ContainsCandidates
    }
}

if ($PortName -notin [System.IO.Ports.SerialPort]::GetPortNames()) {
    throw "Serial port is not present: $PortName"
}

$serial = New-InspectionPort
$foundExpected = $false
$messagesFound = 0
try {
    $serial.Open()
    [void](Invoke-At -Port $serial -Command 'AT')
    [void](Invoke-At -Port $serial -Command 'ATE0')
    [void](Invoke-At -Port $serial -Command 'AT+CMGF=1')
    [void](Invoke-At -Port $serial -Command 'AT+CSDH=1')
    [void](Invoke-At -Port $serial -Command 'AT+CPMS="ME","ME","ME"')

    $ims = Invoke-At -Port $serial -Command 'AT+QCFG="ims"'
    $imsReady = $ims -match '\+QCFG:\s*"ims",1,1'
    $registration = Invoke-At -Port $serial -Command 'AT+CEREG?'
    $epsReady = $registration -match '\+CEREG:\s*\d+,(1|5)(?:\D|$)'
    $cpms = Get-CpmsState -Response (Invoke-At -Port $serial -Command 'AT+CPMS?')

    Write-Output 'InspectionReady=True'
    Write-Output "Port=$PortName"
    Write-Output "ImsReady=$imsReady"
    Write-Output "EpsReady=$epsReady"
    Write-Output 'Storage=ME'
    Write-Output "StoredCount=$($cpms.Used)"
    Write-Output "StorageCapacity=$($cpms.Total)"
    Write-Output 'Privacy=sender-and-message-content-never-emitted'

    for ($messageIndex = 0; $messageIndex -lt $cpms.Total; $messageIndex++) {
        $response = Invoke-At -Port $serial -Command "AT+CMGR=$messageIndex" -TimeoutMilliseconds 10000 -AllowCmsError
        if ($null -eq $response) {
            continue
        }
        if ($response -notmatch '(?m)^\s*\+CMGR:') {
            # QDC507 acknowledges an empty ME slot with a bare OK instead of CMS ERROR.
            continue
        }

        $metadata = Read-CmgrMetadata -Response $response -Index $messageIndex -Expected $ExpectedBody
        $messagesFound++
        $exact = $metadata.ExactCandidates.Count -gt 0
        $contains = $metadata.ContainsCandidates.Count -gt 0
        if ($exact) {
            $foundExpected = $true
        }

        Write-Output "Message[$messageIndex].Status=$($metadata.Status)"
        Write-Output "Message[$messageIndex].HeaderFieldCount=$($metadata.HeaderFieldCount)"
        Write-Output "Message[$messageIndex].Dcs=$($metadata.Dcs)"
        Write-Output "Message[$messageIndex].BodyLineCount=$($metadata.BodyLineCount)"
        Write-Output "Message[$messageIndex].RawBodyLength=$($metadata.RawBodyLength)"
        Write-Output "Message[$messageIndex].TrimmedBodyLength=$($metadata.TrimmedBodyLength)"
        Write-Output "Message[$messageIndex].RawBodyIsHex=$($metadata.RawBodyIsHex)"
        if ($metadata.RawBodyIsHex) {
            Write-Output "Message[$messageIndex].HexByteLength=$($metadata.HexByteLength)"
        }
        Write-Output "Message[$messageIndex].ExpectedBodyExact=$exact"
        Write-Output "Message[$messageIndex].ExpectedBodyContained=$contains"
        if ($exact) {
            Write-Output "Message[$messageIndex].MatchingDecoder=$($metadata.ExactCandidates -join ',')"
            Write-Output "Message[$messageIndex].Body=$ExpectedBody"
        }
        elseif ($contains) {
            Write-Output "Message[$messageIndex].ContainingDecoder=$($metadata.ContainsCandidates -join ',')"
            Write-Output "Message[$messageIndex].Body=<redacted-unexpected-wrapper>"
        }
        else {
            Write-Output "Message[$messageIndex].Body=<redacted-unexpected-message>"
        }
    }

    Write-Output "MessagesFound=$messagesFound"
    Write-Output "ExpectedMessageFound=$foundExpected"
    if ($foundExpected) {
        exit 0
    }
    exit 2
}
finally {
    if ($serial.IsOpen) {
        $serial.Close()
    }
    $serial.Dispose()
}
