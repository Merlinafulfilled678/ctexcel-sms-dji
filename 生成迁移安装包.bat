@echo off
setlocal
cd /d "%~dp0"

where pwsh >nul 2>&1
if errorlevel 1 (
    echo ERROR: PowerShell 7 ^(pwsh^) not found in PATH.
    pause
    exit /b 1
)

echo This will build ONE private migration EXE from the current files.
echo The running DJI SMS service will be stopped and will remain stopped.
echo The EXE contains Telegram credentials and SMS history. Do not share it.
choice /c YN /n /m "Continue? [Y/N] "
if errorlevel 2 exit /b 0

pwsh -NoProfile -File "%~dp0tools\Build-MigrationInstaller.ps1" -StopRunningService
set "BUILD_EXIT=%ERRORLEVEL%"
echo.
if not "%BUILD_EXIT%"=="0" echo Build failed. Read the error above.
pause
exit /b %BUILD_EXIT%
