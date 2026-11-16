@echo off
rem ============================================================
rem AD9910 Control Service - double-click launcher (CMD wrapper)
rem
rem 1) Calls start_web.ps1 with a QUOTED -File path, so spaces in
rem    the project path ("2022-02-21 Controller1.1") are safe.
rem    Without quotes, PowerShell splits the path and the window
rem    closes instantly ("flash") with CommandNotFoundException.
rem 2) Uses -ExecutionPolicy Bypass so the .ps1 is allowed to run.
rem 3) Keeps the window open after exit so errors stay visible.
rem
rem Just double-click this file.
rem ============================================================
chcp 65001 >nul
setlocal
pushd "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_web.ps1"
set "AD9910_EXIT=%ERRORLEVEL%"

echo.
echo [AD9910 control service exited, code %AD9910_EXIT%] Press any key to close...
pause >nul

popd
endlocal
