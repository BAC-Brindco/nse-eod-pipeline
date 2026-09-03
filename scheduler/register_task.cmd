@echo off
REM ===========================================================================
REM  Register the NSE EOD nightly run with Windows Task Scheduler.
REM  Run this ONCE, from an ELEVATED prompt.
REM ===========================================================================
REM
REM  TIMEZONE  -- read this before changing ST
REM  ----------------------------------------
REM  /ST takes the time in the SERVER'S LOCAL TIME, not UTC or IST.
REM  Verified on this host (NODE238):
REM      Get-TimeZone  ->  Id = "India Standard Time", BaseUtcOffset = 05:30:00
REM  so local time IS IST and ST 19:00 is correct as written.
REM
REM  If you move this to a server whose clock is UTC, 19:00 IST becomes
REM  13:30 UTC. Check FIRST, every time:
REM      powershell -NoProfile -Command "Get-TimeZone | Select Id,BaseUtcOffset"
REM  and if BaseUtcOffset is 00:00:00, use /ST 13:30 instead of /ST 19:00.
REM
REM  Getting this wrong is silent: the task runs happily at the wrong hour and,
REM  at 13:30 IST, before NSE has published the bhavcopy -- so it would find no
REM  data, skip, and the dead-man's switch would still look satisfied.
REM
REM  CREDENTIALS
REM  -----------
REM  /RU and /RP are supplied INTERACTIVELY below. No password is stored in this
REM  file or in the repo. Passing /RP with no value makes schtasks prompt.
REM  /RL HIGHEST because the task must run whether or not anyone is logged on.
REM
REM  RETRY
REM  -----
REM  schtasks.exe CANNOT set retry-on-failure. This command gives you the
REM  schedule only. To get "retry every 15 minutes, up to 3 times", import
REM  nse_eod_daily.xml instead (see the README), or import it over this task
REM  afterwards.

setlocal

set "TASKNAME=NSE EOD Daily"
set "SCRIPT=Z:\nse_eod_pipeline\run_daily.bat"

if not exist "%SCRIPT%" (
    echo [FATAL] %SCRIPT% not found
    exit /b 1
)

echo Registering "%TASKNAME%"
echo   script : %SCRIPT%
echo   when   : daily 19:00 SERVER LOCAL TIME
echo.
echo Current server time zone:
powershell -NoProfile -Command "Get-TimeZone | Select-Object Id,BaseUtcOffset | Format-List"
echo If BaseUtcOffset is 00:00:00 (UTC), press Ctrl-C now and change /ST to 13:30.
echo.
pause

schtasks /Create ^
  /TN "%TASKNAME%" ^
  /TR "\"%SCRIPT%\"" ^
  /SC DAILY ^
  /ST 19:00 ^
  /RL HIGHEST ^
  /RU "%USERDOMAIN%\%USERNAME%" ^
  /RP ^
  /F

if errorlevel 1 (
    echo [FATAL] registration failed
    exit /b 1
)

echo.
echo Registered. Verify and test with:
echo   schtasks /Query /TN "%TASKNAME%" /V /FO LIST
echo   schtasks /Run   /TN "%TASKNAME%"
echo.
echo schtasks cannot set retry-on-failure. To add "every 15 min, up to 3 times":
echo   schtasks /Delete /TN "%TASKNAME%" /F
echo   schtasks /Create /TN "%TASKNAME%" /XML "Z:\nse_eod_pipeline\scheduler\nse_eod_daily.xml" /RU "%USERDOMAIN%\%USERNAME%" /RP

endlocal
