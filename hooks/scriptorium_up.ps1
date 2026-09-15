<#
.SYNOPSIS
  Start everything, once: the UI (your entry point) and the unattended watcher, and register
  both to start at logon so a reboot does not need you.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File hooks\scriptorium_up.ps1            # start now + register at logon
  powershell -ExecutionPolicy Bypass -File hooks\scriptorium_up.ps1 -NoRegister
#>
[CmdletBinding()]
param([switch]$NoRegister, [switch]$NoUI, [switch]$UIOnly, [int]$Port = 8765)
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$scr = Join-Path $Root '.venv\Scripts\scr.exe'
$watch = "powershell -ExecutionPolicy Bypass -File `"$Root\hooks\steward_watch.ps1`" -Unattended"
$ui    = "`"$scr`" ui --port $Port --no-browser"
if (-not $NoRegister) {
  if (-not $UIOnly) { schtasks /Create /F /TN "Scriptorium\Watcher" /SC ONLOGON /RL LIMITED /TR $watch | Out-Null }
  schtasks /Create /F /TN "Scriptorium\UI" /SC ONLOGON /RL LIMITED /TR $ui | Out-Null
  Write-Host "registered at logon: Scriptorium\UI$(if (-not $UIOnly) { ', Scriptorium\Watcher' })"
}
# start the watcher now if not running (skipped with -UIOnly: the UI's chore list starts it when you press Run)
if (-not $UIOnly) {
  $running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'steward_watch\.ps1' }
  if (-not $running) { Start-Process powershell -ArgumentList '-ExecutionPolicy', 'Bypass', '-File', "$Root\hooks\steward_watch.ps1", '-Unattended' -WindowStyle Minimized; Write-Host "watcher started (minimized window)" } else { Write-Host "watcher already running" }
}
if (-not $NoUI) {
  $uiRunning = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'scr(\.exe)? ui' }
  if (-not $uiRunning) { Start-Process $scr -ArgumentList 'ui', '--port', "$Port", '--no-browser' -WindowStyle Minimized; Start-Sleep 2 }
  Start-Process "http://127.0.0.1:$Port/"
  Write-Host "UI: http://127.0.0.1:$Port/"
}
