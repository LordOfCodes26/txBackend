@echo off
rem Remove the management system from this PC (asks for administrator rights).
rem   uninstall.bat               services, firewall rules, backup task; KEEPS data and backups
rem   uninstall.bat -RemoveData   EVERYTHING in the install folder (asks to confirm)
rem Works from the kit folder and as C:\Management\uninstall.bat. Developers' backend-dev
rem and frontend-dev folders are never touched.
setlocal
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
rem The scripts: next to this file (kit folder), or in the installed backend.
set "SRC=%~dp0"
if not exist "%SRC%uninstall.ps1" set "SRC=%~dp0backend\current\deploy\windows\"
if not exist "%SRC%uninstall.ps1" (
    echo ERROR: uninstall.ps1 not found next to this file or in backend\current\deploy\windows.
    pause
    exit /b 1
)
rem The install folder: this file's folder when it is C:\Management\uninstall.bat.
set "ROOT=C:\Management"
if exist "%~dp0etc\backend.env" set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
rem Run from a temporary copy: -RemoveData deletes the folder this file may be in.
set "TMPDIR=%TEMP%\mgmt-uninstall"
if exist "%TMPDIR%" rmdir /s /q "%TMPDIR%"
mkdir "%TMPDIR%"
copy /y "%SRC%uninstall.ps1" "%TMPDIR%\" >nul
copy /y "%SRC%common.ps1" "%TMPDIR%\" >nul
rem Out of the install folder: Windows can't delete the folder a window is "in".
cd /d "%TMPDIR%"
rem One line on purpose: cmd reads a batch file line by line, and this file may be gone.
powershell -NoProfile -ExecutionPolicy Bypass -File "%TMPDIR%\uninstall.ps1" -Root "%ROOT%" %* & echo. & pause & exit /b
