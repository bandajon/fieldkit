@echo off
rem FieldKit Windows setup launcher - just double-click this file.
rem Clears the downloaded-file block and runs setup-windows.ps1 past the
rem execution policy; the script itself relaunches as Administrator.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -Recurse | Unblock-File -ErrorAction SilentlyContinue; & '.\setup-windows.ps1'"
pause
