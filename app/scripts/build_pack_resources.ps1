# =============================================================================
# Build staging resources for the Windows installer.
#
# Produces: build/installer/resources/
#   runtime/python/            portable CPython 3.11 + MediaCrawler dependencies
#                              (pruned: tests / dev tooling / caches / unused data)
#   runtime/node/              node.exe (execjs / Zhihu sign + Playwright driver)
#   sidecar/                   FastAPI sidecar (runtime files only)
#   engine/                    MediaCrawler engine wrapper
#   MediaCrawler/              MediaCrawler source (no venv/user data/browser data)
#   app/report_pipeline/       local report statistics/template pipeline (→ resources/report_pipeline)
#   LICENSES/                  third-party license texts
#   build-info.json
#   staging-manifest.json      剪裁记录（baseline / pruned[] / final）
#
# Usage:
#   pwsh -NoProfile -File app/scripts/build_pack_resources.ps1
#   pwsh -NoProfile -File app/scripts/build_pack_resources.ps1 -Clean
#   pwsh -NoProfile -File app/scripts/build_pack_resources.ps1 -SkipRuntime
#   pwsh -NoProfile -File app/scripts/build_pack_resources.ps1 -SkipPrune   # 回退：不剪裁
# =============================================================================
param(
    [string]$OutDir = '',
    [string]$BasePython = '',
    [string]$VenvDir = '',
    [switch]$Clean,
    [switch]$SkipRuntime,
    # 回退开关：保留 staging 全量（不删测试/文档/缓存/重复 node），用于排查
    # 「剪裁后某个能力不可用」这类问题。
    [switch]$SkipPrune
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
function Warn([string]$Text) { Write-Host "    [WARN] $Text" -ForegroundColor Yellow }
function Fail([string]$Text) { throw $Text }

function RoboCopyChecked([string]$Source, [string]$Target, [string[]]$Extra = @()) {
    if (-not (Test-Path $Source)) { Fail "source not found: $Source" }
    $args = @($Source, $Target, '/E', '/R:1', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS', '/NP') + $Extra
    & robocopy.exe @args *> $null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy failed ($LASTEXITCODE): $Source -> $Target" }
}

function Get-DirSizeMB([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return 0.0 }
    $sum = (Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if (-not $sum) { return 0.0 }
    return [math]::Round($sum / 1MB, 2)
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
} else {
    Note 'SkipRuntime: keeping existing runtime/python'
}

# 共用 Node 运行时：先就位，prune 才知道 Playwright driver 里那份 node.exe 可以删。
Step 'copying portable Node.js (execjs / Zhihu sign / Playwright driver)'
$nodeExe = (Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $nodeExe -or -not (Test-Path $nodeExe)) {
    Fail 'node.exe not found in PATH; install Node.js on build machine before packaging'
}
$nodeOut = Join-Path $OutDir 'runtime\node'
New-Item -ItemType Directory -Force -Path $nodeOut | Out-Null
Copy-Item $nodeExe (Join-Path $nodeOut 'node.exe') -Force
(& $nodeExe --version) | ForEach-Object { Note "bundled node: $_" }

# =============================================================================
# prune：剪裁 staging 里的运行时（2026-11-15 / M5b 安装包瘦身）
# -----------------------------------------------------------------------------
# 规则（只删确定运行时不需要的东西）：
#   * 缓存：__pycache__ / *.pyc / *.pyo（首次 import 会重新生成）
#   * 开发/测试工具链：pip / setuptools / virtualenv / distlib / pre_commit /
#     pytest / _pytest / pluggy / iniconfig / identify / nodeenv / cfgv
#     （打包运行时从不安装/编译/跑测试）
#   * **不删 __pycache__**（2026-11-15 决策）：删了会让新用户首次启动重新编译一遍，
#     sidecar 启动预算只有 25s（app/desktop/main.js: SIDECAR_READY_TIMEOUT_MS），
#     慢机器上会直接超时被 SIGKILL。字节码保留 = 首启速度与开发机一致。
#   * 各包的 tests/ 目录（**保留** numpy.testing / pandas.testing / sqlalchemy.testing，
#     它们是公共 API）
#   * 构建残留：包里的 *.c / *.h / *.pyx / *.pxd / *.pyi（Cython 生成源码、C 头文件、
#     类型存根，运行期一律不读；fontTools 的 .c 就占 ~7 MB）
#   * 数据/文档：matplotlib mpl-data/sample_data、jieba 的 LAC 预训练模型 / posseg / analyse
#     （本项目只用 jieba.cut/lcut/add_word）、opencv 的 Haar 级联数据、PIL 的 AVIF 编解码器
#   * 应用用不到的能力分支：mysql/postgres 驱动（引擎强制 sqlite）、mongo 栈、
#     alembic 迁移、cryptography、pygments、Crypto 自带自测向量
#   * 编译期产物：include/ 头文件、libs/ 静态库、tcl/ + tk DLL（本项目无 tkinter）
#   * 重复的 node.exe：Playwright driver 自带一份 node.exe(88 MB)，与
#     runtime/node/node.exe 完全可共用；剪掉后用 sitecustomize shim 把
#     PLAYWRIGHT_NODEJS_PATH 指到共用 node（见下方 $NodeShimSource）。
#
# 所有 LICENSE / COPYING / NOTICE 文件保持原状；被整体删除的包（pip 等）
# 不随包分发，因此其许可证不构成分发义务。
# =============================================================================
$script:pruned = New-Object System.Collections.Generic.List[object]

function Invoke-PrunePath {
    param([string]$Glob, [string]$Why)
    $targets = @(Get-ChildItem -Path (Join-Path $OutDir $Glob) -Force -ErrorAction SilentlyContinue)
    if ($targets.Count -eq 0) { return }
    $mb = 0.0
    foreach ($t in $targets) {
        if ($t.PSIsContainer) { $mb += Get-DirSizeMB $t.FullName } else { $mb += ($t.Length / 1MB) }
    }
    foreach ($t in $targets) { Remove-Item -LiteralPath $t.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    $mb = [math]::Round($mb, 2)
    $script:pruned.Add([ordered]@{ pattern = $Glob; removed_mb = $mb; items = $targets.Count; note = $Why })
    Write-Host ("    -{0,7:N2} MB  {1}" -f $mb, $Glob) -ForegroundColor DarkGray
}

function Invoke-PruneCollection {
    param([object[]]$Targets, [string]$Label, [string]$Why)
    if (-not $Targets -or $Targets.Count -eq 0) { return }
    $mb = 0.0
    foreach ($t in $Targets) {
        if ($t.PSIsContainer) { $mb += Get-DirSizeMB $t.FullName } else { $mb += ($t.Length / 1MB) }
    }
    $n = $Targets.Count
    foreach ($t in $Targets) { Remove-Item -LiteralPath $t.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    $mb = [math]::Round($mb, 2)
    $script:pruned.Add([ordered]@{ pattern = $Label; removed_mb = $mb; items = $n; note = $Why })
    Write-Host ("    -{0,7:N2} MB  {1}（{2} 项）" -f $mb, $Label, $n) -ForegroundColor DarkGray
}

$stagingMBBeforePrune = Get-DirSizeMB $OutDir

if ($SkipPrune) {
    Step 'prune：已跳过（-SkipPrune，保留 staging 全量）'
    Note ("staging 体积：{0} MB（未剪裁）" -f $stagingMBBeforePrune)
} elseif ($SkipRuntime) {
    Step 'prune：已跳过（-SkipRuntime：runtime 未重建，不重复剪裁）'
} else {
    Step 'prune：剪裁 staging 运行时（测试 / 构建残留 / 开发工具 / 重复 node）'
    $spDir = Join-Path $OutDir 'runtime\python\Lib\site-packages'

    # --- 1. 字节码缓存：**刻意保留** ---------------------------------------
    #     删 __pycache__ 会省 ~30 MB staging，但新用户首次启动要重新编译全部
    #     import（sidecar 启动预算是 25s，见 app/desktop/main.js
    #     SIDECAR_READY_TIMEOUT_MS），慢机/带杀软扫描时会直接超时被杀。
    #     实测过这个问题，因此这里只记一笔、不动手。
    $pycMB = [math]::Round(((Get-ChildItem -LiteralPath $spDir -Recurse -Directory -Force -Filter '__pycache__' -ErrorAction SilentlyContinue |
        ForEach-Object { (Get-ChildItem $_.FullName -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum }) |
        Measure-Object -Sum).Sum / 1MB, 2)
    Note ("保留 __pycache__：{0} MB（首启编译成本换首启速度，见脚本注释）" -f $pycMB)

    # --- 1.5 构建残留：Cython 生成源码 / C 头文件 / 类型存根 ---------------
    #     这些文件运行期一律不读（Python 只加载 .py/.pyd），fontTools 的 .c 就占 ~7 MB。
    $buildLeftovers = @(Get-ChildItem -LiteralPath $spDir -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @('.c', '.h', '.pyx', '.pxd', '.pyi') })
    Invoke-PruneCollection -Targets $buildLeftovers `
        -Label 'runtime\python\Lib\site-packages\**\*.c|*.h|*.pyx|*.pxd|*.pyi' `
        -Why '构建残留（Cython 生成源码 / C 头文件 / 类型存根）：运行期不读'

    # --- 2. 各包的 tests/（保留 numpy.testing / pandas.testing / sqlalchemy.testing）
    $testDirs = @(Get-ChildItem -LiteralPath $spDir -Recurse -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq 'tests' -or $_.Name -like 'test_*' } |
        Where-Object { $_.FullName -notmatch '\\testing(\\|$)|\\_testing(\\|$)' })
    Invoke-PruneCollection -Targets $testDirs `
        -Label 'runtime\python\Lib\site-packages\**\{tests,test_*}（保留 *_testing 公共 API）' `
        -Why '第三方包自带测试套件：运行时不需要'

    # --- 3. 开发/测试工具链（打包运行时既不安装也不跑测试）------------------
    $devTooling = @(
        @{ G = 'runtime\python\Lib\site-packages\pip*';           W = 'pip：运行时不需要安装包' },
        @{ G = 'runtime\python\Lib\site-packages\setuptools*';     W = 'setuptools/pkg_resources：运行时不需要构建' },
        @{ G = 'runtime\python\Lib\site-packages\_distutils_hack'; W = 'setuptools 的 distutils 兼容钩子' },
        @{ G = 'runtime\python\Lib\site-packages\distutils-precedence.pth'; W = 'setuptools 的 .pth 钩子' },
        @{ G = 'runtime\python\Lib\site-packages\virtualenv*';     W = 'virtualenv：仅开发用' },
        @{ G = 'runtime\python\Lib\site-packages\distlib*';        W = 'virtualenv 依赖' },
        @{ G = 'runtime\python\Lib\site-packages\nodeenv*';        W = 'virtualenv 依赖' },
        @{ G = 'runtime\python\Lib\site-packages\pre_commit*';     W = 'pre-commit：仅开发用' },
        @{ G = 'runtime\python\Lib\site-packages\identify*';       W = 'pre-commit 依赖' },
        @{ G = 'runtime\python\Lib\site-packages\cfgv*';           W = 'pre-commit 依赖' },
        @{ G = 'runtime\python\Lib\site-packages\pytest*';         W = 'pytest/pytest_asyncio：运行时不需要' },
        @{ G = 'runtime\python\Lib\site-packages\_pytest*';        W = 'pytest 内核' },
        @{ G = 'runtime\python\Lib\site-packages\pluggy*';         W = 'pytest 钩子库' },
        @{ G = 'runtime\python\Lib\site-packages\iniconfig*';      W = 'pytest 依赖' }
    )
    foreach ($d in $devTooling) { Invoke-PrunePath -Glob $d.G -Why $d.W }

    # --- 4. 大块数据 / 文档 -------------------------------------------------
    $dataAndDocs = @(
        @{ G = 'runtime\python\Lib\site-packages\matplotlib\mpl-data\sample_data'; W = 'matplotlib 示例数据' },
        @{ G = 'runtime\python\Lib\site-packages\jieba\lac_small\model_baseline'; W = 'jieba LAC 预训练模型（仅 jieba.lac* 使用）' },
        @{ G = 'runtime\python\Lib\site-packages\jieba\posseg';                     W = 'jieba 词性标注（本项目只用 cut/lcut/add_word）' },
        @{ G = 'runtime\python\Lib\site-packages\jieba\analyse';                    W = 'jieba 关键词/TF-IDF/TextRank（未使用；idf 词表 5.9 MB）' },
        @{ G = 'runtime\python\Lib\site-packages\cv2\data';                        W = 'OpenCV Haar 级联/示例数据（本项目不用人脸检测）' },
        @{ G = 'runtime\python\Lib\site-packages\cv2\opencv_videoio_ffmpeg4110_64.dll'; W = 'OpenCV FFmpeg 后端（滑动验证码只用 matchTemplate/imread）' },
        @{ G = 'runtime\python\Lib\site-packages\PIL\_avif.cp311-win_amd64.pyd';   W = 'Pillow AVIF 编解码器（平台图片不用 AVIF）' },
        @{ G = 'runtime\python\Lib\site-packages\playwright\driver\package\types';  W = 'Playwright driver 的 .d.ts 类型声明（运行期不读）' }
    )
    foreach ($d in $dataAndDocs) { Invoke-PrunePath -Glob $d.G -Why $d.W }

    # --- 4.5 应用用不到的能力分支（有明确代码依据，不是猜）------------------
    #   * 数据库：engine 强制 sqlite（app/engine/patches.py 里
    #     `config.SAVE_DATA_OPTION = "sqlite"`），mysql/postgres 的**驱动本体**与
    #     alembic 迁移不会被 import（db_session.py 只在 URL 字符串里出现
    #     mysql+asyncmy://、postgresql+asyncpg://）。
    #     注意：aiomysql/pymysql（MediaCrawler/var.py 顶层 import）和
    #     motor/pymongo/bson（store/*/_store_impl.py 顶层 import MongoDBStoreBase）
    #     是**必须保留**的，删了整条采集链路 import 就崩。
    #   * 加密：cryptography 不在任何运行链路上（DPAPI 用 Electron 侧；MC 用 pycryptodome）。
    #   * 高亮：pygments 只被 rich.syntax 用，而 rich 在打包运行时不参与
    #     （sidecar 的 test_notify.py 不进包，typer 只用到 rich.console）。
    $unusedFeatures = @(
        @{ G = 'runtime\python\Lib\site-packages\asyncmy*';    W = 'MySQL 异步驱动本体（引擎强制 sqlite，仅 URL 字符串引用）' },
        @{ G = 'runtime\python\Lib\site-packages\asyncpg*';    W = 'PostgreSQL 异步驱动本体（引擎强制 sqlite，仅 URL 字符串引用）' },
        @{ G = 'runtime\python\Lib\site-packages\alembic*';    W = 'alembic 数据库迁移（运行时不建迁移）' },
        @{ G = 'runtime\python\Lib\site-packages\sqlalchemy\dialects\postgresql'; W = 'SQLAlchemy 方言：postgres（引擎强制 sqlite）' },
        @{ G = 'runtime\python\Lib\site-packages\sqlalchemy\dialects\mysql';      W = 'SQLAlchemy 方言：mysql（引擎强制 sqlite）' },
        @{ G = 'runtime\python\Lib\site-packages\sqlalchemy\dialects\oracle';     W = 'SQLAlchemy 方言：oracle（引擎强制 sqlite）' },
        @{ G = 'runtime\python\Lib\site-packages\sqlalchemy\dialects\mssql';      W = 'SQLAlchemy 方言：mssql（引擎强制 sqlite）' },
        @{ G = 'runtime\python\Lib\site-packages\cryptography*'; W = 'cryptography：运行链路未使用' },
        @{ G = 'runtime\python\Lib\site-packages\pygments*';   W = 'pygments：仅 rich.syntax 使用，打包运行时不涉及' },
        @{ G = 'runtime\python\Lib\site-packages\Crypto\SelfTest'; W = 'pycryptodome 自带自测向量（运行期不跑）' },
        @{ G = 'runtime\python\Lib\site-packages\lxml\includes';   W = 'lxml 的 C 头文件（运行期不需要）' }
    )
    foreach ($d in $unusedFeatures) { Invoke-PrunePath -Glob $d.G -Why $d.W }

    # --- 5. 编译期产物 / tkinter 无关部分 ----------------------------------
    $buildArtifacts = @(
        @{ G = 'runtime\python\include';                    W = 'CPython C 头文件：运行期不需要' },
        @{ G = 'runtime\python\libs';                       W = 'CPython 静态库（.lib）：运行期不需要' },
        @{ G = 'runtime\python\tcl';                        W = 'Tcl/Tk 数据目录：本项目无 tkinter' },
        @{ G = 'runtime\python\DLLs\tcl86t.dll';            W = 'Tcl 运行库：无 tkinter' },
        @{ G = 'runtime\python\DLLs\tk86t.dll';             W = 'Tk 运行库：无 tkinter' },
        @{ G = 'runtime\python\DLLs\_tkinter.pyd';          W = 'tkinter 扩展模块' },
        @{ G = 'runtime\python\Lib\tkinter';                W = 'tkinter 纯 Python 包' },
        @{ G = 'runtime\python\Lib\idlelib';                W = 'IDLE 编辑器' },
        @{ G = 'runtime\python\Lib\ensurepip';              W = 'ensurepip：运行时不需要安装包' },
        @{ G = 'runtime\python\Lib\pydoc_data';             W = 'pydoc 数据表' },
        @{ G = 'runtime\python\Lib\venv';                   W = 'venv 模块：打包运行时不需要再建虚拟环境' },
        @{ G = 'runtime\python\Lib\distutils';              W = 'distutils（3.11 已并入 setuptools）' },
        @{ G = 'runtime\python\Lib\turtledemo';             W = 'turtle 示例' },
        @{ G = 'runtime\python\Lib\lib2to3';                W = '2to3 迁移工具' }
    )
    foreach ($d in $buildArtifacts) { Invoke-PrunePath -Glob $d.G -Why $d.W }

    # --- 6. 重复的 node.exe：Playwright driver 内置 vs runtime/node ----------
    #     Playwright 的 python 驱动默认用 driver\node.exe 跑 driver\package\cli.js。
    #     安装包已经自带 runtime\node\node.exe（execjs / 知乎签名用），两者是同一种
    #     Node 运行时；保留两份等于白占 88 MB。做法：删掉 driver 内那份，放一个
    #     sitecustomize shim（Python 启动时自动执行）把 PLAYWRIGHT_NODEJS_PATH
    #     指到共用 node（Playwright 官方支持该环境变量）。
    $pwNode = Join-Path $OutDir 'runtime\python\Lib\site-packages\playwright\driver\node.exe'
    $sharedNode = Join-Path $OutDir 'runtime\node\node.exe'
    if ((Test-Path -LiteralPath $pwNode) -and (Test-Path -LiteralPath $sharedNode)) {
        Invoke-PrunePath -Glob 'runtime\python\Lib\site-packages\playwright\driver\node.exe' `
            -Why 'Playwright driver 自带 node.exe 与 runtime/node 重复（由 sitecustomize 指向共用 node）'
        $nodeShim = @'
# ---------------------------------------------------------------------------
# 由 app/scripts/build_pack_resources.ps1 生成（安装包瘦身 / M5b）。
#
# Playwright for Python 默认用 playwright/driver/node.exe 跑 driver/package/cli.js。
# 安装包只保留一份 Node 运行时（resources/runtime/node/node.exe，execjs 与
# Playwright 共用），所以这里在解释器启动时把 PLAYWRIGHT_NODEJS_PATH 指过去。
# 没有这个文件时 Playwright 仍按自己的默认路径查找，不影响其他环境。
# ---------------------------------------------------------------------------
import os as _os
import sys as _sys
from pathlib import Path as _Path


def _apply_playwright_node_shim():
    if _sys.platform != "win32":
        return
    if _os.environ.get("PLAYWRIGHT_NODEJS_PATH"):
        return
    candidates = [
        _Path(_sys.executable).resolve().parent.parent / "node" / "node.exe",
        _Path(_sys.executable).resolve().parent / ".." / "node" / "node.exe",
    ]
    for node in candidates:
        if node.is_file():
            _os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(node)
            return


try:
    _apply_playwright_node_shim()
except Exception:  # 启动期绝不能抛：任何异常都静默忽略
    pass
'@
        Set-Content -LiteralPath (Join-Path $spDir 'sitecustomize.py') -Value $nodeShim -Encoding utf8
        Note 'sitecustomize.py 已写入（PLAYWRIGHT_NODEJS_PATH → runtime/node/node.exe）'
    } else {
        Warn 'Playwright driver node 去重跳过（driver/node.exe 或 runtime/node/node.exe 缺失）'
    }
}

$stagingMBAfterPrune = Get-DirSizeMB $OutDir

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

# =============================================================================
# 剪裁后验证（真跑）：import 探针 + 关键功能（分词/图像/Excel/绘图/词云/驱动）
# =============================================================================
if (-not $SkipRuntime) {
    Step 'verifying pruned runtime（import 探针 + 关键功能）'
    $pyExe = Join-Path $OutDir 'runtime\python\python.exe'
    $env:PYTHONNOUSERSITE = '1'
    $probeOut = & $pyExe -B -c 'import fastapi, uvicorn, httpx, pandas, openpyxl, sqlalchemy, playwright, cv2, jieba, PIL, matplotlib, wordcloud, numpy, lxml, parsel, typer, rich, aiosqlite, aiofiles, websockets, execjs; print("runtime imports ok")' 2>&1
    if ($LASTEXITCODE -ne 0) { Fail "portable Python import check failed: $probeOut" }
    Write-Host "    $probeOut"

    $probeFile = Join-Path $env:TEMP ("ol_prune_probe_{0}.py" -f $PID)
    $probeTmp = Join-Path $env:TEMP ("ol_prune_probe_{0}" -f $PID)
    $probeSource = @'
import os
import sys
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import cv2
import jieba
import numpy as np
import openpyxl
import PIL.Image
from wordcloud import WordCloud

tmp = sys.argv[1]
os.makedirs(tmp, exist_ok=True)

# pandas / numpy
import pandas as pd
df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
assert df["a"].sum() == 6

# openpyxl 往返
wb = openpyxl.Workbook()
wb.active["A1"] = "报告"
xlsx = os.path.join(tmp, "t.xlsx")
wb.save(xlsx)
assert openpyxl.load_workbook(xlsx).active["A1"].value == "报告"

# jieba（分词；posseg/analyse 已按“本项目未使用”剪掉，这里只验 cut/lcut/add_word）
words = jieba.lcut("今天天气不错，我们去公园散步")
assert words, "jieba.lcut 返回空"
jieba.add_word("舆情分析")
assert "舆情分析" in jieba.lcut("舆情分析很重要")

# Pillow 往返
png = os.path.join(tmp, "p.png")
PIL.Image.new("RGB", (64, 32), (10, 120, 200)).save(png)
assert PIL.Image.open(png).size == (64, 32)

# OpenCV：模板匹配（滑动验证码路径）
hay = np.zeros((48, 48), np.uint8)
hay[12:36, 12:36] = 255
needle = np.zeros((24, 24), np.uint8)
needle[:] = 255
cv2.matchTemplate(hay, needle, cv2.TM_CCOEFF_NORMED)
cv2.imwrite(os.path.join(tmp, "cv.png"), cv2.imread(png))

# matplotlib（报告图表）
fig, ax = plt.subplots()
ax.plot([1, 2, 3])
ax.set_title("probe")
fig.savefig(os.path.join(tmp, "m.png"))
plt.close(fig)

# wordcloud（媒体词云）
wc = WordCloud(width=160, height=100, background_color="white")
wc.generate_from_frequencies({"舆情": 10, "报告": 8})
wc.to_file(os.path.join(tmp, "wc.png"))

# playwright：驱动 node 必须解析到真实存在的可执行文件
from playwright._impl._driver import compute_driver_executable
node_path, cli_path = compute_driver_executable()
assert os.path.isfile(node_path), "playwright 驱动 node 不存在: %s" % node_path
assert os.path.isfile(cli_path), "playwright cli.js 不存在: %s" % cli_path
print("playwright driver node:", node_path)

# MediaCrawler 采集链路 import 探针：剪裁最容易踩的坑就在这里
# （var.py 顶层 import aiomysql→pymysql、store/*/_store_impl.py 顶层 import
#  MongoDBStoreBase→motor、zhihu/tieba help.py 顶层 import parsel→lxml）。
res_root = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
mc_root = os.path.join(res_root, "MediaCrawler")
if os.path.isdir(mc_root):
    sys.path.insert(0, mc_root)
    os.chdir(mc_root)
    import config  # noqa: F401
    import database.db_session  # noqa: F401
    import store.zhihu, store.xhs  # noqa: F401
    import media_platform.zhihu.core, media_platform.tieba.core  # noqa: F401
    import media_platform.xhs.core, media_platform.weibo.core  # noqa: F401
    import media_platform.bilibili.core, media_platform.douyin.core  # noqa: F401
    import media_platform.kuaishou.core  # noqa: F401
    import cmd_arg.arg, proxy.proxy_ip_pool, tools.cdp_browser, tools.words  # noqa: F401
    print("MediaCrawler import chain ok")
else:
    print("MediaCrawler 目录不存在，跳过采集链路 import 探针")

print("functional probe ok")
'@
    Set-Content -LiteralPath $probeFile -Value $probeSource -Encoding utf8
    try {
        $fnOut = & $pyExe -B $probeFile $probeTmp 2>&1
        if ($LASTEXITCODE -ne 0) { Fail "functional probe failed: $fnOut" }
        $fnOut | ForEach-Object { Write-Host "    $_" }
    } finally {
        Remove-Item -LiteralPath $probeFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $probeTmp -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONNOUSERSITE -ErrorAction SilentlyContinue
    }
}

# -----------------------------------------------------------------------------
# staging 体积清单（剪裁明细）
# -----------------------------------------------------------------------------
$finalMB = Get-DirSizeMB $OutDir
$licenseFiles = Get-ChildItem -LiteralPath (Join-Path $OutDir 'runtime\python\Lib\site-packages') -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^(LICENSE|COPYING|NOTICE|THIRD[-_ ]?PARTY)' }
# 注意：本机 PowerShell（7.6 / .NET 10）对 @(List[object]) 会抛 "Argument types do
# not match"，因此明细表统一用 ToArray() 落地，累加值也单独维护。
$prunedEntries = $script:pruned.ToArray()
$prunedTotalMB = 0.0
foreach ($e in $prunedEntries) { $prunedTotalMB += [double]$e['removed_mb'] }
$prunedTotalMB = [math]::Round($prunedTotalMB, 2)
$manifest = [ordered]@{
    baseline_mb     = $stagingMBBeforePrune
    pruned          = $prunedEntries
    final_mb        = $finalMB
    generated_at    = (Get-Date).ToString('o')
    prune_enabled   = (-not $SkipPrune -and -not $SkipRuntime)
    pruned_mb       = $prunedTotalMB
    licenses_kept   = $licenseFiles.Count
}
$manifestPath = Join-Path $OutDir 'staging-manifest.json'
$manifest | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $manifestPath

$info = [ordered]@{
    built_at = (Get-Date).ToString('o')
    python = (& (Join-Path $OutDir 'runtime\python\python.exe') --version 2>&1)
    electron = (Get-Content (Join-Path $Repo 'app\desktop\package.json') -Raw | ConvertFrom-Json).devDependencies.electron
    size_mb = $finalMB
    engine_sha = (Get-FileHash (Join-Path $OutDir 'engine\entry.py') -Algorithm SHA256).Hash
    pruned = $manifest.prune_enabled
    pruned_mb = $prunedTotalMB
}
$info | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 (Join-Path $OutDir 'build-info.json')

Step 'staging ready'
Note ("output: {0}" -f $OutDir)
if ($manifest.prune_enabled) {
    Note ("size:   {0} MB（剪裁前 {1} MB，删除 {2} MB）" -f $finalMB, $stagingMBBeforePrune, $manifest.pruned_mb)
    Note ("manifest: {0}" -f $manifestPath)
} else {
    Note ("size:   {0} MB（未剪裁）" -f $finalMB)
}
Get-ChildItem $OutDir | Select-Object Name, Mode | Format-Table -AutoSize | Out-String -Width 80 | Write-Host
