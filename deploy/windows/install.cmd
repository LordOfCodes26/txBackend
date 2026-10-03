@echo off
rem Double-click to install or upgrade (asks for administrator rights). Options for
rem install-all.ps1 can follow, e.g.:  install.cmd -ServerIp 192.168.1.10 -DevUser kim
setlocal
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo Asking for administrator rights...
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-all.ps1" %*
echo.
pause
