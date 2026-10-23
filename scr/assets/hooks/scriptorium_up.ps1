<#
.SYNOPSIS
  Start Scriptorium: the UI (your entry point) and the unattended watcher, registered at logon.
  Everything lives in `scr up` now; this file only exists so the old command keeps working.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\scriptorium_up.ps1            # start + register
  powershell -ExecutionPolicy Bypass -File hooks\scriptorium_up.ps1 -RestartUI  # after an update
#>
param([switch]$NoRegister, [switch]$NoUI, [switch]$UIOnly, [switch]$RestartUI, [int]$Port = 8765, [string]$Root, [string]$Runtime)
# The state root: -Root, else SCRIPTORIUM_ROOT, else the current folder (this file ships inside the scr package,
# so the folder above it is no longer the scriptorium root).
if (-not $Root) { $Root = if ($env:SCRIPTORIUM_ROOT) { $env:SCRIPTORIUM_ROOT } else { (Get-Location).Path } }
$Root = (Resolve-Path $Root).Path
# The scr to run: the live release (<runtime>\current, scr upgrade) once one is installed, else the state root's .venv.
if (-not $Runtime) { $Runtime = if ($env:SCR_RUNTIME) { $env:SCR_RUNTIME } else { Join-Path (Split-Path $Root -Parent) 'scr-runtime' } }
$ScrExe = Join-Path $Runtime 'current\Scripts\scr.exe'
if (-not (Test-Path $ScrExe)) { $ScrExe = Join-Path $Root '.venv\Scripts\scr.exe' }
$a = @('up', '--port', "$Port")
if ($NoRegister) { $a += '--no-register' }
if ($UIOnly -or $NoUI) { $a += '--ui-only' }
if ($RestartUI) { $a += '--restart' }
& $ScrExe --root $Root @a
