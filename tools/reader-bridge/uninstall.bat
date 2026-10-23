@echo off
rem Remove the reader bridge service. Double-click it, or run it from a command prompt.
setlocal
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
for /f "tokens=2" %%S in ('sc query type^= service state^= all ^| findstr /b /c:"SERVICE_NAME: ReaderBridge"') do (
    echo Removing %%S
    net stop %%S >nul 2>&1
    sc delete %%S
)
pause
