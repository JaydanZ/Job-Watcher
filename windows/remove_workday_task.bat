@echo off
rem Stops the scheduled Workday checks on this computer.
schtasks /Delete /TN "Job Watcher - Workday" /F
pause
