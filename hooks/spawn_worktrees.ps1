<#
.SYNOPSIS
  Create one quantum-insertion worktree per dispatched Scriptorium task, sharing the
  dependency checkouts so N worktrees do not mean N copies of Mathlib.

.DESCRIPTION
  For every task in status dispatched/in_progress (or the ids you pass with -Tasks):
    .worktrees/<id>  on branch task/<id>  (from master), with
    .lake/packages  -> junction to the main checkout's .lake/packages   (Mathlib, QuantumOptimization: shared, read-mostly)
    .lake/build     <- robocopy of the main checkout's .lake/build      (the library's own oleans; per-worktree so builds do not collide)
  then prints the launch line for that task's Steward/scriber session.
  Numerics-only tasks (-NoLake) skip the .lake setup entirely.

  Never deletes anything; retire with hooks/retire_worktree.sh (quarantine).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\spawn_worktrees.ps1
  powershell -ExecutionPolicy Bypass -File hooks\spawn_worktrees.ps1 -Tasks T-0025,T-0026,T-0029 -NoLake
#>
[CmdletBinding()]
param(
  [string[]]$Tasks,
  [switch]$NoLake,
  [string]$Workload = 'quantum-insertion'
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$wl   = Get-Content (Join-Path $Root 'config\workloads.yaml') -Raw
$repoRel = if ($wl -match "(?m)^\s*repo:\s*(\S+)") { $Matches[1] } else { "../$Workload" }
$main    = if ($wl -match "(?m)^\s*main_branch:\s*(\S+)") { $Matches[1] } else { 'master' }
$Repo    = (Resolve-Path (Join-Path $Root $repoRel)).Path
$WtRoot  = Join-Path $Repo '.worktrees'
New-Item -ItemType Directory -Force -Path $WtRoot | Out-Null

if (-not $Tasks) {
  $Tasks = Get-ChildItem (Join-Path $Root 'tasks') -Directory | ForEach-Object {
    $y = Get-Content (Join-Path $_.FullName 'task.yaml') -Raw
    if ($y -match "(?m)^status:\s*(dispatched|in_progress|checkpoint)\s*$" -and $y -match "(?m)^id:\s*(T-\d+)") { $Matches[1] }
  }
}
if (-not $Tasks) { Write-Host "No dispatched tasks. Run: scr --actor prioritizer dispatch"; exit 0 }

foreach ($t in $Tasks) {
  $wt = Join-Path $WtRoot $t
  $br = "task/$t"
  Write-Host "`n=== $t -> $wt ($br)" -ForegroundColor Cyan
  if (-not (Test-Path $wt)) {
    $exists = [bool]((git -C $Repo branch --list $br) -join '').Trim()
    if ($exists) { git -C $Repo worktree add $wt $br } else { git -C $Repo worktree add -b $br $wt $main }
    if ($LASTEXITCODE -ne 0) { throw "git worktree add failed for $t" }
  } else { Write-Host "  exists, reusing" }

  if (-not $NoLake) {
    $srcPk = Join-Path $Repo '.lake\packages'; $dstPk = Join-Path $wt '.lake\packages'
    $srcB  = Join-Path $Repo '.lake\build';    $dstB  = Join-Path $wt '.lake\build'
    New-Item -ItemType Directory -Force -Path (Join-Path $wt '.lake') | Out-Null
    if ((Test-Path $srcPk) -and -not (Test-Path $dstPk)) { New-Item -ItemType Junction -Path $dstPk -Target $srcPk | Out-Null; Write-Host "  junction .lake\packages -> main" }
    if ((Test-Path $srcB) -and -not (Test-Path $dstB)) {
      Write-Host "  robocopy .lake\build (library oleans; one-off per worktree) ..."
      robocopy $srcB $dstB /E /MT:16 /NFL /NDL /NJH /NJS /NP | Out-Null
      if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
    }
  }
  $spec = Get-ChildItem (Join-Path $Root 'tasks') -Directory | Where-Object Name -like "$t-*" | Select-Object -First 1
  Write-Host "  steward:  cd $Root; scr --actor steward:$t task set $t in_progress"
  Write-Host "  scriber:  cd $wt; claude   # then: 'Use the scriber agent. Brief: $($spec.FullName)\spec.md'"
}
Write-Host "`nRAM note: each Lean worktree with an open lean-lsp costs several GB; numerics tasks cost almost nothing. Stagger Lean sessions." -ForegroundColor Yellow
