<#
一键出安装包。

    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -SkipInstaller

四步：装打包依赖 → 画图标 → PyInstaller → Inno Setup。
没有 Inno Setup 时会跳过最后一步，只留下可以直接双击运行的 exe 目录。
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,  # 只出 dist\NiceReadingLens\，不压安装包
    [switch]$SkipDeps        # 依赖已经装过就跳过联网
)

$ErrorActionPreference = "Stop"

$packaging = $PSScriptRoot
$root = Split-Path -Parent $packaging
$python = Join-Path $root ".venv\Scripts\python.exe"
$pyinstaller = Join-Path $root ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $python)) {
    throw "没找到虚拟环境：$python`n先执行：python -m venv .venv"
}

# ---- 1. 依赖 ----
if (-not $SkipDeps) {
    Write-Host "[1/4] 安装打包依赖" -ForegroundColor Cyan
    & $python -m pip install --disable-pip-version-check --upgrade pyinstaller cryptography
    if ($LASTEXITCODE -ne 0) { throw "pip install 失败" }
}

# ---- 2. 图标 ----
Write-Host "[2/4] 生成图标" -ForegroundColor Cyan
& $python (Join-Path $packaging "make_icon.py") (Join-Path $packaging "build")
if ($LASTEXITCODE -ne 0) { throw "生成图标失败" }

# ---- 3. PyInstaller ----
Write-Host "[3/4] PyInstaller 打包" -ForegroundColor Cyan
Push-Location $root
try {
    & $pyinstaller (Join-Path $packaging "NiceReadingLens.spec") `
        --noconfirm --clean `
        --distpath (Join-Path $root "dist") `
        --workpath (Join-Path $root "build")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }
}
finally {
    Pop-Location
}

$appDir = Join-Path $root "dist\NiceReadingLens"
$exe = Join-Path $appDir "NiceReadingLens.exe"
if (-not (Test-Path $exe)) { throw "没生成 exe，往上翻看看报错" }
$sizeMb = [math]::Round((Get-ChildItem $appDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "      -> $appDir  ($sizeMb MB)" -ForegroundColor Green

if ($SkipInstaller) {
    Write-Host "`n只做了 exe 目录，没压安装包（-SkipInstaller）。" -ForegroundColor Yellow
    return
}

# ---- 4. 安装包 ----
Write-Host "[4/4] 编译安装包" -ForegroundColor Cyan
$candidates = @()
if (${env:ProgramFiles(x86)}) { $candidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
if ($env:ProgramFiles) { $candidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe" }
$iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    $found = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($found) { $iscc = $found.Source }
}

if (-not $iscc) {
    Write-Warning "没找到 Inno Setup，跳过安装包。装一个即可：winget install JRSoftware.InnoSetup"
    Write-Host "    exe 目录已经能用，直接双击：$exe" -ForegroundColor Yellow
    return
}

& $iscc (Join-Path $packaging "installer.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC 编译失败" }

$setup = Get-ChildItem (Join-Path $root "dist\installer") -Filter "*.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1

Write-Host "`n完成：" -ForegroundColor Green
Write-Host "  绿色目录  $appDir"
if ($setup) {
    $setupMb = [math]::Round($setup.Length / 1MB, 1)
    Write-Host "  安装包    $($setup.FullName)  ($setupMb MB)"
}
