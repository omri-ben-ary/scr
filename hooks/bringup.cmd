@echo off
rem Scriptorium bring-up after reboot (2026-10-13). Safe to re-run.
cd /d C:\Users\User\Desktop\scriptorium
echo === scr verify
.venv\Scripts\scr.exe verify
if errorlevel 1 ( echo VERIFY FAILED - stopping & pause & exit /b 1 )
echo === UI
.venv\Scripts\scr.exe up --ui-only --no-browser
echo === watcher (unattended, statements allowed)
start "scriptorium watcher" /MIN .venv\Scripts\scr.exe watch --unattended --allow-statements --quiet
echo === status
.venv\Scripts\scr.exe status
echo === done - you can close this window
pause
