<#
.SYNOPSIS
    Build the portable, no-install GammaNederwiek folder and (optionally) sign it.

.DESCRIPTION
    One command per repack:

        .\packaging\build_portable.ps1 -Version 0.4C.18

    The script is the release gate, so it runs in this order:

      1. preflight   - Python 3.12, uv, signtool.exe, signing certificate
      2. build env   - reuse .venv-build (pinned PySide6 + PyInstaller)
      3. test gate   - full unittest suite; a failure aborts the build
      4. metadata    - VERSION file + build_info.json (git SHA, dirty flag)
      5. freeze      - PyInstaller onedir into a wiped dist\
      6. selfcheck   - run the built exe with --selfcheck before signing
      7. sign        - signtool + Get-AuthenticodeSignature must report Valid
      8. package     - README-FIRST.txt, SHA256SUMS.txt, versioned .zip

.PARAMETER Version
    Release label, e.g. 0.4C.18. Defaults to the contents of VERSION.

.PARAMETER Fast
    Iteration build: no clean, no signing, no zip, output in dist\dev.
    Anything it produces is unsigned and must not be shipped.

.PARAMETER SkipTests
    Emergency hotfix only. Skips the unittest gate and says so loudly.

.PARAMETER NoSign
    Produce an unsigned release zip (name gets an -unsigned suffix) for local
    testing only.

.PARAMETER Thumbprint
    Certificate thumbprint when more than one code-signing cert is installed.
    Falls back to $env:GNI_CERT_THUMBPRINT.

.EXAMPLE
    .\packaging\build_portable.ps1 -Version 0.4C.17

.EXAMPLE
    .\packaging\build_portable.ps1 -Fast
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$Fast,
    [switch]$SkipTests,
    [switch]$NoSign,
    [string]$Thumbprint = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BuildVenv = Join-Path $RepoRoot ".venv-build"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
$DistRoot = Join-Path $RepoRoot "dist"
$WorkRoot = Join-Path $RepoRoot "build\pyinstaller"
$ReleaseRoot = Join-Path $RepoRoot "release"
$SpecPath = Join-Path $PSScriptRoot "GammaNederwiek.spec"
$VersionInfoPath = Join-Path $PSScriptRoot "version_info.txt"
$ReadmeSource = Join-Path $PSScriptRoot "README-FIRST.txt"

$PinnedPySide = "PySide6==6.12.0"
$PinnedPyInstaller = "pyinstaller>=6.11,<7"

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Text)
    Write-Host "    $Text" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Text)
    Write-Host "    $Text" -ForegroundColor Yellow
}

function Fail {
    param([string]$Text)
    Write-Host ""
    Write-Host "BUILD FAILED: $Text" -ForegroundColor Red
    exit 1
}

function Find-Uv {
    $command = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $fallback = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $fallback) { return $fallback }
    return $null
}

function Find-SignTool {
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $kits = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
        "$env:ProgramFiles\Windows Kits\10\bin"
    )
    foreach ($kit in $kits) {
        if (-not (Test-Path $kit)) { continue }
        $candidate = Get-ChildItem -Path $kit -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\signtool\.exe$' } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) { return $candidate.FullName }
    }
    return $null
}

function Get-Sha256 {
    # Deliberately not Get-FileHash: a domain PC can have a broken PSModulePath
    # (Windows PowerShell 5.1 picking up the PowerShell 7 module tree), where
    # Microsoft.PowerShell.Utility does not auto-load and the cmdlet is missing.
    param([string]$Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $bytes = $sha.ComputeHash($stream)
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
    return (([System.BitConverter]::ToString($bytes)) -replace '-', '').ToLowerInvariant()
}

function Get-NumericVersion {
    param([string]$Label)
    $numbers = New-Object System.Collections.Generic.List[int]
    foreach ($match in [regex]::Matches($Label, '\d+')) {
        $numbers.Add([int]$match.Value)
    }
    while ($numbers.Count -lt 4) { $numbers.Add(0) }
    return ($numbers.GetRange(0, 4))
}

# ---------------------------------------------------------------- 1. preflight
Write-Step "Preflight"

if (-not $Version) {
    $versionFile = Join-Path $RepoRoot "VERSION"
    if (-not (Test-Path $versionFile)) { Fail "VERSION file not found and -Version was not supplied." }
    $Version = (Get-Content $versionFile -Raw).Trim()
}
if (-not $Version) { Fail "Version label is empty." }
Write-Ok "version: $Version"

$uv = Find-Uv
if (-not $uv) { Fail "uv.exe not found. Install it (see run_app.bat) or put it on PATH." }
Write-Ok "uv: $uv"

if (-not $Fast) {
    if (-not $NoSign) {
        $signTool = Find-SignTool
        if (-not $signTool) {
            Fail "signtool.exe not found (Windows SDK). Use -NoSign for a local test build."
        }
        Write-Ok "signtool: $signTool"
        if (-not $Thumbprint) { $Thumbprint = $env:GNI_CERT_THUMBPRINT }
    }
    if (-not (Test-Path $SpecPath)) { Fail "Spec file missing: $SpecPath" }
}

# ---------------------------------------------------------------- 2. build env
Write-Step "Build environment"

if (-not (Test-Path $BuildPython)) {
    if ($Fast) { Fail ".venv-build is missing; run the script once without -Fast to create it." }
    Write-Ok "creating .venv-build (one time, downloads PySide6)"
    & $uv venv $BuildVenv --python 3.12
    if ($LASTEXITCODE -ne 0) { Fail "uv venv failed." }
}

$needInstall = $false
if (-not $Fast) { $needInstall = $true } else {
    & $BuildPython -c "import PySide6, PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) { $needInstall = $true }
}

if ($needInstall) {
    Write-Ok "installing pinned build dependencies"
    & $uv pip install --python $BuildPython $PinnedPySide $PinnedPyInstaller
    if ($LASTEXITCODE -ne 0) { Fail "Dependency installation failed." }
} else {
    Write-Ok "reusing .venv-build"
}

# ---------------------------------------------------------------- 3. test gate
Write-Step "Test gate"

if ($SkipTests) {
    Write-Warn "SKIPPING TESTS (-SkipTests). This build is not release quality."
} else {
    Push-Location $RepoRoot
    try {
        & $BuildPython -m unittest discover -t . -s tests
        if ($LASTEXITCODE -ne 0) { Fail "Unit tests failed - refusing to package." }
    } finally {
        Pop-Location
    }
    Write-Ok "unit tests passed"
}

# ---------------------------------------------------------------- 4. metadata
Write-Step "Build metadata"

$numeric = Get-NumericVersion $Version
$fileVersion = "$($numeric[0]), $($numeric[1]), $($numeric[2]), $($numeric[3])"

$versionTemplate = @'
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(__FILEVERS__),
    prodvers=(__FILEVERS__),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Seatrium'),
        StringStruct('FileDescription', 'Gamma to Nederwiek revision migration'),
        StringStruct('FileVersion', '__VERSION__'),
        StringStruct('InternalName', 'GammaNederwiek'),
        StringStruct('LegalCopyright', 'Seatrium'),
        StringStruct('OriginalFilename', 'GammaNederwiek.exe'),
        StringStruct('ProductName', 'Gamma to Nederwiek'),
        StringStruct('ProductVersion', '__VERSION__')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
'@
$versionInfo = $versionTemplate.Replace("__FILEVERS__", $fileVersion).Replace("__VERSION__", $Version)
Set-Content -Path $VersionInfoPath -Value $versionInfo -Encoding UTF8
Write-Ok "version resource: $VersionInfoPath"

$gitCommit = "unknown"
$gitDirty = "unknown"
Push-Location $RepoRoot
try {
    $gitCommit = (& git rev-parse --short HEAD 2>$null)
    if (-not $gitCommit) { $gitCommit = "unknown" }
    $status = (& git status --porcelain 2>$null)
    $gitDirty = if ([string]::IsNullOrWhiteSpace($status)) { "false" } else { "true" }
} catch {
    Write-Warn "git metadata unavailable"
} finally {
    Pop-Location
}
if ($gitDirty -eq "true") {
    Write-Warn "working tree has uncommitted changes - the build is not reproducible from a commit"
}

# ---------------------------------------------------------------- 5. freeze
Write-Step "Freezing application"

if (-not $Fast) {
    if (Test-Path $DistRoot) { Remove-Item $DistRoot -Recurse -Force }
}
$distPath = if ($Fast) { Join-Path $DistRoot "dev" } else { $DistRoot }
New-Item -ItemType Directory -Path $distPath -Force | Out-Null

$pyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--distpath", $distPath,
    "--workpath", $WorkRoot,
    $SpecPath
)
if (-not $Fast) { $pyInstallerArgs = @("-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", $distPath, "--workpath", $WorkRoot, $SpecPath) }

Push-Location $RepoRoot
try {
    & $BuildPython @pyInstallerArgs
    if ($LASTEXITCODE -ne 0) { Fail "PyInstaller failed." }
} finally {
    Pop-Location
}

$AppDir = Join-Path $distPath "GammaNederwiek"
$ExePath = Join-Path $AppDir "GammaNederwiek.exe"
if (-not (Test-Path $ExePath)) { Fail "Expected executable not produced: $ExePath" }

$sizeMb = [math]::Round(((Get-ChildItem $AppDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Ok "bundle: $AppDir ($sizeMb MB)"

# ---------------------------------------------------------------- 6. selfcheck
Write-Step "Bundle selfcheck"

$selfcheck = Start-Process -FilePath $ExePath -ArgumentList "--selfcheck" -Wait -PassThru -WindowStyle Hidden
if ($selfcheck.ExitCode -ne 0) {
    Fail "Bundled executable failed --selfcheck (exit $($selfcheck.ExitCode)). See logs\app.log next to the build output."
}
Write-Ok "selfcheck passed (fonts, lisp, resources, revision settings)"

# ---------------------------------------------------------------- 7. package+sign
Write-Step "Packaging"

Copy-Item $ReadmeSource (Join-Path $AppDir "README-FIRST.txt") -Force
Write-Ok "README-FIRST.txt copied"

$buildInfo = [ordered]@{
    version    = $Version
    git_commit = $gitCommit
    git_dirty  = $gitDirty
    built_at   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    built_by   = $env:USERNAME
    signed     = (-not $NoSign -and -not $Fast)
}
$buildInfo | ConvertTo-Json | Set-Content -Path (Join-Path $AppDir "build_info.json") -Encoding UTF8
Write-Ok "build_info.json written"

$signed = $false
if (-not $Fast -and -not $NoSign) {
    $signArgs = @("sign", "/fd", "SHA256", "/tr", "http://timestamp.digicert.com", "/td", "SHA256")
    if ($Thumbprint) { $signArgs += @("/sha1", $Thumbprint) } else { $signArgs += "/a" }
    $signArgs += $ExePath
    & $signTool @signArgs
    if ($LASTEXITCODE -ne 0) { Fail "signtool sign failed. Check the certificate and the timestamp server." }

    # `signtool verify` instead of Get-AuthenticodeSignature: the cmdlet lives in
    # Microsoft.PowerShell.Security, which a broken PSModulePath can hide.
    & $signTool verify /pa /v $ExePath
    if ($LASTEXITCODE -ne 0) { Fail "signtool verify failed - the build cannot be shipped." }
    $signed = $true
    Write-Ok "signature verified (publisher rule can be requested from IT)"
} elseif (-not $Fast) {
    Write-Warn "UNSIGNED build (-NoSign) - do not distribute"
}

$exeHash = Get-Sha256 $ExePath
$sumsPath = Join-Path $AppDir "SHA256SUMS.txt"
Set-Content -Path $sumsPath -Value "$exeHash  GammaNederwiek.exe" -Encoding ASCII
Write-Ok "sha256: $exeHash"

if ($Fast) {
    Write-Host ""
    Write-Host "Fast build ready: $AppDir" -ForegroundColor Yellow
    Write-Host "Unsigned and unzipped - for a local smoke test only." -ForegroundColor Yellow
    exit 0
}

Write-Step "Creating release archive"

Add-Type -AssemblyName System.IO.Compression.FileSystem
$suffix = if ($signed) { "portable" } else { "portable-unsigned" }
$zipName = "GammaNederwiek-$Version-$suffix.zip"
$zipDir = Join-Path $ReleaseRoot $Version
New-Item -ItemType Directory -Path $zipDir -Force | Out-Null
$zipPath = Join-Path $zipDir $zipName
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $AppDir,
    $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $true
)
$zipMb = [math]::Round(((Get-Item $zipPath).Length / 1MB), 1)
$zipHash = Get-Sha256 $zipPath
Write-Ok "archive: $zipPath ($zipMb MB)"

Write-Step "Summary"
Write-Host "    version   : $Version"
Write-Host "    git commit: $gitCommit (dirty=$gitDirty)"
Write-Host "    signature : $(if ($signed) { 'valid' } else { 'UNSIGNED' })"
Write-Host "    exe sha256: $exeHash"
Write-Host "    zip sha256: $zipHash"
Write-Host ""
Write-Host "Send IT the publisher name (AppLocker publisher rule) once - not the hash," -ForegroundColor Cyan
Write-Host "which changes on every repack. Distribute the zip from an internal share." -ForegroundColor Cyan
