<#
.SYNOPSIS
  Run the Intake agent once, headless: Inbox lines and `unlocks` blocks of merged/rejected
  reports become task records (scr task new), triaged and ready/blocked. Records the run.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\intake_run.ps1
#>
[CmdletBinding()]
param([switch]$DryRun)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) { throw 'claude not on PATH' }
$brief = @"
You are Intake. Read $Root\agents\intake.md and $Root\agents\_common.md and follow them exactly.
Scriptorium root: $Root (run scr from there; use .venv\Scripts\scr.exe --actor intake).
Sources this run: (1) every line under '## Inbox' in queues\omri.md — one task each, then remove the line;
(2) the unlocks block of every report.md whose task reached merged or rejected since the last intake run
(find them with scr replay and the 'intake_ran' system events); (3) system events blocker_rejected / intake_starved.
For every task you create: fill spec.md from the template with a real Goal, Scope (files!), Definition of done
(mechanical lines wherever possible) and Context; set done_when in task.yaml; add informs/blocks edges with
--reason; then task set <id> triaged and ready (or blocked). Respect limits.yaml (wip_ready_scribers, max_depth).
Never create a task that names a declaration an open task already names (dedup by scope; add an informs edge instead).
End with: scr event system --name intake_ran --note '<n created>'; scr verify; git add -A; git commit -m '[intake] ...'.
$(if ($DryRun) { 'DRY RUN: do not create anything; print what you would create.' })
"@
$out = Join-Path $Root "reports\intake-$(Get-Date -Format yyyyMMdd-HHmmss).txt"
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null
$brief | & $claude.Source -p --permission-mode acceptEdits --output-format text 2>&1 | Tee-Object -FilePath $out | Select-Object -Last 15
