# =============================================================================
# Build staging resources for the Windows installer.
#
# Produces: build/installer/resources/
#   runtime/python/            portable CPython 3.11 + MediaCrawler dependencies
#   sidecar/                   FastAPI sidecar (runtime files only)
#   engine/                    MediaCrawler engine wrapper
#   MediaCrawler/              MediaCrawler source (no venv/user data/browser data)
#   app/report_pipeline/       local report statistics/template pipeline (→ resources/report_pipeline)
#   LICENSES/                  third-party license texts
#   build-info.json
#
# Usage:
#   pwsh -NoProfile -File app/scripts/build_pack_resources.ps1
#   pwsh -NoProfile -File app/scripts/build_pack_resources.ps1 -Clean
#   pwsh -NoProfile -File app/scripts/build_pack_resources.ps1 -SkipRuntime
# =============================================================================
param(
    [string]$OutDir = '',
    [string]$BasePython = '',
    [string]$VenvDir = '',
    [switch]$Clean,
    [switch]$SkipRuntime
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = (Resolve-Path (Join-Path $Here '..\..')).Path
if (-not $OutDir) { $OutDir = Join-Path $Repo 'build\installer\resources' }
$OutDir = [IO.Path]::GetFullPath($OutDir)
if (-not $BasePython) { $BasePython = Join-Path $env:APPDATA 'uv\python\cpython-3.11-windows-x86_64-none' }
if (-not $VenvDir) { $VenvDir = Join-Path $Repo 'MediaCrawler\.venv' }

function Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Note([string]$Text) { Write-Host "    $Text" -ForegroundColor DarkGray }
function Fail([string]$Text) { throw $Text }

function RoboCopyChecked([string]$Source, [string]$Target, [string[]]$Extra = @()) {
    if (-not (Test-Path $Source)) { Fail "source not found: $Source" }
    $args = @($Source, $Target, '/E', '/R:1', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP') + $Extra
    & robocopy.exe @args *> $null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy failed ($LASTEXITCODE): $Source -> $Target" }
}

if ($Clean -and (Test-Path $OutDir)) {
    Step "cleaning $OutDir"
    Remove-Item -LiteralPath $OutDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $OutDir 'runtime') | Out-Null

if (-not $SkipRuntime) {
    $pyOut = Join-Path $OutDir 'runtime\python'
    Step "portable Python: $BasePython -> $pyOut"
    if (-not (Test-Path (Join-Path $BasePython 'python.exe'))) { Fail "base python not found: $BasePython" }
    if ($Clean -and (Test-Path $pyOut)) { Remove-Item -LiteralPath $pyOut -Recurse -Force }
    RoboCopyChecked $BasePython $pyOut

    $venvSite = Join-Path $VenvDir 'Lib\site-packages'
    if (-not (Test-Path $venvSite)) { Fail "venv site-packages not found: $venvSite" }
    Step 'overlaying venv site-packages into portable Python'
    RoboCopyChecked $venvSite (Join-Path $pyOut 'Lib\site-packages')

    # venv-only DLLs that live next to python.exe must be available too.
    $venvDlls = Get-ChildItem (Join-Path $VenvDir 'Scripts') -File -Filter '*.dll' -ErrorAction SilentlyContinue
    foreach ($dll in $venvDlls) { Copy-Item $dll.FullName (Join-Path $pyOut $dll.Name) -Force }

    Step 'checking portable Python imports'
    $probe = 'import fastapi, uvicorn, httpx, pandas, openpyxl, sqlalchemy, playwright, cv2, jieba, PIL; print("runtime imports ok")'
    $env:PYTHONNOUSERSITE = '1'
    $out = & (Join-Path $pyOut 'python.exe') -c $probe 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "portable Python import check failed: $out" }
    Remove-Item Env:PYTHONNOUSERSITE -ErrorAction SilentlyContinue
    Write-Host "    $out"
} else {
    Note 'SkipRuntime: keeping existing runtime/python'
}

Step 'copying portable Node.js (execjs / Zhihu sign)'
$nodeExe = (Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $nodeExe -or -not (Test-Path $nodeExe)) {
    Fail 'node.exe not found in PATH; install Node.js on build machine before packaging'
}
$nodeOut = Join-Path $OutDir 'runtime\node'
New-Item -ItemType Directory -Force -Path $nodeOut | Out-Null
Copy-Item $nodeExe (Join-Path $nodeOut 'node.exe') -Force
(& $nodeExe --version) | ForEach-Object { Note "bundled node: $_" }

Step 'copying sidecar runtime files'
$sidecarOut = Join-Path $OutDir 'sidecar'
if (Test-Path $sidecarOut) { Remove-Item -LiteralPath $sidecarOut -Recurse -Force }
RoboCopyChecked (Join-Path $Repo 'app\sidecar') $sidecarOut @('/XD', '__pycache__', '_fixtures', '/XF', 'selftest.py', 'test_*.py', '_fake_*')

Step 'copying engine files'
$engineOut = Join-Path $OutDir 'engine'
if (Test-Path $engineOut) { Remove-Item -LiteralPath $engineOut -Recurse -Force }
RoboCopyChecked (Join-Path $Repo 'app\engine') $engineOut @('/XD', '__pycache__', '/XF', 'test_engine.py', '_probe_xhs_login.py')

Step 'copying MediaCrawler source (no .venv / browser data / docs / webui / verification)'
$mcOut = Join-Path $OutDir 'MediaCrawler'
if (Test-Path $mcOut) { Remove-Item -LiteralPath $mcOut -Recurse -Force }
RoboCopyChecked (Join-Path $Repo 'MediaCrawler') $mcOut @(
    '/XD', '.venv', 'browser_data', 'verification', 'webui', 'docs', '.git', '__pycache__', '.idea', 'data',
    '/XF', 'sqlite_media_crawler.db', '*.pyc'
)

Step 'copying report pipeline (app/report_pipeline: local report statistics/template)'
$reportPipelineOut = Join-Path $OutDir 'report_pipeline'
if (Test-Path -LiteralPath $reportPipelineOut) { Remove-Item -LiteralPath $reportPipelineOut -Recurse -Force }
if (Test-Path -LiteralPath (Join-Path $Repo 'app\report_pipeline')) {
    RoboCopyChecked (Join-Path $Repo 'app\report_pipeline') $reportPipelineOut @('/XD', '__pycache__', 'out', '/XF', '*.pyc')
} else {
    Note 'app/report_pipeline not found; skipped (report statistics/local template pipeline unavailable)'
}

Step 'copying licenses'
$licOut = Join-Path $OutDir 'LICENSES'
New-Item -ItemType Directory -Force -Path $licOut | Out-Null
Copy-Item (Join-Path $Repo 'MediaCrawler\LICENSE') (Join-Path $licOut 'MediaCrawler-LICENSE.txt') -Force
if (Test-Path (Join-Path $BasePython 'LICENSE.txt')) {
    Copy-Item (Join-Path $BasePython 'LICENSE.txt') (Join-Path $licOut 'Python-LICENSE.txt') -Force
}

$info = [ordered]@{
    built_at = (Get-Date).ToString('o')
    python = (& (Join-Path $OutDir 'runtime\python\python.exe') --version 2>&1)
    electron = (Get-Content (Join-Path $Repo 'app\desktop\package.json') -Raw | ConvertFrom-Json).devDependencies.electron
    size_mb = [Math]::Round(((Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
    engine_sha = (Get-FileHash (Join-Path $OutDir 'engine\entry.py') -Algorithm SHA256).Hash
}
$info | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 (Join-Path $OutDir 'build-info.json')

Step 'staging ready'
Note ("output: {0}" -f $OutDir)
Note ("size:   {0} MB" -f $info.size_mb)
Get-ChildItem $OutDir | Select-Object Name, Mode | Format-Table -AutoSize | Out-String -Width 80 | Write-Host
