@echo off
rem FieldKit Windows setup launcher - just double-click this file.
rem All logic lives in setup-windows.ps1; -File makes exit codes and quoting sane.
cd /d "%~dp0"
if not exist "%~dp0setup-windows.ps1" (
  echo Please right-click the ZIP, choose "Extract All...", and run setup
  echo from inside the extracted folder - not from the zip preview window.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1"
echo.
echo Setup launcher finished with exit code %ERRORLEVEL%.
echo If a second (Administrator) window opened, setup continued THERE.
pause
