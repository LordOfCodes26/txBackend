@echo off
rem Install (or update) the reader bridge as one Windows service for all card readers.
rem Double-click it, or from a command prompt:
rem
rem   install.bat 192.168.100.10                      server port 9100, 9600 baud
rem   install.bat 192.168.100.10 9100 115200
rem   install.bat 192.168.100.10 9100 9600 "--exclude COM7"   never open COM7 (a printer)
rem
rem The bridge finds the readers by itself on any USB COM port.
setlocal
set SERVER=%~1
set TCPPORT=%~2
set BAUD=%~3
set EXTRA=%~4
if "%TCPPORT%"=="" set TCPPORT=9100
if "%BAUD%"=="" set BAUD=9600

rem Not administrator: start again as administrator (Windows asks "Yes / No").
net session >nul 2>&1
if errorlevel 1 (
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)

rem The kit's server.txt (set when the kit was built) holds the default address.
set DEFAULT=
if exist "%~dp0server.txt" set /p DEFAULT=<"%~dp0server.txt"
if "%SERVER%"=="" (
    echo Card reader bridge
    echo.
    if defined DEFAULT echo Press Enter to use the server %DEFAULT%, or type another IP address.
    set /p SERVER=Server IP address:
)
if "%SERVER%"=="" set SERVER=%DEFAULT%
if "%SERVER%"=="" (
    echo No server IP address given.
    goto fail
)

set DIR=C:\ReaderBridge
set SVC=ReaderBridge
set NSSM=%DIR%\nssm.exe

rem The readers' USB chip (CH341): Windows gets its driver from the internet, which this
rem PC may not have. Installing it again over the same version does no harm.
rem pnputil: 0 done, 259 added but no reader plugged in yet, 3010 done after a restart.
if not exist "%~dp0driver\CH341SER.INF" goto driverdone
echo Installing the card reader USB driver (CH341)
pnputil /add-driver "%~dp0driver\CH341SER.INF" /install >"%TEMP%\ch341-driver.txt" 2>&1
set DRVRESULT=%errorlevel%
if "%DRVRESULT%"=="3010" echo Restart the PC after this, then the readers work.
if "%DRVRESULT%"=="0" goto driverdone
if "%DRVRESULT%"=="259" goto driverdone
if "%DRVRESULT%"=="3010" goto driverdone
type "%TEMP%\ch341-driver.txt"
echo The USB driver could not be installed (code %DRVRESULT%): the readers may not work.
:driverdone

rem Remove the old service(s): this one, and the one-per-COM-port services of older kits.
for /f "tokens=2" %%S in ('sc query type^= service state^= all ^| findstr /b /c:"SERVICE_NAME: ReaderBridge"') do (
    echo Removing the old %%S service
    net stop %%S >nul 2>&1
    sc delete %%S >nul 2>&1
)
rem Windows needs a moment to let go of a deleted service.
timeout /t 2 /nobreak >nul

if not exist "%DIR%" mkdir "%DIR%"
copy /y "%~dp0ReaderBridge.exe" "%DIR%\" >nul || goto copyfail
copy /y "%~dp0nssm.exe" "%DIR%\" >nul || goto copyfail
copy /y "%~dp0uninstall.bat" "%DIR%\" >nul

echo Installing the reader bridge: all USB COM ports at %BAUD% baud to %SERVER%:%TCPPORT%
"%NSSM%" install %SVC% "%DIR%\ReaderBridge.exe" --server %SERVER%:%TCPPORT% --baud %BAUD% %EXTRA% || goto fail
"%NSSM%" set %SVC% DisplayName "Card reader bridge" >nul
"%NSSM%" set %SVC% Description "Card readers on USB COM ports to %SERVER%:%TCPPORT%" >nul
"%NSSM%" set %SVC% AppDirectory "%DIR%" >nul
"%NSSM%" set %SVC% Start SERVICE_AUTO_START >nul
"%NSSM%" set %SVC% AppNoConsole 1 >nul
"%NSSM%" set %SVC% AppStdout "%DIR%\bridge.log" >nul
"%NSSM%" set %SVC% AppStderr "%DIR%\bridge.log" >nul
"%NSSM%" set %SVC% AppRotateFiles 1 >nul
"%NSSM%" set %SVC% AppRotateOnline 1 >nul
"%NSSM%" set %SVC% AppRotateBytes 5000000 >nul
"%NSSM%" set %SVC% AppExit Default Restart >nul
"%NSSM%" set %SVC% AppRestartDelay 3000 >nul
"%NSSM%" start %SVC% >nul
"%NSSM%" status %SVC% | findstr /c:"SERVICE_RUNNING" >nul || goto fail
echo.
echo Done. The card readers work on any USB socket; nothing else to set up.
echo Log: %DIR%\bridge.log
echo.
pause
exit /b 0

:copyfail
echo Could not copy the files to %DIR%. Is ReaderBridge.exe still open in a window?
goto fail

:fail
echo.
echo Installation FAILED. Send a photo of this window to the administrator.
pause
exit /b 1
