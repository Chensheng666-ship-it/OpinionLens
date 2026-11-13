# Start only the Python sidecar for debugging (fixed token = devtoken).
# ASCII-only for compatibility with Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$project = Split-Path -Parent $root
$python = Join-Path $project 'MediaCrawler\.venv\Scripts\python.exe'
$dataDir = Join-Path $root 'data'
$env:MC_ROOT = Join-Path $project 'MediaCrawler'
$env:PYTHONIOENCODING = 'utf-8'

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

Write-Host "Starting sidecar (token=devtoken) ..." -ForegroundColor Cyan
& $python (Join-Path $root 'sidecar\main.py') --port 0 --token devtoken --data-dir $dataDir
