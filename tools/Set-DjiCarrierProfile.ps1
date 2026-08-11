[CmdletBinding()]
param(
    [ValidateSet('Status', 'ApplyCt', 'RollbackRow', 'ForceIms', 'RestoreIms')]
    [string]$Mode = 'Status',

    [ValidatePattern('^COM\d+$')]
    [string]$PortName,

    [ValidateRange(1200, 921600)]
    [int]$BaudRate = 115200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'DjiDeviceDiscovery.ps1')
$PortName = Resolve-DjiAtPortName -PortName $PortName

$ctProfile = 'VoLTE_OPNMKT_CT'
$rowProfile = 'ROW_Generic_3GPP'

function New-DjiSerialPort {
    $port = [System.IO.Ports.SerialPort]::new(
        $PortName,
        $BaudRate,
        [System.IO.Ports.Parity]::None,
        8,
        [System.IO.Ports.StopBits]::One)
    $port.Handshake = [System.IO.Ports.Handshake]::None
    $port.DtrEnable = $false
    $port.RtsEnable = $false
    $port.ReadTimeout = 250
    $port.WriteTimeout = 2000
    $port.Encoding = [Text.Encoding]::ASCII
    $port.NewLine = "`r`n"
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
        $quietMilliseconds = ([DateTime]::UtcNow - $lastData).TotalMilliseconds
        if ($hasTerminal -and $quietMilliseconds -ge 200) {
            return $text.Trim()
        }
    }

    return ($builder.ToString() + "`r`n<TIMEOUT>").Trim()
}

function Invoke-AtCommand {
    param(
        [Parameter(Mandatory)]
        [System.IO.Ports.SerialPort]$Port,

        [Parameter(Mandatory)]
        [string]$Command,

        [ValidateRange(500, 30000)]
        [int]$TimeoutMilliseconds = 6000,

        [switch]$AllowDisconnect
    )

    try {
        $Port.DiscardInBuffer()
        $Port.DiscardOutBuffer()
        $Port.Write($Command + "`r")
        $response = Read-AtResponse -Port $Port -TimeoutMilliseconds $TimeoutMilliseconds
    }
    catch [System.IO.IOException] {
        if ($AllowDisconnect) {
            return '<PORT_DISCONNECTED_AFTER_COMMAND>'
        }
        throw
    }
    catch [System.InvalidOperationException] {
        if ($AllowDisconnect) {
            return '<PORT_DISCONNECTED_AFTER_COMMAND>'
        }
        throw
    }

    Write-Host "--- $Command ---"
    Write-Host $response
    return $response
}

function Assert-AtOk {
    param(
        [Parameter(Mandatory)]
        [string]$Command,

        [Parameter(Mandatory)]
        [string]$Response
    )

    if ($Response -match '(?m)^\s*(ERROR|\+CME ERROR:.*|\+CMS ERROR:.*)\s*$' -or
        $Response -notmatch '(?m)^\s*OK\s*$') {
        throw "AT command did not complete with OK: $Command`n$Response"
    }
}

function Open-ReadyPort {
    param(
        [ValidateRange(1, 180)]
        [int]$TimeoutSeconds = 120
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastError = $null

    while ([DateTime]::UtcNow -lt $deadline) {
        if ($PortName -in [System.IO.Ports.SerialPort]::GetPortNames()) {
            $candidate = New-DjiSerialPort
            try {
                $candidate.Open()
                $response = Invoke-AtCommand -Port $candidate -Command 'AT' -TimeoutMilliseconds 2000
                if ($response -match '(?m)^\s*OK\s*$') {
                    return $candidate
                }
            }
            catch {
                $lastError = $_.Exception.Message
            }

            if ($candidate.IsOpen) {
                $candidate.Close()
            }
            $candidate.Dispose()
        }

        Start-Sleep -Seconds 1
    }

    throw "Port $PortName did not become AT-ready within $TimeoutSeconds seconds. Last error: $lastError"
}

function Get-ProfileState {
    param(
        [Parameter(Mandatory)]
        [System.IO.Ports.SerialPort]$Port
    )

    $auto = Invoke-AtCommand -Port $Port -Command 'AT+QMBNCFG="AutoSel"'
    Assert-AtOk -Command 'AT+QMBNCFG="AutoSel"' -Response $auto

    $list = Invoke-AtCommand -Port $Port -Command 'AT+QMBNCFG="List"' -TimeoutMilliseconds 10000
    Assert-AtOk -Command 'AT+QMBNCFG="List"' -Response $list

    $ims = Invoke-AtCommand -Port $Port -Command 'AT+QCFG="ims"'
    Assert-AtOk -Command 'AT+QCFG="ims"' -Response $ims

    [pscustomobject]@{
        Auto = $auto
        List = $list
        Ims = $ims
    }
}

function Wait-NetworkRegistration {
    param(
        [Parameter(Mandatory)]
        [System.IO.Ports.SerialPort]$Port,

        [ValidateRange(1, 180)]
        [int]$TimeoutSeconds = 120
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastResponse = $null

    while ([DateTime]::UtcNow -lt $deadline) {
        $lastResponse = Invoke-AtCommand -Port $Port -Command 'AT+CEREG?'
        if ($lastResponse -match '\+CEREG:\s*\d+,(1|5)(?:\D|$)') {
            return $lastResponse
        }
        Start-Sleep -Seconds 2
    }

    throw "EPS registration did not reach stat 1 or 5 within $TimeoutSeconds seconds. Last response: $lastResponse"
}

$serial = $null
try {
    $serial = Open-ReadyPort -TimeoutSeconds 10
    Write-Output "Port=$PortName"
    Write-Output "Mode=$Mode"

    $before = Get-ProfileState -Port $serial
    if ($before.List -notmatch [regex]::Escape('"' + $ctProfile + '"')) {
        throw "Required CT MBN profile is absent: $ctProfile"
    }

    if ($Mode -eq 'Status') {
        Write-Output 'Result=STATUS_OK'
        exit 0
    }

    if ($Mode -in @('ForceIms', 'RestoreIms')) {
        $activeRowPattern = '"List",\d+,1,1,"' + [regex]::Escape($rowProfile) + '"'
        if ($before.List -notmatch $activeRowPattern) {
            throw "IMS mode changes require the active ROW MBN profile: $rowProfile"
        }
        if ($before.Auto -notmatch '\+QMBNCFG:\s*"AutoSel",1') {
            throw 'IMS mode changes require MBN AutoSel=1.'
        }

        $imsValue = if ($Mode -eq 'ForceIms') { 1 } else { 0 }
        $imsCommand = 'AT+QCFG="ims",' + $imsValue
        $imsResponse = Invoke-AtCommand -Port $serial -Command $imsCommand
        Assert-AtOk -Command $imsCommand -Response $imsResponse

        $pendingIms = Invoke-AtCommand -Port $serial -Command 'AT+QCFG="ims"'
        Assert-AtOk -Command 'AT+QCFG="ims"' -Response $pendingIms
        if ($pendingIms -notmatch ('\+QCFG:\s*"ims",' + $imsValue + ',[01]')) {
            throw "IMS configuration was not accepted before reboot: expected $imsValue."
        }

        $rebootResponse = Invoke-AtCommand -Port $serial -Command 'AT+CFUN=1,1' -TimeoutMilliseconds 5000 -AllowDisconnect
        if ($rebootResponse -ne '<PORT_DISCONNECTED_AFTER_COMMAND>') {
            Assert-AtOk -Command 'AT+CFUN=1,1' -Response $rebootResponse
        }

        if ($serial.IsOpen) {
            $serial.Close()
        }
        $serial.Dispose()
        $serial = $null

        Start-Sleep -Seconds 3
        $serial = Open-ReadyPort -TimeoutSeconds 120

        $cpinDeadline = [DateTime]::UtcNow.AddSeconds(60)
        do {
            $cpin = Invoke-AtCommand -Port $serial -Command 'AT+CPIN?'
            if ($cpin -match '\+CPIN:\s*READY') {
                break
            }
            Start-Sleep -Seconds 1
        } while ([DateTime]::UtcNow -lt $cpinDeadline)
        if ($cpin -notmatch '\+CPIN:\s*READY') {
            throw "SIM did not become ready after IMS reboot. Last response: $cpin"
        }

        $afterImsChange = Get-ProfileState -Port $serial
        if ($afterImsChange.List -notmatch $activeRowPattern) {
            throw "ROW MBN was not preserved after IMS reboot: $rowProfile"
        }
        if ($afterImsChange.Auto -notmatch '\+QMBNCFG:\s*"AutoSel",1') {
            throw 'MBN AutoSel was not preserved as 1 after IMS reboot.'
        }
        if ($afterImsChange.Ims -notmatch ('\+QCFG:\s*"ims",' + $imsValue + ',[01]')) {
            throw "IMS configuration did not persist after reboot: expected $imsValue."
        }

        [void](Wait-NetworkRegistration -Port $serial -TimeoutSeconds 120)

        $finalIms = $afterImsChange.Ims
        if ($Mode -eq 'ForceIms') {
            $imsDeadline = [DateTime]::UtcNow.AddSeconds(90)
            while ($finalIms -notmatch '\+QCFG:\s*"ims",1,1' -and [DateTime]::UtcNow -lt $imsDeadline) {
                Start-Sleep -Seconds 3
                $finalIms = Invoke-AtCommand -Port $serial -Command 'AT+QCFG="ims"'
                Assert-AtOk -Command 'AT+QCFG="ims"' -Response $finalIms
            }
        }

        [void](Invoke-AtCommand -Port $serial -Command 'AT+CEREG?')
        [void](Invoke-AtCommand -Port $serial -Command 'AT+CGREG?')
        [void](Invoke-AtCommand -Port $serial -Command 'AT+CREG?')
        [void](Invoke-AtCommand -Port $serial -Command 'AT+COPS?')
        [void](Invoke-AtCommand -Port $serial -Command 'AT+CSQ')
        [void](Invoke-AtCommand -Port $serial -Command 'AT+QCFG="ltesms/format"')

        Write-Output "ActiveProfile=$rowProfile"
        Write-Output 'AutoSelect=1'
        Write-Output "ImsConfig=$imsValue"
        if ($finalIms -match '\+QCFG:\s*"ims",1,1') {
            Write-Output 'ImsCapability=1'
            Write-Output 'Result=IMS_REGISTERED'
        }
        elseif ($Mode -eq 'ForceIms') {
            Write-Output 'ImsCapability=0'
            Write-Output 'Result=IMS_ENABLED_NOT_REGISTERED'
        }
        else {
            Write-Output 'Result=IMS_RESTORE_OK'
        }
        exit 0
    }

    if ($Mode -eq 'ApplyCt') {
        $targetProfile = $ctProfile
        $autoValue = 0
    }
    else {
        $targetProfile = $rowProfile
        $autoValue = 1
    }

    $autoCommand = 'AT+QMBNCFG="AutoSel",' + $autoValue
    $autoResponse = Invoke-AtCommand -Port $serial -Command $autoCommand
    Assert-AtOk -Command $autoCommand -Response $autoResponse

    $selectCommand = 'AT+QMBNCFG="Select","' + $targetProfile + '"'
    $selectResponse = Invoke-AtCommand -Port $serial -Command $selectCommand
    Assert-AtOk -Command $selectCommand -Response $selectResponse

    $pending = Invoke-AtCommand -Port $serial -Command 'AT+QMBNCFG="List"' -TimeoutMilliseconds 10000
    Assert-AtOk -Command 'AT+QMBNCFG="List"' -Response $pending
    $selectedPattern = '"List",\d+,1,[01],"' + [regex]::Escape($targetProfile) + '"'
    if ($pending -notmatch $selectedPattern) {
        throw "Target MBN was not marked selected before reboot: $targetProfile"
    }

    $rebootResponse = Invoke-AtCommand -Port $serial -Command 'AT+CFUN=1,1' -TimeoutMilliseconds 5000 -AllowDisconnect
    if ($rebootResponse -ne '<PORT_DISCONNECTED_AFTER_COMMAND>') {
        Assert-AtOk -Command 'AT+CFUN=1,1' -Response $rebootResponse
    }

    if ($serial.IsOpen) {
        $serial.Close()
    }
    $serial.Dispose()
    $serial = $null

    Start-Sleep -Seconds 3
    $serial = Open-ReadyPort -TimeoutSeconds 120

    $cpinDeadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        $cpin = Invoke-AtCommand -Port $serial -Command 'AT+CPIN?'
        if ($cpin -match '\+CPIN:\s*READY') {
            break
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $cpinDeadline)
    if ($cpin -notmatch '\+CPIN:\s*READY') {
        throw "SIM did not become ready after module reboot. Last response: $cpin"
    }

    $after = Get-ProfileState -Port $serial
    $activatedPattern = '"List",\d+,1,1,"' + [regex]::Escape($targetProfile) + '"'
    if ($after.List -notmatch $activatedPattern) {
        throw "Target MBN is not selected and activated after reboot: $targetProfile"
    }

    [void](Wait-NetworkRegistration -Port $serial -TimeoutSeconds 120)
    [void](Invoke-AtCommand -Port $serial -Command 'AT+CGREG?')
    [void](Invoke-AtCommand -Port $serial -Command 'AT+CREG?')
    [void](Invoke-AtCommand -Port $serial -Command 'AT+COPS?')
    [void](Invoke-AtCommand -Port $serial -Command 'AT+CSQ')
    [void](Invoke-AtCommand -Port $serial -Command 'AT+QCFG="ltesms/format"')

    Write-Output "ActiveProfile=$targetProfile"
    Write-Output "AutoSelect=$autoValue"
    Write-Output 'Result=PROFILE_SWITCH_OK'
}
finally {
    if ($null -ne $serial) {
        if ($serial.IsOpen) {
            $serial.Close()
        }
        $serial.Dispose()
    }
}
