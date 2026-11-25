@echo off
setlocal EnableDelayedExpansion
title Job watcher - Workday setup
rem Sets up a Windows scheduled task that checks the Workday companies every 30 minutes.
rem Double-click to run. Safe to run again (it just updates the task).

cd /d "%~dp0.."
if not exist job_watcher.py (
  echo Couldn't find job_watcher.py. Keep this "windows" folder inside your job watcher folder.
  pause
  exit /b 1
)

rem --- Find Python. The windowless version is used, so nothing pops up every 30 minutes.
set "PYW="
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%P"
if not defined PYW for /f "delims=" %%P in ('where pyw.exe 2^>nul') do if not defined PYW set "PYW=%%P"
if not defined PYW (
  echo Python isn't installed, or isn't on your PATH.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH"
  echo on the first screen of the installer, then run this file again.
  pause
  exit /b 1
)
echo Using Python: !PYW!

rem --- Your ntfy topic (same as the NTFY_TOPIC secret on GitHub). Saved only on this computer.
if exist ntfy_topic.txt (
  echo Using your saved ntfy topic from ntfy_topic.txt
) else (
  set "TOPIC="
  set /p "TOPIC=Paste your ntfy topic name and press Enter: "
  if "!TOPIC!"=="" (
    echo No topic entered.
    pause
    exit /b 1
  )
  > ntfy_topic.txt echo !TOPIC!
)

rem --- Where to fetch your latest settings and company list from
if not exist sync_url.txt (
  > sync_url.txt echo https://raw.githubusercontent.com/JaydanZ/Job-Watcher/main/
)

rem --- Create (or update) the scheduled task
set "TASK=Job Watcher - Workday"
schtasks /Create /F /SC MINUTE /MO 30 /TN "%TASK%" /TR "\"!PYW!\" \"%CD%\job_watcher.py\" --local-workday" >nul
if errorlevel 1 (
  echo Couldn't create the scheduled task.
  pause
  exit /b 1
)
rem Also run on battery power, and catch up after the computer wakes from sleep
powershell -NoProfile -Command "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 25); Set-ScheduledTask -TaskName '%TASK%' -Settings $s | Out-Null" 2>nul
if errorlevel 1 echo (Note: couldn't change the battery settings. On a laptop it may only run while plugged in.)

schtasks /Run /TN "%TASK%" >nul
echo.
echo Done. Workday companies will be checked every 30 minutes while you're signed in.
echo The first two checks quietly record the jobs already posted. Alerts start after that.
echo Open workday_log.txt in this folder to see what each check did.
echo.
pause
