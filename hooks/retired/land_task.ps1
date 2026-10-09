<#
.SYNOPSIS
  Commit a submitted task's worktree, push its branch, open the PR, and record the acceptance.

.DESCRIPTION
  For one task id:
    1. in its worktree (<repo>/.worktrees/<id>, branch task/<id>): stage everything except build
       noise (build.log, __pycache__, .pytest_cache, .lake, .loopy), show what is outside the
       task's scope.files (warning only — the Steward already judged it), commit with a message
       built from report.md;
    2. git push -u origin task/<id>;
    3. gh pr create --base <workload.main_branch> with the report's Result section as the body
       (skipped with -NoPR; -Draft opens a draft);
    4. scr task set <id> accepted and a `pr` system event with the URL.
  Merging stays yours (docs/PR_WORKFLOW.md: verify master's TREE after merging, then
  `scr task set <id> merged --tokens N` and retire the worktree with hooks/retire_worktree.sh).
  Never deletes anything, never touches master, never force-pushes.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\land_task.ps1 -Task T-0021
  powershell -ExecutionPolicy Bypass -File hooks\land_task.ps1 -Task T-0024 -Draft
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Task,
  [switch]$NoPR,
  [switch]$Draft,
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ScrExe = Join-Path $Root '.venv\Scripts\scr.exe'
function Scr { param([string]$Actor, [string[]]$a)
  if (Test-Path $ScrExe) { $out = & $ScrExe --root $Root --actor $Actor @a 2>&1 } else { $out = & python -m scr --root $Root --actor $Actor @a 2>&1 }
  if ($LASTEXITCODE -ne 0) { throw "scr $($a -join ' ') failed:`n$out" }
  return $out }

$dir = Get-ChildItem (Join-Path $Root 'tasks') -Directory | Where-Object Name -like "$Task-*" | Select-Object -First 1
if (-not $dir) { throw "no record for $Task" }
$y = Get-Content (Join-Path $dir.FullName 'task.yaml') -Raw
$status = if ($y -match "(?m)^status:\s*(\S+)") { $Matches[1] } else { '?' }
$title  = if ($y -match "(?m)^title:\s*(.+)$") { $Matches[1].Trim("'`" ") } else { $Task }
$scope = @()
if ($y -match "(?ms)^scope:\s*\n\s*files:\s*\n((?:\s*-\s*.+\n)+)") { $scope = ($Matches[1] -split "`n" | ForEach-Object { ($_ -replace '^\s*-\s*', '').Trim() } | Where-Object { $_ }) }

$wl = Get-Content (Join-Path $Root 'config\workloads.yaml') -Raw
$repoRel = if ($wl -match "(?m)^\s*repo:\s*(\S+)") { $Matches[1] } else { '../quantum-insertion' }
$main    = if ($wl -match "(?m)^\s*main_branch:\s*(\S+)") { $Matches[1] } else { 'master' }
$Repo = (Resolve-Path (Join-Path $Root $repoRel)).Path
$wt = Join-Path $Repo ".worktrees\$Task"
if (-not (Test-Path $wt)) { throw "worktree missing: $wt" }
$branch = (git -C $wt rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "task/$Task") { throw "worktree is on '$branch', expected task/$Task — not landing from the wrong branch" }

Write-Host "=== $Task [$status] on $branch" -ForegroundColor Cyan
if ($status -notin 'submitted', 'accepted') { Write-Host "  note: status is '$status' (expected submitted) — continuing, but check the record" -ForegroundColor Yellow }

# ---- 1. stage + commit -------------------------------------------------------------------
$exclude = @(':(exclude)build.log', ':(exclude)**/__pycache__/**', ':(exclude)**/.pytest_cache/**', ':(exclude).lake/**', ':(exclude).loopy/**', ':(exclude)**/*.pyc')
git -C $wt add -A -- . @exclude
$staged = @(git -C $wt diff --cached --name-only)
if (-not $staged) { Write-Host "  nothing to commit on $branch (already committed?)"; }
else {
  $outside = @($staged | Where-Object { $p = $_; -not ($scope | Where-Object { $p -eq $_.TrimEnd('/') -or $p.StartsWith($_.TrimEnd('/') + '/') }) })
  Write-Host "  staged $($staged.Count) file(s):"; $staged | ForEach-Object { Write-Host "    $_" }
  if ($scope.Count -and $outside.Count) { Write-Host "  outside scope.files (Steward judged these; listed for the PR): $($outside -join ', ')" -ForegroundColor Yellow }

  $report = Join-Path $dir.FullName 'report.md'
  $result = ''
  if (Test-Path $report) {
    $txt = Get-Content $report -Raw
    if ($txt -match '(?ms)^## Result\s*\n(.*?)(?=^## )') { $result = $Matches[1].Trim() }
  }
  $summary = ($result -split "`n" | Where-Object { $_.Trim() -and $_ -notmatch '^\|' -and $_ -notmatch '^#' } | Select-Object -First 6) -join "`n"
  $msg = "[$Task] $title`n`n$summary`n`nScriptorium task $Task; report: scriptorium/tasks/$($dir.Name)/report.md"
  $msgFile = Join-Path $dir.FullName 'commit-msg.txt'; Set-Content -Encoding UTF8 $msgFile $msg
  if ($DryRun) { Write-Host "`n--- commit message ---`n$msg"; git -C $wt reset -q; exit 0 }
  git -C $wt commit -q -F $msgFile
  if ($LASTEXITCODE -ne 0) { throw 'git commit failed' }
  Write-Host "  committed $((git -C $wt rev-parse --short HEAD).Trim())"
}

# ---- 2. push -----------------------------------------------------------------------------
git -C $wt push -u origin $branch
if ($LASTEXITCODE -ne 0) { throw 'git push failed (credentials? remote?)' }

# ---- 3. PR --------------------------------------------------------------------------------
$url = ''
if (-not $NoPR) {
  $gh = Get-Command gh -ErrorAction SilentlyContinue
  if (-not $gh) { Write-Host "  gh not on PATH — open the PR by hand: $branch -> $main" -ForegroundColor Yellow }
  else {
    $existing = (gh pr list --repo (git -C $wt remote get-url origin) --head $branch --json url --jq '.[0].url' 2>$null)
    if ($existing) { $url = $existing.Trim(); Write-Host "  PR already exists: $url" }
    else {
      $report = Join-Path $dir.FullName 'report.md'
      $body = "Scriptorium task **$Task** — $title`n`n"
      if (Test-Path $report) {
        $txt = Get-Content $report -Raw
        if ($txt -match '(?ms)^## Result\s*\n(.*?)(?=^## )') { $body += "## Result (from report.md)`n`n" + $Matches[1].Trim() + "`n`n" }
        if ($txt -match '(?ms)^## Statement changes\s*\n(.*?)(?=^## )') { $body += "## Statement changes`n`n" + $Matches[1].Trim() + "`n`n" }
      }
      $body += "Full record: ``scriptorium/tasks/$($dir.Name)/`` (spec, log, report, review).`n`n"
      $body += "🤖 Generated with [Claude Code](https://claude.com/claude-code)`n`nhttps://claude.ai/code/session_01RXEyBuy3AYS9wnj5BWYyqG`n"
      $bodyFile = Join-Path $dir.FullName 'pr-body.md'; Set-Content -Encoding UTF8 $bodyFile $body
      $prArgs = @('pr', 'create', '--base', $main, '--head', $branch, '--title', "[$Task] $title", '--body-file', $bodyFile)
      if ($Draft) { $prArgs += '--draft' }
      Push-Location $wt
      try { $url = (& gh @prArgs 2>&1 | Select-Object -Last 1).ToString().Trim() } finally { Pop-Location }
      Write-Host "  PR: $url"
    }
  }
}

# ---- 4. record -----------------------------------------------------------------------------
if ($status -eq 'submitted') { Scr "steward:$Task" @('task', 'set', $Task, 'accepted', '--note', "landed on $branch$(if ($url) { "; PR $url" })", '--quiet') | Out-Null }
if ($url) { Scr "steward:$Task" @('event', 'system', '--name', 'pr', '--task', $Task, '--note', $url, '--quiet') | Out-Null }
Write-Host "`nNext (you): review + merge the PR, verify $main's tree, then:" -ForegroundColor Green
Write-Host "  .venv\Scripts\scr.exe --actor steward:$Task task set $Task merged --tokens <n>"
Write-Host "  bash hooks/retire_worktree.sh --repo $Repo --worktrees $Repo\.worktrees --main $main --name $Task --execute"
