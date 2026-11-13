# Offline self-test: engine / exporter / sidecar / Electron screenshot.
# ASCII-only for compatibility with Windows PowerShell 5.1.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $root
$python = Join-Path $project 'MediaCrawler\.venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
$env:MC_ROOT = Join-Path $project 'MediaCrawler'
$failed = 0

function Step($name, $scriptBlock) {
    Write-Host "`n===== $name =====" -ForegroundColor Cyan
    & $scriptBlock
    if ($LASTEXITCODE -ne 0) { Write-Host "[FAIL] $name (exit $LASTEXITCODE)" -ForegroundColor Red; $script:failed++ }
    else { Write-Host "[PASS] $name" -ForegroundColor Green }
}

if (Test-Path (Join-Path $root 'engine\test_engine.py')) {
    Step "engine self-test" { & $python (Join-Path $root 'engine\test_engine.py') }
}
if (Test-Path (Join-Path $root 'sidecar\test_exporter.py')) {
    Step "exporter self-test" { & $python (Join-Path $root 'sidecar\test_exporter.py') }
}
# 更新逻辑（版本比较 / 安装包挑选）：纯 Node，无需 Electron 与网络
$updateTest = Join-Path $root 'scripts\test_update_logic.js'
if ((Test-Path $updateTest) -and (Get-Command node -ErrorAction SilentlyContinue)) {
    Step "update logic unit test (node)" { node $updateTest }
}
if (Test-Path (Join-Path $root 'sidecar\selftest.py')) {
    Step "sidecar self-test" { & $python (Join-Path $root 'sidecar\selftest.py') }
}

$electron = Join-Path $root 'desktop\node_modules\.bin\electron.cmd'
if (Test-Path $electron) {
    $shot = Join-Path $root 'data\logs\ui_selftest.png'
    New-Item -ItemType Directory -Force -Path (Split-Path $shot) | Out-Null
    Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
    Step "Electron UI screenshot" {
        Push-Location (Join-Path $root 'desktop')
        try { & $electron . --screenshot $shot } finally { Pop-Location }
    }
    if (Test-Path $shot) { Write-Host "screenshot: $shot" -ForegroundColor Yellow }
}

Write-Host ""
if ($failed -gt 0) { Write-Host "FAILED: $failed" -ForegroundColor Red; exit 1 }
Write-Host "ALL SELF-TESTS PASSED" -ForegroundColor Green
