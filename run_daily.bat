@echo off
REM ===========================================================================
REM  NSE EOD pipeline - nightly run (~19:00 IST)
REM  Registered with Windows Task Scheduler; see docs at the bottom of README.md
REM ===========================================================================
REM
REM  EXIT-CODE TRANSLATION (the important part)
REM  ------------------------------------------
REM  run-daily exits:
REM     0  clean
REM     1  WARNINGS  <- normal. Open review items, illiquid-name breaks, etc.
REM     2  CRITICAL  <- an orphaned factor, a missed corporate action, a failed
REM                     check. The universe was NOT rebuilt.
REM     3  crash
REM  Task Scheduler treats any non-zero result as a failure and will retry, so a
REM  warnings run must be translated to 0 or the task retries every night
REM  forever and the retry signal becomes meaningless. Only >= 2 propagates.
REM
REM  PATH NOTE
REM  ---------
REM  Z: on this host is a LOCAL FIXED volume (Win32_LogicalDisk DriveType 3,
REM  label "Storage"), NOT a mapped network drive, so it IS visible to a
REM  service/off-session account and needs no UNC indirection. Verified:
REM  `net use` lists no Z: mapping.
REM
REM  The "mapped drives are invisible to service accounts" caveat applies only to
REM  drives created with `net use` inside an interactive logon session. If this
REM  repo is later moved to a real share, swap the PUSHD line below for the
REM  commented UNC form: pushd resolves a UNC path to a temporary drive letter
REM  for the life of the script, which is the only reliable way to reach a share
REM  from a scheduled task.

setlocal EnableDelayedExpansion

REM --- repo location -------------------------------------------------------
set "REPO=Z:\nse_eod_pipeline"
pushd "%REPO%" || (echo [FATAL] cannot reach %REPO% & exit /b 3)

REM --- if this ever moves to a share, use these two lines instead: ----------
REM set "REPO=\\SERVERNAME\quant\nse_eod_pipeline"
REM pushd "%REPO%" || (echo [FATAL] cannot reach %REPO% & exit /b 3)

REM --- venv ----------------------------------------------------------------
set "PY=%REPO%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [FATAL] venv interpreter missing: %PY%
    popd & exit /b 3
)

REM --- log file: one per day, appended, so a retry lands in the same file ---
if not exist "%REPO%\logs" mkdir "%REPO%\logs"
for /f "tokens=1-3 delims=/-. " %%a in ("%DATE%") do set "DSTAMP=%%c%%b%%a"
set "LOG=%REPO%\logs\run_daily_%DSTAMP%.log"

echo. >> "%LOG%"
echo ============================================================ >> "%LOG%"
echo [START] %DATE% %TIME%  host=%COMPUTERNAME% user=%USERNAME% >> "%LOG%"
echo ============================================================ >> "%LOG%"

REM --- the run: stdout AND stderr appended ---------------------------------
"%PY%" -m nse_eod.cli run-daily >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"

echo [EXIT] run-daily raw exit code = !RC! >> "%LOG%"

REM --- dead-man's switch stamp, so a silent no-run is visible by morning ---
"%PY%" -m nse_eod.cli last-success >> "%LOG%" 2>&1

REM --- translate ------------------------------------------------------------
REM  0 -> 0 clean
REM  1 -> 0 warnings are a NORMAL completed run; do not trigger a retry
REM >=2 -> propagate so Task Scheduler retries and the failure is visible
set "TRC=0"
if !RC! GEQ 2 set "TRC=!RC!"

if "!TRC!"=="0" (
    echo [RESULT] success ^(raw=!RC!; 1=warnings, translated to 0^) >> "%LOG%"
) else (
    echo [RESULT] FAILURE raw=!RC! - propagating so Task Scheduler retries >> "%LOG%"
)

popd
REM NOTE: %TRC% not !TRC! on this line, deliberately.
REM `endlocal` discards the setlocal environment, and in a compound
REM `endlocal & exit /b X` the DELAYED form !TRC! is expanded AFTER endlocal has
REM already run -- by then TRC no longer exists, so it expands to nothing and
REM `exit /b` returns 0. The observed symptom was the log correctly saying
REM "propagating raw=2" while the caller received 0, i.e. a real failure
REM reported as success and no Task Scheduler retry. %TRC% is expanded at PARSE
REM time, before endlocal executes, which is the standard idiom.
endlocal & exit /b %TRC%
