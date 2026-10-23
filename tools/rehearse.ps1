<#
.SYNOPSIS
  Phase 2 of the code/state split: rehearse a release against a COPY of the live state.

.DESCRIPTION
  Installs the release beside the live system (<Runtime>\<Tag>, a non-editable venv), copies the state folder
  to a temp folder, and runs against the copy, with the new scr.exe:
    migrate --dry-run, migrate, verify, watch --dry-run, the UI on a second port (GET / and /api/state),
    a print-only Discuss launch, version.
  Writes a markdown report into the copy and prints its path. Exit code 0 only when every step passed.

  It never writes to the live state folder, never repoints <Runtime>\current, and never touches the live .venv,
  the logon tasks or the desktop shortcut. Windows PowerShell 5.1 compatible.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\rehearse.ps1 -Tag v0.2.0
#>
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [string]$State = (Join-Path $env:USERPROFILE "Desktop\scriptorium"),
    [string]$Runtime = "",
    [int]$UiPort = 8766,
    [string]$Wheel = "",                                   # install this wheel (or source folder) instead of the git tag
    [string]$Python = "",                                  # interpreter that creates the release venv
    [string]$RepoUrl = "https://github.com/omri-ben-ary/scr"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false   # read scr's utf-8 output on a cp1255 console
$env:PYTHONIOENCODING = "utf-8"
$env:SCR_NO_NOTIFY = "1"                                   # no toasts or pushes from the copy

$State = (Resolve-Path $State).Path
if (-not $Runtime) {
    $Runtime = if ($env:SCR_RUNTIME) { $env:SCR_RUNTIME } else { Join-Path (Split-Path $State -Parent) "scr-runtime" }
}
$Release = Join-Path $Runtime $Tag
$NewScr = Join-Path $Release "Scripts\scr.exe"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Work = Join-Path ([IO.Path]::GetTempPath()) "scr-rehearsal-$Stamp"
$Copy = Join-Path $Work "scriptorium"
$Steps = New-Object System.Collections.ArrayList
$Failed = $false

function Invoke-Native([string]$Exe, [string[]]$Arguments, [string]$Cwd = $null) {
    # Native stderr must not become a terminating error under Stop (PowerShell 5.1); rc is checked explicitly.
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try {
        if ($Cwd) { Push-Location $Cwd }
        $out = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        $rc = $LASTEXITCODE
    } finally {
        if ($Cwd) { Pop-Location }
        $ErrorActionPreference = $old
    }
    return @{ rc = $rc; out = @($out) }
}

function Add-Step([string]$Name, [int]$Rc, [string[]]$Out, [bool]$Ok = ($Rc -eq 0)) {
    $tail = @($Out | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 12)
    [void]$Steps.Add([pscustomobject]@{ name = $Name; rc = $Rc; ok = $Ok; out = $tail })
    $mark = if ($Ok) { "ok  " } else { "FAIL" }
    Write-Host ("[{0}] {1} (rc {2})" -f $mark, $Name, $Rc)
    if (-not $Ok) { $tail | ForEach-Object { Write-Host "       $_" }; $script:Failed = $true }
}

function Invoke-Scr([string]$Name, [string[]]$Arguments) {
    $r = Invoke-Native $NewScr (@("--root", $Copy) + $Arguments) $Copy
    Add-Step $Name $r.rc $r.out
    return $r
}

function Find-Python {
    if ($Python) { return $Python }
    $venvPy = Join-Path $State ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }               # python -m venv from a venv uses its base interpreter
    if (Get-Command py -ErrorAction SilentlyContinue) { return "py" }
    return "python"
}

Write-Host "rehearsal of $Tag"
Write-Host "  live state : $State (read only)"
Write-Host "  release    : $Release"
Write-Host "  copy       : $Copy"

# 1. install the release beside the live system
if (Test-Path $NewScr) {
    Add-Step "install $Tag (already at $Release)" 0 @()
} else {
    $py = Find-Python
    $pyArgs = if ($py -eq "py") { @("-3", "-m", "venv", $Release) } else { @("-m", "venv", $Release) }
    $r = Invoke-Native $py $pyArgs
    if ($r.rc -eq 0) {
        $src = if ($Wheel) { (Resolve-Path $Wheel).Path } else { "git+$RepoUrl@$Tag" }
        $r = Invoke-Native (Join-Path $Release "Scripts\python.exe") @("-m", "pip", "install", "--quiet", "--disable-pip-version-check", $src)
    }
    Add-Step "install $Tag into $Release" $r.rc $r.out ($r.rc -eq 0 -and (Test-Path $NewScr))
}
if ($Failed) { Write-Host "FAIL: the release did not install; nothing else was run"; exit 1 }

# 2. copy the state (no .venv, no live lock); the copy loses its remote so nothing it does can be pushed
New-Item -ItemType Directory -Path $Copy -Force | Out-Null
$r = Invoke-Native "robocopy" @($State, $Copy, "/E", "/XD", ".venv", "/XF", "journal.lock", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1")
Add-Step "copy the state to $Copy" $r.rc $r.out ($r.rc -lt 8)            # robocopy: 0-7 is success
$isGit = Test-Path (Join-Path $Copy ".git")
$before = $null
if ($isGit) {
    foreach ($remote in (Invoke-Native "git" @("-C", $Copy, "remote")).out) {
        if ($remote) { [void](Invoke-Native "git" @("-C", $Copy, "remote", "remove", $remote)) }
    }
    $before = (Invoke-Native "git" @("-C", $Copy, "rev-parse", "HEAD")).out | Select-Object -First 1
}

# 3. the new release against the copy
[void](Invoke-Scr "migrate --dry-run" @("migrate", "--dry-run"))
[void](Invoke-Scr "migrate" @("--actor", "omri", "migrate"))
$after = if ($isGit) { (Invoke-Native "git" @("-C", $Copy, "rev-parse", "HEAD")).out | Select-Object -First 1 } else { $null }
$diff = if (-not $isGit) { @("(the state folder is not a git repo: no diff)") }
        elseif ($before -ne $after) { (Invoke-Native "git" @("-C", $Copy, "diff", "--stat", $before, $after)).out }
        else { @("(no commit: nothing to migrate)") }
[void](Invoke-Scr "verify" @("verify"))
[void](Invoke-Scr "watch --dry-run (one tick on a throwaway copy of the copy)" @("watch", "--dry-run"))

# the UI on a second port, so the live one on 8765 is undisturbed
$uiLog = Join-Path $Work "ui.log"
$ui = Start-Process -FilePath $NewScr -ArgumentList @("--root", "`"$Copy`"", "ui", "--port", "$UiPort", "--no-browser") `
      -WorkingDirectory $Copy -RedirectStandardOutput $uiLog -RedirectStandardError "$uiLog.err" -PassThru -WindowStyle Hidden
$got = @(); $uiOk = $false
try {
    foreach ($path in @("/", "/api/state")) {
        $ok = $false
        for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
            try {
                $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://127.0.0.1:$UiPort$path"
                $ok = ($resp.StatusCode -eq 200); $got += "GET $path -> $($resp.StatusCode) ($($resp.Content.Length) bytes)"
            } catch { Start-Sleep -Seconds 1 }
        }
        if (-not $ok) { $got += "GET $path -> no answer within 30 s" }
    }
    $uiOk = ($got | Where-Object { $_ -match "-> 200" }).Count -eq 2
} finally {
    if ($ui -and -not $ui.HasExited) { Stop-Process -Id $ui.Id -Force -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Copy*" -and $_.CommandLine -like "* ui *" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }   # the UI's --child process
}
Add-Step "UI on port $UiPort" ($(if ($uiOk) { 0 } else { 1 })) $got $uiOk

# a print-only Discuss launch; it writes agent files under the user profile, so give it a throwaway one
$realProfile = $env:USERPROFILE; $realHome = $env:HOME
try {
    $env:USERPROFILE = Join-Path $Work "profile"; $env:HOME = $env:USERPROFILE
    New-Item -ItemType Directory -Path $env:USERPROFILE -Force | Out-Null
    [void](Invoke-Scr "discuss plan --print-only" @("discuss", "plan", "--print-only"))
} finally {
    $env:USERPROFILE = $realProfile; $env:HOME = $realHome
}
$ver = Invoke-Scr "version" @("version")

# 4. the report
$lines = @("# Rehearsal of $Tag", "", "- when: $(Get-Date -Format s)", "- live state (read only): ``$State``",
           "- release: ``$Release``", "- copy: ``$Copy``", "- result: **$(if ($Failed) { 'FAIL' } else { 'PASS' })**", "",
           "## Steps", "", "| step | rc | result |", "|---|---|---|")
foreach ($s in $Steps) { $lines += "| $($s.name) | $($s.rc) | $(if ($s.ok) { 'ok' } else { 'FAIL' }) |" }
$lines += @("", "## Migration diff", "", '```') + @($diff) + @('```', "", "## Output (last lines of each step)", "")
foreach ($s in $Steps) { $lines += @("### $($s.name)", '```') + @($s.out) + @('```', "") }
$lines += @("## Cut-over", "", "When this report is clean (gate G2), the cut-over is:", "", '```',
            "powershell -NoProfile -ExecutionPolicy Bypass -File tools\cutover.ps1 -Tag $Tag -State `"$State`" -Runtime `"$Runtime`"",
            '```', "", "The copy can be deleted afterwards: ``Remove-Item -Recurse -Force `"$Work`"``")
$report = Join-Path $Copy "reports\rehearsal-$Tag-$Stamp.md"
New-Item -ItemType Directory -Path (Split-Path $report -Parent) -Force | Out-Null
[IO.File]::WriteAllText($report, ($lines -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding $false))

Write-Host ""
Write-Host "report: $report"
if ($Failed) { Write-Host "FAIL: see the report"; exit 1 }
Write-Host "PASS: $Tag rehearsed cleanly against a copy of the state"
exit 0
