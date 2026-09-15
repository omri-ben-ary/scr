<#
.SYNOPSIS
  Sweep uncommitted work out of mission worktrees into git: for every worktree under
  .worktrees/ that is NOT a task worktree (T-xxxx) and has uncommitted or untracked files,
  commit them on that worktree's branch ("[sweep] ..."), push, and (with -Merge) open a PR
  into the agents' main branch and merge it. Nothing is ever lost or deleted; the commit is
  the freeze point (e.g. PREDICTION_shape.md gets its sha here).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\sweep_worktrees.ps1 -Merge
#>
[CmdletBinding()]
param([switch]$Merge, [switch]$DryRun)
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$wl = Get-Content (Join-Path $Root 'config\workloads.yaml') -Raw
$repoRel = if ($wl -match "(?m)^\s*repo:\s*(\S+)") { $Matches[1] } else { '../quantum-insertion' }
$main = if ($wl -match "(?m)^\s*main_branch:\s*(\S+)") { $Matches[1] } else { 'master' }
$Repo = (Resolve-Path (Join-Path $Root $repoRel)).Path
$exclude = @(':(exclude)build.log', ':(exclude)**/__pycache__/**', ':(exclude)**/.pytest_cache/**', ':(exclude).lake/**', ':(exclude).loopy/**', ':(exclude)**/*.pyc', ':(exclude)*.lock')
foreach ($d in Get-ChildItem (Join-Path $Repo '.worktrees') -Directory | Where-Object { $_.Name -notmatch '^T-\d+$' }) {
  $wt = $d.FullName
  $dirty = git -C $wt status --porcelain --untracked-files=all -- . 2>$null | Where-Object { $_ -notmatch '\.lake/|__pycache__|\.pytest_cache|build\.log' }
  if (-not $dirty) { continue }
  $branch = (git -C $wt rev-parse --abbrev-ref HEAD).Trim()
  Write-Host "=== $($d.Name) ($branch): $($dirty.Count) uncommitted file(s)" -ForegroundColor Cyan
  $dirty | Select-Object -First 25 | ForEach-Object { Write-Host "  $_" }
  if ($DryRun) { continue }
  git -C $wt add -A -- . @exclude
  git -C $wt commit -q -m "[sweep] uncommitted work found in .worktrees/$($d.Name) on $branch ($(Get-Date -Format s))`n`nSwept by hooks/sweep_worktrees.ps1 so it exists in git history; content unreviewed."
  if ($LASTEXITCODE -ne 0) { Write-Host "  commit failed" -ForegroundColor Red; continue }
  git -C $wt push -u origin $branch
  if ($LASTEXITCODE -ne 0) { Write-Host "  push failed" -ForegroundColor Red; continue }
  if ($Merge) {
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $gh) { Write-Host "  gh missing; PR by hand: $branch -> $main"; continue }
    $url = (gh pr list --head $branch --json url --jq '.[0].url' 2>$null)
    if (-not $url) { Push-Location $wt; $url = (gh pr create --base $main --head $branch --title "[sweep] $($d.Name): land uncommitted mission work" --body "Swept by hooks/sweep_worktrees.ps1; see the commit message. Merging keeps the agents' main branch a complete record." 2>&1 | Select-Object -Last 1).ToString().Trim(); Pop-Location }
    Write-Host "  PR: $url"
    gh pr merge $url --merge --delete-branch=false 2>&1 | Select-Object -Last 1
  }
}
Write-Host "sweep done."
