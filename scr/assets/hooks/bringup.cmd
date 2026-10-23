@echo off
rem Scriptorium bring-up after reboot. Safe to re-run. The state root is SCRIPTORIUM_ROOT (default: the Desktop
rem folder); scr is the live release (<runtime>\current, scr upgrade) when one is installed, else the root's .venv.
setlocal
if not defined SCRIPTORIUM_ROOT set "SCRIPTORIUM_ROOT=C:\Users\User\Desktop\scriptorium"
cd /d "%SCRIPTORIUM_ROOT%"
if not defined SCR_RUNTIME set "SCR_RUNTIME=%SCRIPTORIUM_ROOT%\..\scr-runtime"
set "SCR=%SCR_RUNTIME%\current\Scripts\scr.exe"
if not exist "%SCR%" set "SCR=%SCRIPTORIUM_ROOT%\.venv\Scripts\scr.exe"
echo === stop any running UI / watcher
"%SCR%" up --stop
echo === refresh snapshot (a reboot can interrupt the watcher between an event append and the snapshot write)
"%SCR%" --actor omri refresh --note "snapshot refresh at bring-up"
echo === scr verify
"%SCR%" verify
if errorlevel 1 ( echo VERIFY FAILED - stopping & pause & exit /b 1 )
echo === UI
"%SCR%" up --ui-only --no-browser
echo === watcher (unattended, statements allowed)
start "scriptorium watcher" /MIN "%SCR%" watch --unattended --allow-statements --quiet
echo === version
"%SCR%" version
echo === done - you can close this window
pause
