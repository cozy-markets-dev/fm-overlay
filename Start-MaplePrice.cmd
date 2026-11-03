@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start_mapleprice.ps1"
if errorlevel 1 (
  pause
  exit /b 1
)
exit /b 0
