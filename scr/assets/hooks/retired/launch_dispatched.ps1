<#
.SYNOPSIS
  One command from `scr dispatch` to running scriber sessions.

.DESCRIPTION
  For every task in status `dispatched` (or the ids given with -Tasks):
    1. creates its worktree if missing (hooks\spawn_worktrees.ps1; Lean tasks get the
       shared-packages junction, tasks whose scope has no .lean file get -NoLake),
    2. renders the launch prompt from the record (`scr task brief <id>`),
    3. marks the task in_progress (actor steward:<id>) and logs a `launch` event,
    4. starts a Claude Code session in the worktree:
         -Mode tabs      (default) a new Windows Terminal tab per task, interactive, with the
                         brief as the opening message — you can watch and intervene;
         -Mode headless  `claude -p` per task, detached, transcript appended to the task's
                         log.md; for unattended runs. Uses -PermissionMode (default
                         bypassPermissions, same as loopy: the session is confined to a
                         worktree and the scriber protocol needs the Lean MCP tools).
  Never deletes anything. Re-running is safe: tasks already in_progress are skipped unless
  -Relaunch is given.

.EXAMPLE
  .\.venv\Scripts\scr.exe --actor prioritizer dispatch --allow-statements
  powershell -ExecutionPolicy Bypass -File hooks\launch_dispatched.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\launch_dispatched.ps1 -Mode headless -Tasks T-0025,T-0026
#>
[CmdletBinding()]
param(
  [string[]]$Tasks,
  [ValidateSet('tabs', 'headless')][string]$Mode = 'tabs',
  [ValidateSet('bypassPermissions', 'acceptEdits', 'default')][string]$PermissionMode = 'bypassPermissions',
  [string]$Model,
  [switch]$Relaunch,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ScrExe = Join-Path $Root '.venv\Scripts\scr.exe'
function Scr { param([string]$Actor, [string[]]$a)
  if (Test-Path $ScrExe) { $out = & $ScrExe --root $Root --actor $Actor @a 2>&1 } else { $out = & python -m scr --root $Root --actor $Actor @a 2>&1 }
  if ($LASTEXITCODE -ne 0) { throw "scr $($a -join ' ') failed:`n$out" }
  return $out }
function TaskDir([string]$id) { Get-ChildItem (Join-Path $Root 'tasks') -Directory | Where-Object Name -like "$id-*" | Select-Object -First 1 }
function TaskYaml([string]$id) { Get-Content (Join-Path (TaskDir $id).FullName 'task.yaml') -Raw }

# ---- 0. verify, pick tasks ----------------------------------------------------------
Scr 'launcher' @('verify', '--quiet') | Out-Null
$wl = Get-Content (Join-Path $Root 'config\workloads.yaml') -Raw
$repoRel = if ($wl -match "(?m)^\s*repo:\s*(\S+)") { $Matches[1] } else { '../quantum-insertion' }
$Repo = (Resolve-Path (Join-Path $Root $repoRel)).Path
$WtRoot = Join-Path $Repo '.worktrees'

if (-not $Tasks) {
  $Tasks = Get-ChildItem (Join-Path $Root 'tasks') -Directory | ForEach-Object {
    $y = Get-Content (Join-Path $_.FullName 'task.yaml') -Raw
    $want = if ($Relaunch) { 'dispatched|in_progress|checkpoint' } else { 'dispatched' }
    if ($y -match "(?m)^status:\s*($want)\s*$" -and $y -match "(?m)^id:\s*(T-\d+)") { $Matches[1] }
  }
}
if (-not $Tasks) { Write-Host "Nothing in status 'dispatched'. Run: .\.venv\Scripts\scr.exe --actor prioritizer dispatch"; exit 0 }
Write-Host "Launching: $($Tasks -join ', ')  (mode: $Mode)" -ForegroundColor Cyan

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) { throw 'claude (Claude Code CLI) not on PATH' }
$wt = Get-Command wt -ErrorAction SilentlyContinue

# ---- 1. worktrees --------------------------------------------------------------------
$lean = @(); $nolean = @()
foreach ($t in $Tasks) {
  $y = TaskYaml $t
  if ($y -match '\.lean') { $lean += $t } else { $nolean += $t }
}
$spawn = Join-Path $PSScriptRoot 'spawn_worktrees.ps1'
if (-not $DryRun) {
  if ($nolean) { & powershell -ExecutionPolicy Bypass -File $spawn -Tasks $nolean -NoLake | Out-Host }
  if ($lean)   { & powershell -ExecutionPolicy Bypass -File $spawn -Tasks $lean | Out-Host }
}

# ---- 2–4. per task: brief, in_progress, launch ------------------------------------------
foreach ($t in $Tasks) {
  $dir = TaskDir $t
  $wtDir = Join-Path $WtRoot $t
  $brief = (Scr "steward:$t" @('task', 'brief', $t)) -join "`n"
  $briefFile = Join-Path $dir.FullName 'brief.txt'
  Set-Content -Encoding UTF8 $briefFile $brief
  $status = if ((TaskYaml $t) -match "(?m)^status:\s*(\S+)") { $Matches[1] } else { '?' }
  Write-Host "`n=== $t  [$status]  $wtDir" -ForegroundColor Cyan
  if ($DryRun) { Write-Host $brief; continue }
  if (-not (Test-Path $wtDir)) { Write-Host "  worktree missing — skipped" -ForegroundColor Yellow; continue }

  if ($status -eq 'dispatched') { Scr "steward:$t" @('task', 'set', $t, 'in_progress', '--quiet') | Out-Null }

  $modelArgs = @(); if ($Model) { $modelArgs = @('--model', $Model) }
  if ($Mode -eq 'headless') {
    $log = Join-Path $dir.FullName 'log.md'
    Add-Content -Encoding UTF8 $log "`n- $(Get-Date -Format s) [launcher] headless scriber session started (permission-mode $PermissionMode)"
    $transcript = Join-Path $dir.FullName "session-$(Get-Date -Format yyyyMMdd-HHmmss).txt"
    # Detached: stdin gets the brief, stdout/stderr go to a per-launch transcript next to the record.
    $cliArgs = @('-p', '--permission-mode', $PermissionMode, '--output-format', 'text') + $modelArgs
    $p = Start-Process -FilePath $claude.Source -ArgumentList $cliArgs -WorkingDirectory $wtDir `
           -RedirectStandardInput $briefFile -RedirectStandardOutput $transcript -RedirectStandardError "$transcript.err" -PassThru -WindowStyle Hidden
    Add-Content -Encoding UTF8 $log "- $(Get-Date -Format s) [launcher] pid $($p.Id), transcript $transcript"
    Scr "steward:$t" @('event', 'system', '--name', 'launch', '--task', $t, '--note', "headless pid $($p.Id)", '--quiet') | Out-Null
    Write-Host "  headless pid $($p.Id); transcript: $transcript"
  } else {
    # Interactive tab: claude "<brief>" opens a session with the brief as the first message.
    # The brief is read from brief.txt inside the tab, so newlines/quotes never touch the command line.
    $m = ($modelArgs -join ' ')
    $cmd = "claude $m (Get-Content -Raw -Encoding UTF8 '$briefFile')"
    if ($wt) {
      & $wt.Source -w 0 new-tab --title $t -d $wtDir powershell -NoExit -Command $cmd
    } else {
      Start-Process powershell -WorkingDirectory $wtDir -ArgumentList '-NoExit', '-Command', $cmd
    }
    Scr "steward:$t" @('event', 'system', '--name', 'launch', '--task', $t, '--note', 'interactive tab', '--quiet') | Out-Null
    Write-Host "  tab opened"
  }
}
Write-Host "`nAll launched. Watch them with:  powershell -ExecutionPolicy Bypass -File hooks\steward_watch.ps1" -ForegroundColor Green
