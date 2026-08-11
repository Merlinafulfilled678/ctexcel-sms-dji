@echo off
setlocal
chcp 65001 >nul
set "PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not exist "%PWSH%" (
  echo [FAIL] 未找到 PowerShell 7。请在终端运行安装目录 tools 下的 Uninstall-CtExcelSmsDji.ps1。
  pause
  exit /b 1
)
"%PWSH%" -NoProfile -File "%~dp0tools\Uninstall-CtExcelSmsDji.ps1"
if errorlevel 1 (
  echo.
  echo 卸载没有完成，请查看上方原因。
  pause
)
endlocal
