@echo off
setlocal
cd /d "%~dp0"

where pwsh >nul 2>&1
if errorlevel 1 (
    echo ERROR: PowerShell 7 ^(pwsh^) not found in PATH.
    pause
    exit /b 1
)

rem Probe port 7597. Exit 0 = the DJI tool is running; 1 = port free; 2 = occupied by another program.
pwsh -NoProfile -Command "try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:7597/api/status' -TimeoutSec 2; if (($r.carrier.key -eq 'ctexcel') -and ($r.modem.key -eq 'dji_qdc507')) { exit 0 } else { exit 2 } } catch { if (Test-NetConnection 127.0.0.1 -Port 7597 -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 2 } else { exit 1 } }" >nul 2>&1

if "%ERRORLEVEL%"=="0" goto open
if "%ERRORLEVEL%"=="2" goto conflict

set "RUNTIME_PYTHONW="
if exist "%~dp0runtime-pythonw.txt" set /p RUNTIME_PYTHONW=<"%~dp0runtime-pythonw.txt"
if defined RUNTIME_PYTHONW if exist "%RUNTIME_PYTHONW%" (
    start "" /b "%RUNTIME_PYTHONW%" "%~dp0app.py"
    goto wait
)

where pyw >nul 2>&1
if not errorlevel 1 (
    start "" /b pyw -3.14 "%~dp0app.py"
    goto wait
)

where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" /b pythonw "%~dp0app.py"
    goto wait
)

echo ERROR: pythonw not found in PATH. Install Python first.
pause
exit /b 1

:wait
timeout /t 3 /nobreak >nul

:open
start "" "http://127.0.0.1:7597/"
endlocal
exit /b 0

:conflict
echo ERROR: port 7597 is occupied by another program (not this SMS tool).
echo Find it with:  Get-NetTCPConnection -LocalPort 7597 -State Listen
echo Stop the old service before switching this DJI tool to production.
pause
exit /b 2
