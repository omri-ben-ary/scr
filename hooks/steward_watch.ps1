<#
.SYNOPSIS
  The mechanical half of the Steward, for every running task at once. Run it once and leave
  the window open; it ticks every -IntervalMinutes (default from limits.yaml
  checkpoint_every_minutes) and prints a one-screen dashboard each tick.

.DESCRIPTION
  Per tick, for each task in dispatched / in_progress / checkpoint:
    - activity: newest file change in the worktree (excluding .lake/.git) and in the task's
      log.md; a task silent for more than -StaleMinutes is flagged;
    - scope drift: files changed in the worktree (git status) outside scope.files → flagged
      and logged as a `scope_drift` system event (once per file);
    - HELP: lines in log.md with no `[steward]` line after them → listed under "needs you"
      and logged as a `help` event (once per line);
    - budget: wall hours since the task went in_progress vs budget.max_wall_hours → flagged
      and logged as `budget_wall` (once);
    - submission: report.md differs from templates/report.md → `task set <id> submitted`,
      logged; the task moves to the "review" list;
    - checkpoint: records `checkpoint` → `in_progress` for every still-running task (the
      brief's 30-minute heartbeat).
  For each task in submitted:
    - with -AutoReview, starts ONE headless Claude session per submitted task (once) with
      agents/steward.md + the record as its brief, to run the workload's done_when_checker
      and statement_diff and fill review.md; the decision (accepted / returned / escalated)
      stays yours unless -AutoDecide is also given.
  Never deletes anything; never edits Lean; never answers a HELP: line itself.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\steward_watch.ps1
  powershell -ExecutionPolicy Bypass -File hooks\steward_watch.ps1 -Once
  powershell -ExecutionPolicy Bypass -File hooks\steward_watch.ps1 -AutoReview
#>
[CmdletBinding()]
param(
  [int]$IntervalMinutes = 0,
  [int]$StaleMinutes = 45,
  [switch]$Once,
  [switch]$AutoReview,
  [switch]$AutoDecide,
  [switch]$AutoDispatch,      # when running tasks < limits.concurrency: scr dispatch + launch_dispatched.ps1 -Mode headless
  [switch]$AllowStatements,   # pass --allow-statements to the auto-dispatch (statement tasks need Gate 1 anyway)
  [switch]$AutoLand,          # accepted tasks: commit + push + PR + merge into main (agent branch), mark merged, retire worktree
  [switch]$AutoIntake,        # after any merge, and nightly: run hooks\intake_run.ps1 (Inbox + report unlocks -> tasks)
  [switch]$Unattended,        # shorthand: -AutoReview -AutoDecide -AutoDispatch -AutoLand -AutoIntake
  [ValidateSet('bypassPermissions', 'acceptEdits', 'default')][string]$PermissionMode = 'acceptEdits'
)
$ErrorActionPreference = 'Continue'
if ($Unattended) { $AutoReview = $true; $AutoDecide = $true; $AutoDispatch = $true; $AutoLand = $true; $AutoIntake = $true }
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ScrExe = Join-Path $Root '.venv\Scripts\scr.exe'
$LogFile = Join-Path $Root 'reports\steward_watch.log'
New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
$StateFile = Join-Path $Root 'reports\steward_watch.state.txt'    # keys already flagged, one per line (not part of the hash tree)
$State = @{}
if (Test-Path $StateFile) { Get-Content $StateFile | ForEach-Object { if ($_.Trim()) { $State[$_.Trim()] = $true } } }

function Scr { param([string]$Actor, [string[]]$a)
  if (Test-Path $ScrExe) { $out = & $ScrExe --root $Root --actor $Actor @a 2>&1 } else { $out = & python -m scr --root $Root --actor $Actor @a 2>&1 }
  if ($LASTEXITCODE -ne 0) { Log "scr $($a -join ' ') FAILED: $out"; return $null }
  return $out }
function ScrOk { param([string]$Actor, [string[]]$a)
  if (Test-Path $ScrExe) { $null = & $ScrExe --root $Root --actor $Actor @a 2>&1 } else { $null = & python -m scr --root $Root --actor $Actor @a 2>&1 }
  return ($LASTEXITCODE -eq 0) }
function Log([string]$m) { $line = "$(Get-Date -Format s) $m"; Add-Content -Encoding UTF8 $LogFile $line; Write-Host $line }
function Flag([string]$key) { if ($State.ContainsKey($key)) { return $false }; $State[$key] = $true; return $true }
function SaveState { $State.Keys | Sort-Object | Set-Content -Encoding UTF8 $StateFile }

$limits = Get-Content (Join-Path $Root 'config\limits.yaml') -Raw
if ($IntervalMinutes -le 0) { $IntervalMinutes = if ($limits -match "(?m)^checkpoint_every_minutes:\s*(\d+)") { [int]$Matches[1] } else { 30 } }
$wl = Get-Content (Join-Path $Root 'config\workloads.yaml') -Raw
$repoRel = if ($wl -match "(?m)^\s*repo:\s*(\S+)") { $Matches[1] } else { '../quantum-insertion' }
$Repo = (Resolve-Path (Join-Path $Root $repoRel)).Path
$WtRoot = Join-Path $Repo '.worktrees'
$ReportTemplate = (Get-Content (Join-Path $Root 'templates\report.md') -Raw).Trim()

function Tasks([string]$statusRegex) {
  Get-ChildItem (Join-Path $Root 'tasks') -Directory | ForEach-Object {
    $y = Get-Content (Join-Path $_.FullName 'task.yaml') -Raw
    if ($y -match "(?m)^status:\s*($statusRegex)\s*$") {
      $st = $Matches[1]
      $id = if ($y -match "(?m)^id:\s*(T-\d+)") { $Matches[1] } else { $null }
      $hours = if ($y -match "(?m)^\s*max_wall_hours:\s*([\d.]+)") { [double]$Matches[1] } else { 6 }
      $files = @()
      if ($y -match "(?ms)^scope:\s*\n\s*files:\s*\n((?:\s*-\s*.+\n)+)") { $files = ($Matches[1] -split "`n" | ForEach-Object { ($_ -replace '^\s*-\s*', '').Trim() } | Where-Object { $_ }) }
      elseif ($y -match "(?m)^\s*files:\s*\[(.*?)\]") { $files = ($Matches[1] -split ',' | ForEach-Object { $_.Trim().Trim("'`"") } | Where-Object { $_ }) }
      $title = ''; if ($y -match "(?m)^title:\s*(.+)$") { $title = $Matches[1].Trim("'`" ") }
      [pscustomobject]@{ Id = $id; Status = $st; Dir = $_.FullName; Hours = $hours; Files = $files; Title = $title }
    }
  }
}
function StartedAt([string]$id) {
  # last state event that moved the task into in_progress
  $ts = $null
  Get-Content (Join-Path $Root 'events\events.jsonl') | ForEach-Object {
    try { $e = $_ | ConvertFrom-Json } catch { return }
    if ($e.kind -eq 'state' -and $e.task -eq $id -and $e.to -eq 'in_progress' -and $e.from -eq 'dispatched') { $ts = $e.ts }
  }
  if ($ts) { [datetime]::Parse($ts).ToUniversalTime() } else { $null }
}
function NewestChange([string]$dir) {
  if (-not (Test-Path $dir)) { return $null }
  $f = Get-ChildItem $dir -Recurse -File -ErrorAction SilentlyContinue |
       Where-Object { $_.FullName -notmatch '[\\/]\.lake[\\/]|[\\/]\.git[\\/]|[\\/]\.loopy[\\/]' } |
       Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
  if ($f) { $f.LastWriteTimeUtc } else { $null }
}
function InScope([string]$path, [string[]]$scope) {
  foreach ($s in $scope) { $s2 = $s.TrimEnd('/'); if ($path -eq $s2 -or $path.StartsWith("$s2/")) { return $true } }
  return $false
}

do {
  $now = (Get-Date).ToUniversalTime()
  Write-Host "`n================ steward_watch $(Get-Date -Format 'yyyy-MM-dd HH:mm') (every $IntervalMinutes min) ================" -ForegroundColor Cyan
  # Scribers write log.md/report.md continuously, which legitimately changes the docs hashes:
  # refresh the snapshot first (what the pre-commit hook does), then verify the chain and replay.
  ScrOk 'steward-watch' @('refresh', '--quiet') | Out-Null
  $ok = ScrOk 'steward-watch' @('verify', '--quiet')
  if (-not $ok) { Log 'verify FAILED — not touching records this tick (Gate: verify_failed)'; Scr 'steward-watch' @('event', 'system', '--name', 'verify_failed', '--note', 'steward_watch: verify failed', '--quiet') | Out-Null }

  $needsYou = @(); $rows = @()
  foreach ($t in Tasks 'dispatched|in_progress|checkpoint') {
    $wt = Join-Path $WtRoot $t.Id
    $log = Join-Path $t.Dir 'log.md'
    $last = NewestChange $wt
    $logLast = if (Test-Path $log) { (Get-Item $log).LastWriteTimeUtc } else { $null }
    $newest = @($last, $logLast) | Where-Object { $_ } | Sort-Object -Descending | Select-Object -First 1
    $idle = if ($newest) { [int]($now - $newest).TotalMinutes } else { -1 }
    $flags = @()

    # --- scope drift
    if (Test-Path $wt) {
      $changed = git -C $wt status --porcelain --untracked-files=all 2>$null | ForEach-Object { ($_ -replace '^...', '').Trim('"') -replace '\\', '/' }
      foreach ($c in $changed) {
        if ($c -match '^\.loopy/|^missions/LOOPY/runs/') { continue }
        if ($t.Files.Count -gt 0 -and -not (InScope $c $t.Files) -and (Flag "drift:$($t.Id):$c")) {
          Log "$($t.Id) SCOPE DRIFT: $c"; Scr "steward:$($t.Id)" @('event', 'system', '--name', 'scope_drift', '--task', $t.Id, '--note', $c, '--quiet') | Out-Null
        }
      }
      $drift = @($changed | Where-Object { $t.Files.Count -gt 0 -and $_ -notmatch '^\.loopy/|^missions/LOOPY/runs/' -and -not (InScope $_ $t.Files) })
      if ($drift.Count) { $flags += "drift:$($drift.Count)" }
    } else { $flags += 'no-worktree' }

    # --- HELP: lines without a steward answer after them
    if (Test-Path $log) {
      $lines = Get-Content $log
      for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\s*(-\s*)?HELP:') {
          $answered = $false
          for ($j = $i + 1; $j -lt $lines.Count; $j++) { if ($lines[$j] -match '\[steward\]') { $answered = $true; break }; if ($lines[$j] -match '^\s*(-\s*)?HELP:') { break } }
          if (-not $answered) {
            $needsYou += "$($t.Id) HELP (log.md:$($i+1)): $($lines[$i].Trim())"
            if (Flag "help:$($t.Id):$($i+1)") { Scr "steward:$($t.Id)" @('event', 'system', '--name', 'help', '--task', $t.Id, '--note', $lines[$i].Trim(), '--quiet') | Out-Null }
          }
        }
      }
    }

    # --- budget (wall)
    $started = StartedAt $t.Id
    $wallH = if ($started) { [math]::Round(($now - $started).TotalHours, 1) } else { $null }
    if ($wallH -and $wallH -gt $t.Hours) { $flags += "OVER-WALL($wallH/$($t.Hours)h)"; if (Flag "wall:$($t.Id)") { Log "$($t.Id) wall budget exhausted: $wallH h > $($t.Hours) h → next transition should be escalated"; Scr "steward:$($t.Id)" @('event', 'system', '--name', 'budget_wall', '--task', $t.Id, '--note', "$wallH h", '--quiet') | Out-Null }; $needsYou += "$($t.Id) over wall budget ($wallH h of $($t.Hours) h) — escalate or extend" }

    # --- submission
    $rep = Join-Path $t.Dir 'report.md'
    $submitted = (Test-Path $rep) -and ((Get-Content $rep -Raw).Trim() -ne $ReportTemplate)
    if ($submitted -and $t.Status -ne 'submitted') {
      if ($t.Status -eq 'checkpoint') { Scr "steward:$($t.Id)" @('task', 'set', $t.Id, 'in_progress', '--quiet') | Out-Null }
      Scr "steward:$($t.Id)" @('task', 'set', $t.Id, 'submitted', '--note', 'report.md written (steward_watch)', '--quiet') | Out-Null
      Log "$($t.Id) → submitted (report.md written)"; $flags += 'SUBMITTED'
    } elseif ($t.Status -in 'in_progress', 'checkpoint') {
      # heartbeat: checkpoint → in_progress
      if ($t.Status -eq 'in_progress') { Scr "steward:$($t.Id)" @('task', 'set', $t.Id, 'checkpoint', '--quiet') | Out-Null }
      Scr "steward:$($t.Id)" @('task', 'set', $t.Id, 'in_progress', '--quiet') | Out-Null
    }
    if ($idle -ge $StaleMinutes) { $flags += "STALE ${idle}m"; $needsYou += "$($t.Id) silent for $idle min — is its session alive?" }
    $rows += [pscustomobject]@{ task = $t.Id; status = $t.Status; idle_min = $idle; wall_h = $wallH; flags = ($flags -join ' '); title = $t.Title.Substring(0, [math]::Min(60, $t.Title.Length)) }
  }

  # --- submitted: optional auto-review
  $review = @()
  foreach ($t in Tasks 'submitted') {
    $review += $t.Id
    if ($AutoReview -and (Flag "review:$($t.Id)")) {
      $wt = Join-Path $WtRoot $t.Id
      $brief = @"
You are the Steward for Scriptorium task $($t.Id) (see $Root\agents\steward.md — follow its "On submission" section exactly).
Record: $($t.Dir)  (spec.md, report.md, log.md; write review.md there).
Worktree: $wt   Workload config: $Root\config\workloads.yaml (done_when_checker, statement_diff, allowed_axioms, main_branch).
Run the mechanical checks from the worktree, run statement_diff against main, fill review.md completely, and end with a Decision line: accepted | returned (feedback) | escalated (Gate N, reason).
$(if ($AutoDecide) { "Then apply the decision with scr (--actor steward:$($t.Id)): accepted→merged path only if statement diff is EMPTY and every mechanical check passes; otherwise returned or escalated. Never merge a branch yourself; never edit Lean." } else { "Do NOT change the task status — Omri applies the decision." })
"@
      $bf = Join-Path $t.Dir 'review-brief.txt'; Set-Content -Encoding UTF8 $bf $brief
      $tr = Join-Path $t.Dir "review-session-$(Get-Date -Format yyyyMMdd-HHmmss).txt"
      $claude = Get-Command claude -ErrorAction SilentlyContinue
      if ($claude -and (Test-Path $wt)) {
        $p = Start-Process -FilePath $claude.Source -ArgumentList @('-p', '--permission-mode', $PermissionMode, '--output-format', 'text') -WorkingDirectory $wt -RedirectStandardInput $bf -RedirectStandardOutput $tr -RedirectStandardError "$tr.err" -PassThru -WindowStyle Hidden
        Log "$($t.Id) review session started (pid $($p.Id)) → $tr"
        Scr "steward:$($t.Id)" @('event', 'system', '--name', 'review_started', '--task', $t.Id, '--note', "pid $($p.Id)", '--quiet') | Out-Null
      } else { Log "$($t.Id) cannot start review: claude or worktree missing" }
    }
  }

  # --- auto-land: accepted -> committed, pushed, PR merged into the agents' main branch, merged, worktree retired
  $mergedNow = 0
  if ($AutoLand) {
    foreach ($t in Tasks 'accepted') {
      if (-not (Flag "land:$($t.Id)")) { continue }   # one attempt per task; failures are flagged for you
      Log "$($t.Id) auto-land: commit/push/PR"
      $out = & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'land_task.ps1') -Task $t.Id 2>&1
      $out | ForEach-Object { Log "  land: $_" }
      $url = ($out | Where-Object { $_ -match 'https://github.com/\S+/pull/\d+' } | Select-Object -Last 1)
      if ($url) { $url = ([regex]::Match($url, 'https://github.com/\S+/pull/\d+')).Value }
      if (-not $url) { $needsYou += "$($t.Id) auto-land: no PR URL (see reports/steward_watch.log) — land by hand"; $State.Remove("land:$($t.Id)"); continue }
      $m = & gh pr merge $url --merge --delete-branch=false 2>&1
      if ($LASTEXITCODE -ne 0) { Log "  merge failed: $m"; $needsYou += "$($t.Id) PR merge failed: $url"; $State.Remove("land:$($t.Id)"); continue }
      # verify main's TREE, not the PR status (docs/PR_WORKFLOW.md)
      $main = if ($wl -match "(?m)^\s*main_branch:\s*(\S+)") { $Matches[1] } else { 'master' }
      git -C $Repo fetch --quiet origin 2>$null
      $branch = "task/$($t.Id)"
      $contained = git -C $Repo branch -r --contains $branch 2>$null | Where-Object { $_ -match "origin/$main" }
      if (-not $contained) { Log "  WARNING: origin/$main does not contain $branch after merge"; $needsYou += "$($t.Id) merged PR but origin/$main does not contain task/$($t.Id) — check"; continue }
      $tok = 0; $rep = Join-Path $t.Dir 'report.md'
      if ((Test-Path $rep) -and ((Get-Content $rep -Raw) -match '(?i)(\d[\d,.]*)\s*(k|m)?\s*tokens')) { $n = [double]($Matches[1] -replace ',', ''); if ($Matches[2] -eq 'k') { $n *= 1e3 } elseif ($Matches[2] -eq 'm') { $n *= 1e6 }; $tok = [int]$n }
      Scr "steward:$($t.Id)" @('task', 'set', $t.Id, 'merged', '--tokens', "$tok", '--note', "auto-land: $url", '--quiet') | Out-Null
      Log "$($t.Id) -> merged ($url)"; $mergedNow++
      $retire = Join-Path $Root 'hooks\retire_worktree.sh'
      $bash = Get-Command bash -ErrorAction SilentlyContinue
      if ($bash -and (Test-Path $retire)) {
        $r = & $bash.Source $retire --repo $Repo --worktrees (Join-Path $Repo '.worktrees') --main $main --name $t.Id --execute 2>&1
        Log "  retire: exit $LASTEXITCODE"; if ($LASTEXITCODE -ne 0) { $needsYou += "$($t.Id) worktree retire returned $LASTEXITCODE (see log)" }
      } else { $needsYou += "$($t.Id) merged; retire its worktree by hand (bash/retire_worktree.sh not found)" }
    }
  }
  # --- auto-intake: turn Inbox lines and report unlocks into tasks after merges (and once per night)
  if ($AutoIntake) {
    $hour = (Get-Date).Hour
    $nightly = ($hour -eq 22) -and (Flag "intake:$(Get-Date -Format yyyy-MM-dd)")
    if ($mergedNow -gt 0 -or $nightly) {
      Log "intake run ($(if ($nightly) { 'nightly' } else { "$mergedNow merge(s)" }))"
      & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'intake_run.ps1') 2>&1 | ForEach-Object { Log "  intake: $_" }
    }
  }

  # --- auto-dispatch: keep the machine busy up to the cap, unattended
  if ($AutoDispatch) {
    $cap = if ($limits -match "(?m)^concurrency:\s*(\d+)") { [int]$Matches[1] } else { 3 }
    $running = @(Tasks 'dispatched|in_progress|checkpoint|submitted').Count
    if ($running -lt $cap) {
      $dArgs = @('dispatch', '--quiet'); if ($AllowStatements) { $dArgs += '--allow-statements' }
      $out = Scr 'prioritizer' $dArgs
      if ($out -and ($out -join ' ') -match 'dispatched T-') {
        Log "auto-dispatch: $((($out | Where-Object { $_ -match 'dispatched T-' }) -join '; '))"
        & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'launch_dispatched.ps1') -Mode headless | ForEach-Object { Log "  launcher: $_" }
      }
    }
  }

  SaveState
  $rows | Format-Table -AutoSize -Wrap | Out-String -Width 220 | Write-Host
  if ($review.Count) { Write-Host "REVIEW (submitted, waiting for a decision): $($review -join ', ')" -ForegroundColor Yellow }
  if ($needsYou.Count) { Write-Host "NEEDS YOU:" -ForegroundColor Red; $needsYou | ForEach-Object { Write-Host "  - $_" } } else { Write-Host "Nothing needs you this tick." -ForegroundColor Green }
  Scr 'steward-watch' @('refresh', '--quiet') | Out-Null
  if ($Once) { break }
  Start-Sleep -Seconds ($IntervalMinutes * 60)
} while ($true)
