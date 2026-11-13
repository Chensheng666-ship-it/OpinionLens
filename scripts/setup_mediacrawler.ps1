# ============================================================================
# 安装 MediaCrawler 采集引擎（固定上游 commit，克隆到项目根目录 MediaCrawler/）
#
# 用法（在仓库根目录执行）：
#   .\scripts\setup_mediacrawler.ps1
#
# 前置：
#   1. git
#   2. uv（Python 包管理器，官方安装：https://docs.astral.sh/uv/getting-started/installation/
#      或 winget install --id astral-sh.uv -e）
#   3. Python >= 3.11（uv 会自动准备解释器）
#
# 说明：
#   - 本项目通过 app/engine/patches.py 在运行时 patch MediaCrawler，
#     不修改其源码，因此这里只需上游原版源码 + 依赖环境。
#   - 默认使用系统 Edge（channel=msedge），无需 playwright install chromium。
#   - 需要代理时，请先设置 HTTPS_PROXY / ALL_PROXY 环境变量。
#   - 目录固定为 <仓库根>\MediaCrawler：app/scripts/dev.ps1、app/scripts/run_sidecar.ps1、
#     app/desktop/main.js 都硬编码了 MediaCrawler\.venv\Scripts\python.exe（这个 venv
#     同时是 sidecar 的 Python 运行时），不要改名或挪位置（可用 MC_PYTHON / MC_ROOT 覆盖）。
#   - 上游 pyproject.toml 把默认索引钉死为清华源，实测部分 wheel 会返回 403；
#     因此 uv sync 失败时会自动依次改用 -FallbackIndexes 中的索引重试。
# ============================================================================
[CmdletBinding()]
param(
    [string]$Ref = 'b059657a872614bc0cb887af801b753ff4587d12',
    [string]$RepoUrl = 'https://github.com/NanmiCoder/MediaCrawler.git',
    [string[]]$FallbackIndexes = @(
        'https://mirrors.aliyun.com/pypi/simple',
        'https://pypi.org/simple'
    )
)

$ErrorActionPreference = 'Stop'

$project = Split-Path -Parent $PSScriptRoot
$target  = Join-Path $project 'MediaCrawler'

if (Test-Path $target) {
    throw "目录已存在：$target。如需重装，请先删除该目录（注意其中可能保存着登录态与采集数据）。"
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw '未找到 git，请先安装 Git for Windows。'
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw '未找到 uv。请安装：winget install --id astral-sh.uv -e（或参考 https://docs.astral.sh/uv/）'
}

Write-Host "[1/3] 克隆 MediaCrawler（commit $Ref）..." -ForegroundColor Cyan
git clone --no-checkout $RepoUrl $target
if ($LASTEXITCODE -ne 0) { throw 'git clone 失败' }

git -C $target checkout $Ref
if ($LASTEXITCODE -ne 0) { throw "git checkout $Ref 失败（上游可能已改写历史，请改用较新的 commit）" }

Write-Host '[2/3] uv sync 安装依赖（首次约 3-5 分钟）...' -ForegroundColor Cyan
Push-Location $target
$usedIndex = '(上游默认索引)'
try {
    $syncOk = $false
    uv sync
    if ($LASTEXITCODE -eq 0) {
        $syncOk = $true
    }
    else {
        # 上游默认索引 = 清华源，实测对部分 wheel 返回 403 Forbidden。
        foreach ($index in $FallbackIndexes) {
            Write-Host "  uv sync 失败（exit $LASTEXITCODE），改用索引重试：$index" -ForegroundColor Yellow
            uv sync --default-index $index
            if ($LASTEXITCODE -eq 0) { $syncOk = $true; $usedIndex = $index; break }
        }
    }
    if (-not $syncOk) {
        throw ("uv sync 失败（已尝试上游默认索引与备用索引：{0}）。" -f ($FallbackIndexes -join ', ')) +
              "请检查网络/代理，或手动执行：cd `"$target`"; uv sync --default-index <可用索引>"
    }
}
finally {
    Pop-Location
}

Write-Host '[3/3] 校验环境...' -ForegroundColor Cyan
$python = Join-Path $target '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "未生成虚拟环境：$python"
}
& $python -c "import playwright, httpx, pandas, openpyxl; print('MediaCrawler deps OK')"
if ($LASTEXITCODE -ne 0) { throw '依赖校验失败' }

Write-Host ''
Write-Host '完成。引擎已安装到：' -ForegroundColor Green
Write-Host "  $target"
Write-Host "Python 解释器：$python"
Write-Host "依赖索引：$usedIndex"
Write-Host ''
Write-Host '下一步：' -ForegroundColor Yellow
Write-Host '  cd app\desktop; npm install; cd ..\..'
Write-Host '  # 若 node_modules\electron\dist\electron.exe 不存在，补一次：'
Write-Host '  #   cd app\desktop; node .\node_modules\electron\install.js'
Write-Host '  .\app\scripts\dev.ps1'
