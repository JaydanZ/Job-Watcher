@echo off
setlocal
title Job watcher - Workday check
rem Runs one Workday check right now and shows the end of the log.

cd /d "%~dp0.."
set "PY="
for /f "delims=" %%P in ('where py.exe 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY (
  echo Python isn't installed, or isn't on your PATH. See setup_workday.bat.
  pause
  exit /b 1
)
echo Checking Workday companies. This can take a few minutes...
"%PY%" job_watcher.py --local-workday
echo.
echo ---- Last part of workday_log.txt ----
powershell -NoProfile -Command "Get-Content -Path 'workday_log.txt' -Tail 30"
echo.
pause
