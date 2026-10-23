<#
.SYNOPSIS
  Steward hook: run loopy-prover on one frozen target of a Scriptorium task, logging the run
  as scr events. Wraps ../quantum-insertion/missions/LOOPY/scripts/run_loopy.ps1.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\loopy_phase_c.ps1 -Task T-0031 `
      -File QuantumInsertion/Example.lean -Target example_lemma -- --max-iterations 3
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Task,
  [Parameter(Mandatory = $true)][string]$File,
  [string]$Target,
  [string]$Worktree,                     # run inside .worktrees/<name> (the task's worktree) instead of the main checkout
  [string]$Workload = 'quantum-insertion',
  [string]$Root,                         # the scriptorium state root (default: SCRIPTORIUM_ROOT, else the current folder)
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$LoopyArgs
)
$ErrorActionPreference = 'Stop'
# The state root: -Root, else SCRIPTORIUM_ROOT, else the current folder (this file ships inside the scr package,
# so the folder above it is no longer the scriptorium root).
if (-not $Root) { $Root = if ($env:SCRIPTORIUM_ROOT) { $env:SCRIPTORIUM_ROOT } else { (Get-Location).Path } }
$Root = (Resolve-Path $Root).Path
$Actor = "steward:$Task"
# Prefer the repo's own venv (uv sync creates .venv\Scripts\scr.exe); fall back to `python -m scr`.
$ScrExe = Join-Path $Root '.venv\Scripts\scr.exe'
function Scr { param([string[]]$a)
  if (Test-Path $ScrExe) { & $ScrExe --root $Root --actor $Actor --quiet @a } else { & python -m scr --root $Root --actor $Actor --quiet @a }
  if ($LASTEXITCODE -ne 0) { throw "scr $($a -join ' ') failed" } }

Push-Location $Root
try {
  if (Test-Path $ScrExe) { & $ScrExe --root $Root verify --quiet } else { & python -m scr --root $Root verify --quiet }
  if ($LASTEXITCODE -ne 0) { throw 'scr verify failed — not proceeding (log verify_failed and stop)' }
  $wl = (Get-Content (Join-Path $Root 'config\workloads.yaml') -Raw)
  if ($wl -notmatch "(?m)^\s*loopy:\s*(\S+)") { throw "workloads.yaml has no 'loopy:' entry for $Workload" }
  $repoRel = if ($wl -match "(?m)^\s*repo:\s*(\S+)") { $Matches[1] } else { "../$Workload" }
  $repo = (Resolve-Path (Join-Path $Root $repoRel)).Path
  $runner = Join-Path $repo (($wl | Select-String "(?m)^\s*loopy:\s*(\S+)").Matches[0].Groups[1].Value)
  if (-not (Test-Path $runner)) { throw "loopy runner not found: $runner" }

  $args = @('-File', $File)
  if ($Target) { $args += @('-Target', $Target) }
  if ($Worktree) { $args += @('-Mode', 'worktree', '-Name', $Worktree) }
  if ($LoopyArgs) { $args += ($LoopyArgs | Where-Object { $_ -ne '--' }) }

  $note = "loopy prove-loop $File" + $(if ($Target) { " --target $Target" } else { '' })
  Scr @('event', 'system', '--name', 'loopy_start', '--task', $Task, '--note', $note)
  Add-Content -Encoding UTF8 (Get-ChildItem (Join-Path $Root 'tasks') -Directory | Where-Object Name -like "$Task-*" | Select-Object -First 1 | ForEach-Object { Join-Path $_.FullName 'log.md' }) `
    "`n- $(Get-Date -Format s) [steward] loopy start: $note"

  $start = Get-Date
  & powershell -ExecutionPolicy Bypass -File $runner @args
  $rc = $LASTEXITCODE
  $mins = [int]((Get-Date) - $start).TotalMinutes

  # find the run folder the wrapper archived (newest under missions/LOOPY/runs of the tree it ran in)
  $tree = if ($Worktree) { Join-Path $repo ".worktrees\loopy-$Worktree" } else { $repo }
  $runDir = Get-ChildItem (Join-Path $tree 'missions\LOOPY\runs') -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
  $usage = ''
  if ($runDir) {
    $u = Get-ChildItem (Join-Path $runDir.FullName 'logs') -Filter '*.log' -ErrorAction SilentlyContinue | ForEach-Object { Select-String -Path $_.FullName -Pattern 'TOTAL USAGE' } | Select-Object -Last 1
    if ($u) { $usage = $u.Line.Trim() }
  }
  $end = "loopy exit $rc after ${mins}m; run=$(if ($runDir) { $runDir.Name } else { '?' }); $usage"
  Scr @('event', 'system', '--name', 'loopy_end', '--task', $Task, '--note', $end, '--data', "{exit: $rc, minutes: $mins, run: '$(if ($runDir) { $runDir.Name } else { '' })'}")
  Add-Content -Encoding UTF8 (Get-ChildItem (Join-Path $Root 'tasks') -Directory | Where-Object Name -like "$Task-*" | Select-Object -First 1 | ForEach-Object { Join-Path $_.FullName 'log.md' }) `
    "- $(Get-Date -Format s) [steward] loopy end: $end"
  Write-Host "`n[loopy_phase_c] $end" -ForegroundColor Cyan
  Write-Host "Next: review the diff on branch loopy/<name>, run done_when_checker + statement_diff on that tree, then cherry-pick onto the task branch."
  exit $rc
} finally { Pop-Location }
