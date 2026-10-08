<#
Build the release: portable exe folder + Inno Setup installer.

    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipInstaller

Steps: deps -> icon -> PyInstaller -> Inno Setup. Without Inno Setup the last
step is skipped and you still get a double-clickable exe folder.
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,  # only build dist\NiceReadingLens\, skip the installer
    [switch]$SkipDeps        # build deps already installed, skip the network step
)

$ErrorActionPreference = "Stop"

$packaging = $PSScriptRoot
$root = Split-Path -Parent $packaging
$python = Join-Path $root ".venv\Scripts\python.exe"
$pyinstaller = Join-Path $root ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $python)) {
    throw "Virtualenv not found: $python`nRun this first: python -m venv .venv"
}

# ---- 1. deps ----
if (-not $SkipDeps) {
    Write-Host "[1/4] Installing build dependencies" -ForegroundColor Cyan
    & $python -m pip install --disable-pip-version-check --upgrade pyinstaller cryptography
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

# ---- 2. icon ----
Write-Host "[2/4] Generating icon" -ForegroundColor Cyan
& $python (Join-Path $packaging "make_icon.py") (Join-Path $packaging "build")
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

# ---- 3. PyInstaller ----
Write-Host "[3/4] Running PyInstaller" -ForegroundColor Cyan
Push-Location $root
try {
    & $pyinstaller (Join-Path $packaging "NiceReadingLens.spec") `
        --noconfirm --clean `
        --distpath (Join-Path $root "dist") `
        --workpath (Join-Path $root "build")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
}
finally {
    Pop-Location
}

$appDir = Join-Path $root "dist\NiceReadingLens"
$exe = Join-Path $appDir "NiceReadingLens.exe"
if (-not (Test-Path $exe)) { throw "No exe was produced - scroll up for the error" }
$sizeMb = [math]::Round((Get-ChildItem $appDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "      -> $appDir  ($sizeMb MB)" -ForegroundColor Green

if ($SkipInstaller) {
    Write-Host "`nDone: portable folder only (-SkipInstaller)." -ForegroundColor Yellow
    return
}

# ---- 4. installer ----
Write-Host "[4/4] Compiling the installer" -ForegroundColor Cyan
$candidates = @()
if (${env:ProgramFiles(x86)}) { $candidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
if ($env:ProgramFiles) { $candidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe" }
$iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    $found = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($found) { $iscc = $found.Source }
}

if (-not $iscc) {
    Write-Warning "Inno Setup not found, skipping the installer. Get it with: winget install JRSoftware.InnoSetup"
    Write-Host "    The portable folder is ready: $exe" -ForegroundColor Yellow
    return
}

& $iscc (Join-Path $packaging "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

$setup = Get-ChildItem (Join-Path $root "dist\installer") -Filter "*.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

Write-Host "`nDone:" -ForegroundColor Green
Write-Host "  portable   $appDir"
if ($setup) {
    $setupMb = [math]::Round($setup.Length / 1MB, 1)
    Write-Host "  installer  $($setup.FullName)  ($setupMb MB)"
}
