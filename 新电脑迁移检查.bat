@echo off
setlocal
cd /d "%~dp0"

where pwsh >nul 2>&1
if errorlevel 1 (
    echo ERROR: PowerShell 7 ^(pwsh^) not found in PATH.
    pause
    exit /b 2
)

pwsh -NoProfile -File "%~dp0tools\Test-NewPcReadiness.ps1"
set "checkExit=%ERRORLEVEL%"
echo.
if not "%checkExit%"=="0" (
    echo Migration readiness check found blocking failures.
) else (
    echo Migration readiness check completed without blocking failures.
)
pause
endlocal & exit /b %checkExit%
