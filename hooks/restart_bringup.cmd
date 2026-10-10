@echo off
rem Scriptorium restart sequence (written 2026-10-10 by Claude via the bridge; Omri asked for an unattended bring-up)
cd /d C:\Users\User\Desktop\scriptorium
echo === scr verify
.venv\Scripts\scr.exe verify
if errorlevel 1 ( echo VERIFY FAILED - stopping & pause & exit /b 1 )
echo === UI
.venv\Scripts\scr.exe up --ui-only --no-browser
echo === watcher (unattended, statements allowed until T-0302 lands)
start "scriptorium watcher" /MIN .venv\Scripts\scr.exe watch --unattended --allow-statements --quiet
echo === hotfix T-0330: tests then close (this window stays open; takes up to 2 h)
.venv\Scripts\scr.exe hotfix close T-0330 --summary "job runner (docs/proposals/job-runner.md): scr job submit/list/tail/kill/wait, detached wrapper, jobs_concurrency queue; new waiting status that holds no slot; watcher jobs_tick (poll, deadline kill, job_done/job_failed, resume by session id); review sessions get review_bg_wait_minutes=120 + foreground instruction (root cause of every dead review 9-10 Oct); briefs/agents/UI updated; tests/test_jobs.py; also covers T-0285 intake stdin fix"
echo === T-0285 (intake fix, same tree)
.venv\Scripts\scr.exe --actor omri task set T-0285 accepted --force-accept --note "intake stdin fix; covered by T-0330 test run"
echo === done
pause
