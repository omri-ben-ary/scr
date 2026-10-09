<#
.SYNOPSIS
  Start Scriptorium: the UI (your entry point) and the unattended watcher, registered at logon.
  Everything lives in `scr up` now; this file only exists so the old command keeps working.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\scriptorium_up.ps1            # start + register
  powershell -ExecutionPolicy Bypass -File hooks\scriptorium_up.ps1 -RestartUI  # after an update
#>
param([switch]$NoRegister, [switch]$NoUI, [switch]$UIOnly, [switch]$RestartUI, [int]$Port = 8765)
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$a = @('up', '--port', "$Port")
if ($NoRegister) { $a += '--no-register' }
if ($UIOnly -or $NoUI) { $a += '--ui-only' }
if ($RestartUI) { $a += '--restart' }
& (Join-Path $Root '.venv\Scripts\scr.exe') --root $Root @a
