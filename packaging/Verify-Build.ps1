<#
.SYNOPSIS
    Check that this portable copy is complete, unblocked and able to run.

.DESCRIPTION
    Run this from the folder that contains GammaNederwiek.exe when the app does
    not start, or before handing the folder to someone else.

    It answers the three questions that cause every "it will not start" report:

      1. Is the copy complete?     (executable hash, _internal, fonts, lisp)
      2. Is Windows blocking it?   (Mark-of-the-Web, Smart App Control, AppLocker)
      3. Can it write its state?   (config\ and logs\ next to the exe)

    Deliberately avoids Get-FileHash and Get-AuthenticodeSignature: a domain PC
    can have a PSModulePath that points Windows PowerShell 5.1 at the PowerShell
    7 module tree, where those cmdlets do not auto-load.

.PARAMETER Path
    Folder to check. Defaults to the folder holding this script.

.PARAMETER Fix
    Remove the Mark-of-the-Web from every file (the common cause of
    "SmartScreen prevented an unrecognized app from starting").

.EXAMPLE
    .\Verify-Build.ps1

.EXAMPLE
    .\Verify-Build.ps1 -Fix
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Path = "",
    [switch]$Fix
)

$ErrorActionPreference = "Continue"

$problems = New-Object System.Collections.Generic.List[string]
$warnings = New-Object System.Collections.Generic.List[string]

function Write-Head {
    param([string]$Text)
    Write-Host ""
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ("-" * $Text.Length) -ForegroundColor DarkGray
}

function Write-Pass {
    param([string]$Text)
    Write-Host "  [ OK ] $Text" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Text)
    Write-Host "  [WARN] $Text" -ForegroundColor Yellow
    $warnings.Add($Text)
}

function Write-Fail {
    param([string]$Text)
    Write-Host "  [FAIL] $Text" -ForegroundColor Red
    $problems.Add($Text)
}

function Write-Info {
    param([string]$Text)
    Write-Host "  [info] $Text" -ForegroundColor Gray
}

function Get-Sha256 {
    param([string]$FilePath)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($FilePath)
    try {
        $bytes = $sha.ComputeHash($stream)
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
    return (([System.BitConverter]::ToString($bytes)) -replace '-', '').ToLowerInvariant()
}

function Get-MotwFiles {
    param([string]$Folder)
    $blocked = New-Object System.Collections.Generic.List[string]
    foreach ($file in Get-ChildItem -Path $Folder -Recurse -File -ErrorAction SilentlyContinue) {
        try {
            $stream = Get-Item -Path $file.FullName -Stream Zone.Identifier -ErrorAction Stop
            if ($stream) { $blocked.Add($file.FullName) }
        } catch {
            # no alternate data stream: the file is not marked
        }
    }
    return $blocked
}

if (-not $Path) { $Path = $PSScriptRoot }
if (-not (Test-Path $Path)) {
    Write-Host "Folder not found: $Path" -ForegroundColor Red
    exit 1
}
$Path = (Resolve-Path $Path).Path
$exePath = Join-Path $Path "GammaNederwiek.exe"
$internal = Join-Path $Path "_internal"

Write-Host ""
Write-Host "GammaNederwiek - portable copy check" -ForegroundColor White
Write-Host "Folder: $Path"

# ---------------------------------------------------------------- 1. completeness
Write-Head "1. Copy completeness"

if (-not (Test-Path $exePath)) {
    Write-Fail "GammaNederwiek.exe is missing - the copy is incomplete or antivirus removed it."
} else {
    $exeSize = [math]::Round(((Get-Item $exePath).Length / 1MB), 2)
    Write-Pass "GammaNederwiek.exe present ($exeSize MB)"
}

$required = @(
    "_internal\python312.dll",
    "_internal\resources\fonts\OFL.txt",
    "_internal\revision\pdf_settings.json",
    "_internal\revision\writer_settings.json",
    "README-FIRST.txt"
)
foreach ($relative in $required) {
    $full = Join-Path $Path $relative
    if (Test-Path $full) { Write-Pass "found $relative" } else { Write-Fail "missing $relative - copy the whole folder, not just the .exe" }
}

if (Test-Path $internal) {
    $lispCount = (Get-ChildItem (Join-Path $internal "lisp") -Filter *.lsp -ErrorAction SilentlyContinue | Measure-Object).Count
    if ($lispCount -ge 4) { Write-Pass "lisp scripts: $lispCount" } else { Write-Fail "only $lispCount LISP script(s) - the bundle is incomplete" }

    $fontCount = (Get-ChildItem (Join-Path $internal "resources\fonts") -Filter *.ttf -ErrorAction SilentlyContinue | Measure-Object).Count
    if ($fontCount -ge 5) { Write-Pass "bundled fonts: $fontCount" } else { Write-Warn "bundled fonts: $fontCount (expected 5) - headings fall back to Segoe UI" }

    $fileCount = (Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
    Write-Info "files in folder: $fileCount"
} else {
    Write-Fail "_internal folder is missing - this is not a valid portable copy"
}

$sumsPath = Join-Path $Path "SHA256SUMS.txt"
if ((Test-Path $exePath) -and (Test-Path $sumsPath)) {
    $expected = ((Get-Content $sumsPath -TotalCount 1) -split '\s+')[0]
    $actual = Get-Sha256 $exePath
    if ($expected -and ($expected -eq $actual)) {
        Write-Pass "executable hash matches SHA256SUMS.txt"
    } else {
        Write-Fail "executable hash does not match SHA256SUMS.txt (expected $expected, got $actual)"
        Write-Info "a mismatch means a partial copy, a different build, or antivirus modification"
    }
    Write-Info "exe sha256: $actual"
} elseif (Test-Path $exePath) {
    Write-Warn "SHA256SUMS.txt missing - cannot prove the copy is intact"
}

# ---------------------------------------------------------------- 2. blocking
Write-Head "2. Windows blocking"

if (Test-Path $exePath) {
    $blocked = Get-MotwFiles $Path
    if ($blocked.Count -eq 0) {
        Write-Pass "no Mark-of-the-Web on any file"
    } elseif ($Fix) {
        Write-Info "$($blocked.Count) file(s) carry Mark-of-the-Web - removing it now"
        foreach ($file in $blocked) { Unblock-File -Path $file -ErrorAction SilentlyContinue }
        $remaining = Get-MotwFiles $Path
        if ($remaining.Count -eq 0) {
            # No warning is recorded: this run fixed the block.
            Write-Pass "Mark-of-the-Web removed from every file"
        } else {
            Write-Fail "$($remaining.Count) file(s) are still blocked after -Fix (permissions?)"
        }
    } else {
        Write-Warn "$($blocked.Count) file(s) carry Mark-of-the-Web (came from a zip/download)"
        Write-Info ("first: " + $blocked[0])
        Write-Info "fix it with:  .\Verify-Build.ps1 -Fix"
    }
}

# Smart App Control silently blocks unsigned executables and offers no bypass.
$sacState = $null
try {
    $ciPolicy = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState -ErrorAction Stop
    $sacState = $ciPolicy.VerifiedAndReputablePolicyState
} catch {
    $sacState = $null
}
switch ($sacState) {
    0 { Write-Pass "Smart App Control: off" }
    1 { Write-Fail "Smart App Control: ON - it blocks unsigned apps with no 'Run anyway'. Ask IT to allow the publisher, or run a signed build." }
    2 { Write-Warn "Smart App Control: evaluation mode (it can switch itself to ON later)" }
    default { Write-Info "Smart App Control: not applicable on this Windows build" }
}

try {
    $smartScreen = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer' -Name SmartScreenEnabled -ErrorAction Stop).SmartScreenEnabled
    if ($smartScreen -eq "requireadmin") {
        Write-Warn "SmartScreen policy: requireadmin - only an administrator may choose 'Run anyway'"
    } elseif ($smartScreen) {
        Write-Info "SmartScreen policy: $smartScreen"
    }
} catch {
    Write-Info "SmartScreen policy: not set"
}

try {
    $appLocker = Get-WinEvent -LogName "Microsoft-Windows-AppLocker/EXE and DLL" -MaxEvents 20 -ErrorAction Stop |
        Where-Object { $_.Message -like "*GammaNederwiek.exe*" } |
        Select-Object -First 1
    if ($appLocker) {
        Write-Fail "AppLocker blocked GammaNederwiek.exe at $($appLocker.TimeCreated) - IT must add an allow rule"
    } else {
        Write-Pass "no AppLocker block recorded for this executable"
    }
} catch {
    Write-Info "AppLocker log not readable (normal when AppLocker is not configured)"
}

# ---------------------------------------------------------------- 3. writability
Write-Head "3. Writable state folder"

$probe = Join-Path $Path ".verify_write_test"
try {
    Set-Content -Path $probe -Value "probe" -Encoding ASCII -ErrorAction Stop
    Remove-Item $probe -Force -ErrorAction Stop
    Write-Pass "folder is writable (settings and logs will live here)"
} catch {
    Write-Warn "folder is not writable - the app falls back to %LOCALAPPDATA%\GammaNederwiek"
}

if ($Path -like "\\*") {
    Write-Warn "running from a network path - copy the folder to a local disk first"
}

$logPath = Join-Path $Path "logs\app.log"
if (Test-Path $logPath) {
    $lastLine = Get-Content $logPath -Tail 1 -ErrorAction SilentlyContinue
    Write-Info "last log line: $lastLine"
    if ($lastLine -match "app_root=(.+?)\s") {
        $logged = $Matches[1]
        if ($logged -and -not ($logged -like "$Path*")) {
            Write-Info "this log came from another machine/folder - the app has not run here yet"
        }
    }
} else {
    Write-Info "no logs\app.log yet: the app has not started in this folder"
}

# ---------------------------------------------------------------- verdict
Write-Host ""
if ($problems.Count -eq 0) {
    if ($warnings.Count -eq 0) {
        Write-Host "RESULT: READY - this copy is complete and unblocked." -ForegroundColor Green
    } else {
        Write-Host "RESULT: RUNNABLE, but $($warnings.Count) warning(s) above are worth reading." -ForegroundColor Yellow
    }
    Write-Host ""
    exit 0
}

Write-Host "RESULT: $($problems.Count) blocking problem(s) found." -ForegroundColor Red
foreach ($problem in $problems) { Write-Host "  - $problem" -ForegroundColor Red }
Write-Host ""
Write-Host "If every file is present and unblocked but Windows still refuses to start the app," -ForegroundColor Gray
Write-Host "the remaining cause is policy (Smart App Control / AppLocker). Only a signed build" -ForegroundColor Gray
Write-Host "plus an IT publisher rule fixes that; no local setting can bypass it." -ForegroundColor Gray
Write-Host ""
exit 1
