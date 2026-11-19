@echo off
rem Install (or update) the reader bridge as one Windows service for all card readers.
rem Double-click it, or from a command prompt:
rem
rem   install.bat 192.168.100.10                      server port 9100, 115200 baud
rem   install.bat 192.168.100.10 9100 9600
rem   install.bat 192.168.100.10 9100 115200 "--exclude COM7"   never open COM7 (a printer)
rem
rem Double-clicked, it asks for the server IP and TCP port (Enter: 9100). The bridge finds
rem the readers by itself on any USB socket.
setlocal
set SERVER=%~1
set TCPPORT=%~2
set BAUD=%~3
set EXTRA=%~4
if "%BAUD%"=="" set BAUD=115200

rem Double-clicked inside the .zip, Windows runs this file alone, without the programs.
if not exist "%~dp0ReaderBridge.exe" (
    echo The kit is still zipped. Right-click the .zip file, choose "Extract All...",
    echo open the extracted folder and double-click install.bat there.
    echo.
    pause
    exit /b 1
)

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

rem The kit's server.txt (set when the kit was built) holds the default address, as IP or
rem IP:PORT. The server's TCP port for the readers is 9100 unless it was changed there
rem (sudo mgmt change-door-port).
set DEFAULT=
set DEFAULTPORT=9100
if exist "%~dp0server.txt" set /p DEFAULT=<"%~dp0server.txt"
for /f "tokens=1,2 delims=:" %%A in ("%DEFAULT%") do (
    set DEFAULT=%%A
    if not "%%B"=="" set DEFAULTPORT=%%B
)

if not "%SERVER%"=="" goto haveserver
echo Card reader bridge
echo.
if defined DEFAULT echo Press Enter to use the server %DEFAULT%, or type another IP address.
set /p SERVER=Server IP address:
if "%SERVER%"=="" set SERVER=%DEFAULT%
if "%SERVER%"=="" (
    set REASON=No server IP address given.
    goto fail
)
rem "192.168.1.10:9200" typed at once: the port is given too.
for /f "tokens=1,2 delims=:" %%A in ("%SERVER%") do (
    set SERVER=%%A
    if not "%%B"=="" set TCPPORT=%%B
)
if not "%TCPPORT%"=="" goto haveserver
echo.
echo Press Enter to use TCP port %DEFAULTPORT% (the usual one), or type the server's port.
set /p TCPPORT=Server TCP port for the readers:
:haveserver
if "%TCPPORT%"=="" set TCPPORT=%DEFAULTPORT%
rem A number stays the same through set /a; anything else turns into 0 or another number.
set /a PORTNUM=%TCPPORT% >nul 2>&1
if not "%PORTNUM%"=="%TCPPORT%" (
    set REASON=Not a port number: %TCPPORT%
    goto fail
)

echo.
echo Checking the connection to %SERVER%:%TCPPORT% ...
set REACHED=1
"%~dp0ReaderBridge.exe" --server %SERVER%:%TCPPORT% --check
if errorlevel 1 set REACHED=0
if "%REACHED%"=="0" (
    echo.
    echo The server does not answer. Installing anyway: the readers start working as
    echo soon as it does. Check the IP address and port, that the server is on, and that
    echo its firewall lets this PC in on port %TCPPORT%.
    echo.
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
rem A ReaderBridge.exe started by hand in a window would block the copy.
taskkill /im ReaderBridge.exe /f >nul 2>&1

rem Windows deletes a service only when nothing holds it (the Services window does): wait.
set TRIES=0
:waitgone
sc query %SVC% >nul 2>&1
if errorlevel 1 goto gone
set /a TRIES+=1
if %TRIES% geq 15 (
    set REASON=The old service is still being removed. Close the "Services" window ^(and Task Manager^), then double-click install.bat again.
    goto fail
)
timeout /t 1 /nobreak >nul
goto waitgone
:gone

if not exist "%DIR%" mkdir "%DIR%"
set REASON=Could not copy the programs to %DIR%. Restart the PC and double-click install.bat again.
copy /y "%~dp0ReaderBridge.exe" "%DIR%\" >nul
if errorlevel 1 goto fail
copy /y "%~dp0nssm.exe" "%DIR%\" >nul
if errorlevel 1 goto fail
copy /y "%~dp0uninstall.bat" "%DIR%\" >nul

echo Installing the reader bridge: card readers at %BAUD% baud to %SERVER%:%TCPPORT%
set REASON=Windows did not create the service ^(the message above says why^).
"%NSSM%" install %SVC% "%DIR%\ReaderBridge.exe" --server %SERVER%:%TCPPORT% --baud %BAUD% %EXTRA%
if errorlevel 1 goto fail
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

echo Starting the service
"%NSSM%" start %SVC% >nul 2>&1
rem Starting can take a few seconds: wait for "running" up to 20 seconds. Ask sc, not
rem nssm: nssm writes UTF-16 into a pipe, which findstr can't search.
set TRIES=0
:waitrunning
sc query %SVC% | findstr /c:"RUNNING" >nul
if not errorlevel 1 goto running
set /a TRIES+=1
if %TRIES% geq 20 goto notrunning
timeout /t 1 /nobreak >nul
goto waitrunning

:notrunning
echo.
echo Service status:
sc query %SVC%
if exist "%DIR%\bridge.log" (
    echo Last lines of %DIR%\bridge.log:
    powershell -NoProfile -Command "Get-Content -Tail 15 '%DIR%\bridge.log'"
)
set REASON=The service was installed but does not keep running ^(see the lines above^).
goto fail

:running
echo.
echo Done. Server %SERVER%, TCP port %TCPPORT%.
echo The card readers work in any USB socket; nothing else to set up.
if "%REACHED%"=="0" echo BUT the server did not answer: the readers wait until it does.
echo Log: %DIR%\bridge.log
echo.
pause
exit /b 0

:fail
echo.
if defined REASON echo %REASON%
echo.
echo Installation FAILED. Send a photo of this window to the administrator.
pause
exit /b 1
