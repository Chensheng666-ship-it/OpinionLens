# One-click dev launcher: Electron desktop app (auto-starts the Python sidecar).
# Works with both Windows PowerShell 5.1 and PowerShell 7+ (ASCII-only on purpose).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot          # app/
$project = Split-Path -Parent $root               # project root
$desktop = Join-Path $root 'desktop'
$python = Join-Path $project 'MediaCrawler\.venv\Scripts\python.exe'
$electron = Join-Path $desktop 'node_modules\.bin\electron.cmd'

if (-not (Test-Path $python)) { throw "Python not found: $python" }
if (-not (Test-Path $electron)) { throw "Electron not found. Run 'npm install' in app\desktop first." }

$env:MC_PYTHON = $python
$env:MC_ROOT = Join-Path $project 'MediaCrawler'
$env:PYTHONIOENCODING = 'utf-8'

# Some terminals inject ELECTRON_RUN_AS_NODE=1 which makes Electron run as plain Node.
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue

# Compatibility mode: some corporate PCs / RDP sessions / EDR setups block Chromium
# child-process sandbox (error "GPU process launch failed: error_code=18" -> crash).
# Start in compat mode (--no-sandbox + hardware acceleration off).
# Set FS_ELECTRON_SANDBOX=1 to force the strict sandbox behavior.
if ($env:FS_ELECTRON_SANDBOX -ne '1') {
    $env:FS_ELECTRON_COMPAT = '1'
    Write-Host "(compat mode: Chromium sandbox / hardware acceleration disabled; set FS_ELECTRON_SANDBOX=1 to force strict sandbox)" -ForegroundColor DarkGray
}

Write-Host "Starting desktop app..." -ForegroundColor Cyan
Push-Location $desktop
try {
    & $electron . @args
    $electronExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($electronExitCode -ne $null -and $electronExitCode -ne 0) {
    Write-Host "Electron exited with code $electronExitCode. See app\data\logs\main.log" -ForegroundColor Red
    exit $electronExitCode
}
