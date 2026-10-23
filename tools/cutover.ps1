<#
.SYNOPSIS
  Phase 3 of the code/state split: switch the PC from the editable checkout to a tagged release.

.DESCRIPTION
  1. Pre-check: the live state verifies clean and no landing job is running; running sessions are listed (they keep
     going on the old install).
  2. Tag the state repo `pre-cutover` and record the old .venv.
  3. Install the release into <Runtime>\<Tag> (the old checkout's scr predates `scr upgrade`, so the NEW release
     runs the upgrade): `<Runtime>\<Tag>\Scripts\scr.exe --root <State> upgrade <Tag>`. That smokes the release,
     stops the watcher and the UI, migrates, points <Runtime>\current at it, restarts, smokes again, and rolls itself
     back on failure.
  4. On success: re-register the logon tasks and the desktop shortcut on <Runtime>\current, then a final smoke
     (version says the tag, verify, one dry tick, the UI answers).
  5. If the final part fails: current is removed, the services and the logon tasks go back to the old .venv, and the
     old install is checked.
  Prints PASS or FAIL. Windows PowerShell 5.1 compatible.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\cutover.ps1 -Tag v0.2.0
#>
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$State = (Join-Path $env:USERPROFILE "Desktop\scriptorium"),
    [string]$Runtime = "",
    [int]$UiPort = 8765,
    [string]$Wheel = "",                                   # install this wheel instead of the git tag (testing)
    [string]$Python = "",
    [string]$RepoUrl = "https://github.com/omri-ben-ary/scr"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$env:PYTHONIOENCODING = "utf-8"

$State = (Resolve-Path $State).Path
if (-not $Runtime) {
    $Runtime = if ($env:SCR_RUNTIME) { $env:SCR_RUNTIME } else { Join-Path (Split-Path $State -Parent) "scr-runtime" }
}
$env:SCR_RUNTIME = $Runtime                                # the new release resolves the same runtime folder
$OldVenv = Join-Path $State ".venv"
$OldScr = Join-Path $OldVenv "Scripts\scr.exe"
$Release = Join-Path $Runtime $Tag
$NewScr = Join-Path $Release "Scripts\scr.exe"
$Current = Join-Path $Runtime "current"
$CurScr = Join-Path $Current "Scripts\scr.exe"
$Shortcuts = @((Join-Path $State "Scriptorium.lnk"), (Join-Path $env:USERPROFILE "Desktop\Scriptorium.lnk"))
$Log = New-Object System.Collections.ArrayList

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $out = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }; $rc = $LASTEXITCODE }
    finally { $ErrorActionPreference = $old }
    return @{ rc = $rc; out = @($out) }
}

function Say([string]$Text) { Write-Host $Text; [void]$Log.Add($Text) }

function Step([string]$Name, [string]$Exe, [string[]]$Arguments, [switch]$Quiet) {
    $r = Invoke-Native $Exe $Arguments
    Say ("[{0}] {1} (rc {2})" -f $(if ($r.rc -eq 0) { "ok  " } else { "FAIL" }), $Name, $r.rc)
    if ($r.rc -ne 0 -or -not $Quiet) { $r.out | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 25 | ForEach-Object { Say "       $_" } }
    return $r
}

function Stop-Here([string]$Why) {
    Say ""; Say "FAIL: $Why"
    $logFile = Join-Path $State "reports\cutover-$Tag-$(Get-Date -Format yyyyMMdd-HHmmss).log"
    New-Item -ItemType Directory -Path (Split-Path $logFile -Parent) -Force | Out-Null
    [IO.File]::WriteAllText($logFile, ($Log -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding $false))
    Write-Host "log: $logFile"
    exit 1
}

function Remove-Shortcuts { foreach ($l in $Shortcuts) { if (Test-Path $l) { Remove-Item -Force $l; Say "  removed $l" } } }

function Restore-Old {
    # Back to the editable checkout: no current junction, services and logon tasks on the old .venv.
    Say ""; Say "restoring the old install"
    if (Test-Path $Current) { [void](Invoke-Native "cmd" @("/c", "rmdir", $Current)); Say "  removed the junction $Current" }
    [void](Step "stop services" $OldScr @("--root", $State, "up", "--stop"))
    Remove-Shortcuts
    [void](Step "logon tasks + shortcut on the old .venv" $OldScr @("--root", $State, "up", "--register", "--no-browser", "--port", "$UiPort"))
    $v = Step "old install verifies" $OldScr @("--root", $State, "verify", "--quiet")
    if ($v.rc -eq 0) { Say "  the system is as it was before the cut-over" } else { Say "  the old install does not verify: look before using it" }
}

Say "cut-over to $Tag"
Say "  state   : $State"
Say "  runtime : $Runtime"
Say "  old venv: $OldVenv"
if (-not (Test-Path $OldScr)) { Stop-Here "no old install at $OldScr" }
if (Test-Path $CurScr) { Stop-Here "$Current already exists: this PC is already on a release (use scr upgrade)" }

# 1. pre-check
$r = Step "live state verifies" $OldScr @("--root", $State, "verify", "--quiet")
if ($r.rc -ne 0) { Stop-Here "scr verify is not clean; fix that first" }
$jobs = Invoke-Native $OldScr @("--root", $State, "job", "list")
$landing = @($jobs.out | Where-Object { $_ -match "\bland-" })
if ($landing.Count -gt 0) { $landing | ForEach-Object { Say "  $_" }; Stop-Here "a landing job is running; wait for it to end" }
Say "[ok  ] no landing job running"

# 2. mark the point to come back to
$tagName = "pre-cutover"
if ((Invoke-Native "git" @("-C", $State, "rev-parse", "-q", "--verify", "refs/tags/$tagName")).rc -eq 0) { $tagName = "pre-cutover-$(Get-Date -Format yyyyMMdd-HHmmss)" }
$r = Step "tag the state repo $tagName" "git" @("-C", $State, "tag", $tagName)
if ($r.rc -ne 0) { Stop-Here "could not tag the state repo" }
Say "  old .venv recorded: $OldVenv"

# 3. install the release beside the live one, then let it run the upgrade
if (-not (Test-Path $NewScr)) {
    $py = if ($Python) { $Python } elseif (Test-Path (Join-Path $OldVenv "Scripts\python.exe")) { Join-Path $OldVenv "Scripts\python.exe" } elseif (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
    $pyArgs = if ($py -eq "py") { @("-3", "-m", "venv", $Release) } else { @("-m", "venv", $Release) }
    $r = Step "create the venv $Release" $py $pyArgs -Quiet
    if ($r.rc -ne 0) { Stop-Here "venv creation failed; nothing on the live system changed" }
    $src = if ($Wheel) { (Resolve-Path $Wheel).Path } else { "git+$RepoUrl@$Tag" }
    $r = Step "install $Tag" (Join-Path $Release "Scripts\python.exe") @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", $src) -Quiet
    if ($r.rc -ne 0 -or -not (Test-Path $NewScr)) { Stop-Here "install failed; nothing on the live system changed" }
}
$sessions = Invoke-Native (Join-Path $Release "Scripts\python.exe") @("-c", "import json,sys; from pathlib import Path; from scr.schema import running_sessions; [print(s.get('task') or '-', s.get('pid'), s.get('kind') or '') for s in running_sessions(Path(sys.argv[1]))]", $State)
if ($sessions.out.Count -gt 0 -and $sessions.rc -eq 0) { Say "sessions still running (they keep the old install until the Phase 4 drain):"; $sessions.out | ForEach-Object { Say "  $_" } }
else { Say "no headless sessions running" }

$up = Step "scr upgrade $Tag (smoke, stop, migrate, switch, start, smoke)" $NewScr @("--root", $State, "--actor", "omri", "upgrade", $Tag)
if ($up.rc -ne 0) {
    # upgrade restored current and the services itself; the logon tasks were never touched
    if (Test-Path $Current) { Restore-Old }
    Stop-Here "scr upgrade failed and rolled back; the old install is live"
}

# 4. logon tasks and shortcut on the release, then the final smoke
Remove-Shortcuts
$r = Step "logon tasks + shortcut on $Current" $CurScr @("--root", $State, "up", "--register", "--no-browser", "--port", "$UiPort")
$ok = ($r.rc -eq 0)
$v = Step "scr version names $Tag" $CurScr @("--root", $State, "version")
$ok = $ok -and $v.rc -eq 0 -and (($v.out -join "`n") -match [regex]::Escape($Tag.TrimStart("v")))
$r = Step "verify" $CurScr @("--root", $State, "verify", "--quiet"); $ok = $ok -and $r.rc -eq 0
$r = Step "one dry tick" $CurScr @("--root", $State, "watch", "--dry-run"); $ok = $ok -and $r.rc -eq 0
$uiOk = $false
for ($i = 0; $i -lt 30 -and -not $uiOk; $i++) {
    try { $uiOk = ((Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:$UiPort/api/state").StatusCode -eq 200) } catch { Start-Sleep -Seconds 1 }
}
Say ("[{0}] the UI answers on port {1}" -f $(if ($uiOk) { "ok  " } else { "FAIL" }), $UiPort)
$ok = $ok -and $uiOk

if (-not $ok) {
    Restore-Old
    Stop-Here "the final smoke failed; rolled back to the old install"
}
Say ""
Say "PASS: $Tag is live from $Current. The old .venv stays until the Phase 4 drain; running sessions keep it."
$logFile = Join-Path $State "reports\cutover-$Tag-$(Get-Date -Format yyyyMMdd-HHmmss).log"
[IO.File]::WriteAllText($logFile, ($Log -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding $false))
Write-Host "log: $logFile"
exit 0
