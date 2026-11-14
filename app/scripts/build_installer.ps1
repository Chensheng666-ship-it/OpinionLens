#Requires -Version 7.0
<#
.SYNOPSIS
    舆见 OpinionLens — Windows x64 NSIS 安装包一键构建 / 验证脚本（开源版）。

.DESCRIPTION
    流程：
      1) 环境检查（node/npm/electron/electron-builder，npmmirror 镜像）
      2) 调 app/scripts/build_pack_resources.ps1 生成 build/installer/resources
         （-SkipResources 时跳过，直接使用已有 staging）
      3) 校验 staging 必需文件（python.exe / sidecar / engine / MediaCrawler）
      4) electron-builder --win nsis --x64（输出 build/installer/output）
      5) 打印安装包绝对路径 / 大小 / SHA256
      6) 可选验证：静默安装到 build/installer/verify-install → 校验 resources 完整
         → 启动安装后的 exe --screenshot 截图（--no-sidecar 与真实 sidecar 各一次）
         → 静默卸载（-KeepInstall 可保留安装）

.PARAMETER SkipResources
    跳过 resources staging 重新生成，直接使用 build/installer/resources 现有内容。

.PARAMETER Clean
    清理输出目录；同时把 -Clean 传给 build_pack_resources.ps1（重建 staging）。

.PARAMETER SkipVerify
    只构建，不做静默安装 / 截图验证。

.PARAMETER SkipPrune
    透传给 build_pack_resources.ps1：staging 不剪裁（保留测试/文档/开发工具/重复 node）。
    仅用于排查「剪裁后某能力不可用」，正常运行构建不要加。

.PARAMETER Compression
    NSIS 压缩级别，默认 maximum（LZMA 固态压缩，安装包最小；打包耗时更长）。
    传 normal 可回到快速构建模式。

.PARAMETER SkipSidecarVerify
    验证时只跑 --no-sidecar 截图，不启动真实 sidecar（更快）。

.PARAMETER KeepInstall
    验证后不卸载，保留在验证目录以便人工查看。

.PARAMETER VerifyDir
    静默安装验证目录。默认 %LOCALAPPDATA%\OpinionLens_verify（C: 盘）。
    **不要放在网络盘/透明加密盘**：实测安装包解压到加密盘会卡死；
    路径不要包含空格（NSIS /D 限制）。

.PARAMETER OutputDir
    产物输出目录，默认 build/installer/output。
    传入其他目录时通过 electron-builder --config.directories.output 覆盖（用于可重复性构建，
    不覆盖已交付安装包）。

.NOTES
    安装调用方式：用 `cmd.exe /c ""<setup>" /S /D=<dir>"`（CreateProcess），
    不要用 PowerShell Start-Process 直接启动未签名安装包（SmartScreen 可能卡住）。
    启动安装后的 exe 截图前会清除 ELECTRON_RUN_AS_NODE（DSH/终端会注入）。

.EXAMPLE
    pwsh -NoProfile -File app/scripts/build_installer.ps1
    pwsh -NoProfile -File app/scripts/build_installer.ps1 -SkipResources
    pwsh -NoProfile -File app/scripts/build_installer.ps1 -SkipResources -SkipVerify
    pwsh -NoProfile -File app/scripts/build_installer.ps1 -Clean
#>
[CmdletBinding()]
param(
    [switch]$SkipResources,
    [switch]$Clean,
    [switch]$SkipVerify,
    [switch]$SkipSidecarVerify,
    [switch]$SkipPrune,
    [switch]$KeepInstall,
    [string]$OutputDir = '',
    [string]$VerifyDir = '',
    # NSIS 压缩级别（默认 normal，与历史构建一致）。实测 maximum 对本项目产物体积
    # 无可见收益（177.8 MB vs 177.8 MB，差 3 字节），只是更慢，所以不做默认；
    # 需要极限压缩时可显式传 -Compression maximum。
    [ValidateSet('store', 'normal', 'maximum')]
    [string]$Compression = 'normal'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# -----------------------------------------------------------------------------
# 基础路径
# -----------------------------------------------------------------------------
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = (Resolve-Path (Join-Path $Here '..\..')).Path
$Desktop = Join-Path $Repo 'app\desktop'
$ResScript = Join-Path $Repo 'app\scripts\build_pack_resources.ps1'
$ResDir = Join-Path $Repo 'build\installer\resources'
if (-not $OutputDir) { $OutputDir = Join-Path $Repo 'build\installer\output' }
if (-not $VerifyDir) {
    # 默认放 C: 盘 per-user 目录：网络盘/透明加密盘会导致 NSIS 解压阶段卡死（实测）
    $localAppData = $env:LOCALAPPDATA
    if (-not $localAppData) { $localAppData = [Environment]::GetFolderPath('LocalApplicationData') }
    if (-not $localAppData) { $localAppData = Join-Path $env:USERPROFILE 'AppData\Local' }
    $VerifyDir = Join-Path $localAppData 'OpinionLens_verify'
}
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
$VerifyDir = [IO.Path]::GetFullPath($VerifyDir)

function Step([string]$Text) { Write-Host ''; Write-Host "===== $Text =====" -ForegroundColor Cyan }
function Note([string]$Text) { Write-Host "  $Text" -ForegroundColor DarkGray }
function Ok([string]$Text) { Write-Host "  [OK] $Text" -ForegroundColor Green }
function Warn([string]$Text) { Write-Host "  [WARN] $Text" -ForegroundColor Yellow }
function Fail([string]$Text) { throw $Text }

function Assert-FileExists([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) { Fail "$Label 不存在：$Path" }
}

function Get-SizeMB([long]$Bytes) { return [math]::Round($Bytes / 1MB, 1) }

# -----------------------------------------------------------------------------
# 0. 环境与镜像
# -----------------------------------------------------------------------------
Step '环境检查'
$env:ELECTRON_MIRROR = 'https://npmmirror.com/mirrors/electron/'
$env:ELECTRON_BUILDER_BINARIES_MIRROR = 'https://npmmirror.com/mirrors/electron-builder-binaries/'
$env:NPM_CONFIG_REGISTRY = 'https://registry.npmmirror.com'
# 本机 DSH 会话可能注入 ELECTRON_RUN_AS_NODE，会让 Electron 以 node 模式运行
Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue

foreach ($cmd in @('node', 'npm')) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { Fail "缺少命令：$cmd" }
}
Note ("node {0} / npm {1}" -f (& node -v), (& npm -v))

Assert-FileExists (Join-Path $Desktop 'node_modules\electron\dist\electron.exe') 'electron（app/desktop/node_modules）'
if (-not (Test-Path -LiteralPath (Join-Path $Desktop 'node_modules\electron-builder\package.json'))) {
    Step '安装 electron-builder@26.18.0（npmmirror 镜像）'
    Push-Location -LiteralPath $Desktop
    try {
        & npm install --save-dev electron-builder@26.18.0 --registry=https://registry.npmmirror.com --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { Fail "npm install electron-builder 失败（exit $LASTEXITCODE）" }
    } finally {
        Pop-Location
    }
}
$ebVersion = (Get-Content -LiteralPath (Join-Path $Desktop 'node_modules\electron-builder\package.json') -Raw | ConvertFrom-Json).version
Ok ("electron-builder {0}" -f $ebVersion)
$electronVersion = (Get-Content -LiteralPath (Join-Path $Desktop 'node_modules\electron\package.json') -Raw | ConvertFrom-Json).version
Note ("electron {0}（保持不变）" -f $electronVersion)

$driveLetter = (Split-Path -Qualifier $Repo).TrimEnd(':')
$freeGB = [math]::Round((Get-PSDrive -Name $driveLetter).Free / 1GB, 1)
Note ("磁盘可用 {0} GB" -f $freeGB)
if ($freeGB -lt 8) { Warn "磁盘可用空间不足 8GB，构建可能失败" }

if ($Clean -and (Test-Path -LiteralPath $OutputDir)) {
    Step "清理输出目录 $OutputDir"
    Remove-Item -LiteralPath $OutputDir -Recurse -Force
}

# -----------------------------------------------------------------------------
# 1. staging resources
# -----------------------------------------------------------------------------
if (-not $SkipResources) {
    Step '生成 staging resources（build_pack_resources.ps1）'
    Assert-FileExists $ResScript 'build_pack_resources.ps1'
    $psArgs = @('-NoProfile', '-File', $ResScript)
    if ($Clean) { $psArgs += '-Clean' }
    if ($SkipPrune) { $psArgs += '-SkipPrune' }
    & pwsh @psArgs
    if ($LASTEXITCODE -ne 0) { Fail "build_pack_resources.ps1 失败（exit $LASTEXITCODE）" }
} else {
    Step '跳过 resources 生成（-SkipResources，使用已有 staging）'
}

# -----------------------------------------------------------------------------
# 2. staging 完整性校验（失败即中止，避免打出坏包）
# -----------------------------------------------------------------------------
Step "校验 staging：$ResDir"
$requiredFiles = @(
    @{ Rel = 'runtime\python\python.exe'; Label = '便携 Python 解释器' },
    @{ Rel = 'sidecar\main.py';           Label = 'sidecar 入口' },
    @{ Rel = 'engine\entry.py';           Label = 'engine 入口' },
    @{ Rel = 'MediaCrawler\main.py';      Label = 'MediaCrawler 源码' },
    @{ Rel = 'LICENSES';                  Label = '第三方许可证目录' },
    @{ Rel = 'build-info.json';           Label = 'staging 构建信息' }
)
foreach ($item in $requiredFiles) {
    Assert-FileExists (Join-Path $ResDir $item.Rel) $item.Label
    Ok $item.Label
}
# 报告统计/模板管线目录（app/report_pipeline → resources/report_pipeline）
$reportPipeline = Join-Path $ResDir 'report_pipeline'
if (Test-Path -LiteralPath $reportPipeline) {
    $rpFiles = Get-ChildItem -LiteralPath $reportPipeline -Recurse -File -ErrorAction SilentlyContinue
    if ($rpFiles.Count -eq 0) { Fail "report_pipeline 为空：$reportPipeline" }
    Ok ("报告流水线（{0} 个文件）" -f $rpFiles.Count)
} else {
    Warn '报告流水线资源缺失（app/report_pipeline）：报告中心统计/本地模板将不可用'
}

$resSizeMB = Get-SizeMB ((Get-ChildItem -LiteralPath $ResDir -Recurse -File | Measure-Object Length -Sum).Sum)
Note ("staging 体积：{0} MB" -f $resSizeMB)
Note ("运行时 Python：{0}" -f (& (Join-Path $ResDir 'runtime\python\python.exe') --version 2>&1))

# -----------------------------------------------------------------------------
# 2.5 版本一致性守卫：staging 不得旧于源码（2026-11-11 修复）
#     历史上先跑过 -SkipResources 会把旧 sidecar/engine 打进安装包，导致
#     “新界面调用新接口 404（采集动态 Not Found）”。这里直接中止，避免再产出坏包。
# -----------------------------------------------------------------------------
$stagingInfo = Get-Item -LiteralPath (Join-Path $ResDir 'build-info.json')
$watchedSources = @(
    (Join-Path $Repo 'app\sidecar'),
    (Join-Path $Repo 'app\engine'),
    (Join-Path $Repo 'app\report_pipeline')
) | Where-Object { Test-Path -LiteralPath $_ }
$newestSource = Get-ChildItem -LiteralPath $watchedSources -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\__pycache__\\|\\_fixtures\\|\\selftest\.py$|\\test_[^\\]*\.py$|\\_fake_' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($newestSource -and $stagingInfo.LastWriteTime -lt $newestSource.LastWriteTime) {
    $staleMsg = ("staging 已过期：最新源文件 {0}（{1:yyyy-MM-dd HH:mm:ss}）比 staging（{2:yyyy-MM-dd HH:mm:ss}）新。" -f `
        $newestSource.FullName, $newestSource.LastWriteTime, $stagingInfo.LastWriteTime) +
        " 这会把旧 sidecar/engine 打进安装包（表现为「采集动态接口 Not Found」）。请先运行 build_pack_resources.ps1（不要加 -SkipResources）再重新构建。"
    Fail $staleMsg
}
Ok ("staging 与源码一致（staging {0:MM-dd HH:mm}，最新源码 {1:MM-dd HH:mm}）" -f $stagingInfo.LastWriteTime, $newestSource.LastWriteTime)

# -----------------------------------------------------------------------------
# 3. electron-builder 构建
# -----------------------------------------------------------------------------
Step 'electron-builder 构建 NSIS 安装包（win x64）'
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$builderCmd = Join-Path $Desktop 'node_modules\.bin\electron-builder.cmd'
Assert-FileExists $builderCmd 'electron-builder CLI'
Push-Location -LiteralPath $Desktop
try {
    # 直接继承控制台输出（不用管道捕获 GUI 进程输出，避免 PowerShell 挂起）
    $builderArgs = @('--win', 'nsis', '--x64', '--publish', 'never', "--config.compression=$Compression")
    Note ("NSIS 压缩级别：{0}" -f $Compression)
    $defaultOutputDir = [IO.Path]::GetFullPath((Join-Path $Repo 'build\installer\output'))
    if ($OutputDir -ne $defaultOutputDir) {
        # 自定义输出目录：可重复性构建时不覆盖已交付安装包
        $builderArgs += "--config.directories.output=$OutputDir"
        Note ("electron-builder 输出目录覆盖：{0}" -f $OutputDir)
    }
    & $builderCmd @builderArgs
    $buildExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($buildExit -ne 0) { Fail "electron-builder 失败（exit $buildExit），请查看上方日志" }
Ok 'electron-builder 完成'

# app.asar 白名单校验：main.js 里 `require('./xxx')` 的本地模块必须真的进包。
# 漏一个就会在打包版启动时抛错——表现为「A JavaScript error occurred in the main process」
# 弹窗卡住、main.log 一行不写（0.5.0 首次出包就踩过：update.js 没在 electron-builder.yml 的 files 里）。
$asarPath = Join-Path $OutputDir 'win-unpacked\resources\app.asar'
if (Test-Path -LiteralPath $asarPath) {
    $mainSrc = Get-Content -LiteralPath (Join-Path $Desktop 'main.js') -Raw
    $localRequires = @(
        [regex]::Matches($mainSrc, "require\(\s*['""](\./[^'""]+)['""]\s*\)") |
            ForEach-Object { $_.Groups[1].Value -replace '^\./', '' } |
            Sort-Object -Unique
    )
    $asarCli = Join-Path $Desktop 'node_modules\@electron\asar\bin\asar.js'
    if ($localRequires.Count -gt 0) {
        if (Test-Path -LiteralPath $asarCli) {
            $listing = (& node $asarCli list $asarPath 2>$null) -join "`n"
            $missing = @()
            foreach ($rel in $localRequires) {
                $needle = '\' + ($rel -replace '/', '\')
                if ($listing -notmatch [regex]::Escape($needle)) { $missing += $rel }
            }
            if ($missing.Count -gt 0) {
                Fail ("app.asar 缺少主进程 local require 目标：{0}。请把它们加进 app/desktop/electron-builder.yml 的 files（建议用 `"*.js`" 通配）。" -f ($missing -join ', '))
            }
            Ok ("app.asar 已包含 {0} 个本地 require 目标：{1}" -f $localRequires.Count, ($localRequires -join ', '))
        }
        else {
            Warn '未找到 @electron/asar，跳过 app.asar 内容校验'
        }
    }
}

# -----------------------------------------------------------------------------
# 4. 产物信息（安装包路径 / 大小 / SHA256）
# -----------------------------------------------------------------------------
Step '产物信息'
$setup = Get-ChildItem -LiteralPath $OutputDir -File -Filter '*Setup*.exe' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setup) { Fail "未找到安装包（*Setup*.exe）：$OutputDir" }
$setupHash = (Get-FileHash -LiteralPath $setup.FullName -Algorithm SHA256).Hash.ToLower()
$setupMB = Get-SizeMB $setup.Length
Write-Host ''
Write-Host "安装包: $($setup.FullName)" -ForegroundColor Yellow
Write-Host ("大小:   {0} MB ({1} bytes)" -f $setupMB, $setup.Length) -ForegroundColor Yellow
Write-Host "SHA256: $setupHash" -ForegroundColor Yellow
$unpacked = Join-Path $OutputDir 'win-unpacked'
if (Test-Path -LiteralPath $unpacked) {
    $unpackedMB = Get-SizeMB ((Get-ChildItem -LiteralPath $unpacked -Recurse -File | Measure-Object Length -Sum).Sum)
    Note ("win-unpacked（安装后体积参考）：{0} MB" -f $unpackedMB)
}

# -----------------------------------------------------------------------------
# 5. 验证：静默安装 → resources 完整性 → 截图 → 卸载
# -----------------------------------------------------------------------------
if ($SkipVerify) {
    Step '跳过安装验证（-SkipVerify）'
} else {
    Step "静默安装验证 → $VerifyDir"
    # 校验验证目录：C: 盘、无空格（NSIS /D 参数限制）
    $verifyDrive = (Split-Path -Qualifier $VerifyDir).TrimEnd(':')
    if ($verifyDrive -ne 'C') {
        Warn "验证目录不在 C: 盘（$VerifyDir）：网络盘/透明加密盘可能导致 NSIS 解压卡死（实测），建议用默认目录"
    }
    if ($VerifyDir -match '\s') {
        Fail "验证目录包含空格，NSIS /D 参数不支持：$VerifyDir（请用 -VerifyDir 指定无空格路径）"
    }

    # 关闭可能残留的已安装实例，避免文件占用
    Get-Process -Name 'OpinionLens' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $VerifyDir) { Remove-Item -LiteralPath $VerifyDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $VerifyDir | Out-Null

    # 用 cmd.exe /c（CreateProcess）调用安装包：直接 Start-Process 安装包在未签名/SmartScreen
    # 场景下可能卡住（实测），cmd.exe 方式已验证可用。
    $comspec = $env:ComSpec
    if (-not $comspec) { $comspec = 'cmd.exe' }
    $installArgs = '/c ""{0}" /S /D={1}"' -f $setup.FullName, $VerifyDir
    Note ("调用安装：{0} {1}" -f $comspec, $installArgs)
    $installProc = Start-Process -FilePath $comspec -ArgumentList $installArgs -PassThru
    if (-not $installProc.WaitForExit(300000)) {
        & taskkill /PID $installProc.Id /T /F *> $null
        Fail '静默安装超时（300s）'
    }
    if ($installProc.ExitCode -ne 0) { Fail "静默安装失败（cmd/installer exit $($installProc.ExitCode)）" }
    Ok '静默安装完成'

    # 5.1 安装目录 / resources 完整性
    $installedExe = Join-Path $VerifyDir 'OpinionLens.exe'
    Assert-FileExists $installedExe '安装后的主程序'
    $checks = @(
        @{ Rel = 'resources\app.asar';                    Label = 'app.asar' },
        @{ Rel = 'resources\runtime\python\python.exe';   Label = 'resources/runtime/python/python.exe' },
        @{ Rel = 'resources\sidecar\main.py';             Label = 'resources/sidecar/main.py' },
        @{ Rel = 'resources\engine\entry.py';             Label = 'resources/engine/entry.py' },
        @{ Rel = 'resources\MediaCrawler\main.py';        Label = 'resources/MediaCrawler/main.py' },
        @{ Rel = 'resources\build-info.json';             Label = 'resources/build-info.json' }
    )
    # 报告管线资源（app/report_pipeline → resources/report_pipeline）
    if (Test-Path -LiteralPath $reportPipeline) {
        $checks += @{ Rel = 'resources\report_pipeline'; Label = 'resources/report_pipeline' }
    }
    foreach ($c in $checks) {
        Assert-FileExists (Join-Path $VerifyDir $c.Rel) $c.Label
        Ok $c.Label
    }
    # 5.1.1 新接口一致性：源码 sidecar 有 /feed 时，安装产物必须同样包含（防旧 staging）
    $srcSidecarMain = Join-Path $Repo 'app\sidecar\main.py'
    if ((Get-Content -LiteralPath $srcSidecarMain -Raw) -match '/api/runs/\{run_id\}/feed') {
        $installedSidecarMain = Join-Path $VerifyDir 'resources\sidecar\main.py'
        if (-not ((Get-Content -LiteralPath $installedSidecarMain -Raw) -match '/api/runs/\{run_id\}/feed')) {
            Fail "安装产物 sidecar 缺少 /feed 接口：staging 可能过期（请先跑 build_pack_resources.ps1）"
        }
        Assert-FileExists (Join-Path $VerifyDir 'resources\sidecar\run_feed.py') '安装产物 sidecar/run_feed.py'
        Ok '安装产物包含采集动态 feed 接口'
    }
    $installedMB = Get-SizeMB ((Get-ChildItem -LiteralPath $VerifyDir -Recurse -File | Measure-Object Length -Sum).Sum)
    Note ("安装后体积：{0} MB" -f $installedMB)

    # 5.2 启动安装后的 exe 截图（先 --no-sidecar 验 UI；再真实 sidecar 验链路）
    $shotsDir = Join-Path $Repo 'build\installer\verify-shots'
    New-Item -ItemType Directory -Force -Path $shotsDir | Out-Null
    $uiShot = Join-Path $shotsDir 'installed_ui_nosidecar.png'
    Set-Content -LiteralPath (Join-Path $shotsDir '.keep') -Value '' -Encoding utf8

    function Invoke-InstalledScreenshot([string]$OutPng, [string[]]$ExtraArgs, [int]$TimeoutSeconds = 180) {
        # DSH/终端可能注入 ELECTRON_RUN_AS_NODE，会让 Electron 以 Node 模式运行（bad option）
        Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $OutPng) { Remove-Item -LiteralPath $OutPng -Force }
        $argList = @('--screenshot', $OutPng) + $ExtraArgs
        $proc = Start-Process -FilePath $installedExe -WorkingDirectory $VerifyDir -ArgumentList $argList -PassThru
        if (-not $proc.WaitForExit($TimeoutSeconds * 1000)) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            # 超时常见原因是主进程启动即报错并弹「A JavaScript error…」对话框（不写日志、不生成截图）。
            # 把已安装版主进程日志尾部带出来，避免只看到一个裸超时。
            $appLog = Join-Path $env:APPDATA 'OpinionLens\logs\main.log'
            $logTail = if (Test-Path -LiteralPath $appLog) {
                ((Get-Content -LiteralPath $appLog -Tail 10) -join "`n")
            }
            else { "（未生成主进程日志：$appLog）" }
            Fail ("截图超时（{0}s）：{1}`n最近主进程日志（{2}）：`n{3}" -f $TimeoutSeconds, ($argList -join ' '), $appLog, $logTail)
        }
        if ($proc.ExitCode -ne 0) { Fail ("截图进程退出码 {0}：{1}" -f $proc.ExitCode, ($argList -join ' ')) }
        if (-not (Test-Path -LiteralPath $OutPng)) { Fail "截图文件未生成：$OutPng" }
        $shotSize = (Get-Item -LiteralPath $OutPng).Length
        if ($shotSize -lt 10KB) { Warn ("截图文件偏小（{0} bytes），可能为空白页" -f $shotSize) }
        Ok ("截图：{0}（{1} bytes）" -f $OutPng, $shotSize)
    }

    Step '安装后 exe 截图（--no-sidecar）'
    Invoke-InstalledScreenshot $uiShot @('--no-sidecar')

    if (-not $SkipSidecarVerify) {
        Step '安装后 exe 截图（真实 sidecar）'
        $realShot = Join-Path $shotsDir 'installed_ui_real_sidecar.png'
        $prevDelay = $env:SCREENSHOT_DELAY_MS
        $env:SCREENSHOT_DELAY_MS = '9000'
        try {
            Invoke-InstalledScreenshot $realShot @()
        } finally {
            if ($prevDelay) { $env:SCREENSHOT_DELAY_MS = $prevDelay } else { Remove-Item Env:SCREENSHOT_DELAY_MS -ErrorAction SilentlyContinue }
        }
    }

    # 5.3 卸载（默认静默卸载；-KeepInstall 保留）
    if ($KeepInstall) {
        Step '保留安装目录（-KeepInstall）'
        Note $VerifyDir
    } else {
        Step '静默卸载'
        Get-Process -Name 'OpinionLens' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        $uninstaller = Get-ChildItem -LiteralPath $VerifyDir -File -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $uninstaller) { Fail "未找到卸载程序（Uninstall*.exe）：$VerifyDir" }
        # 卸载器同安装包一样走 cmd.exe /c（避免未签名 exe 直接 Start-Process 卡住）
        $uninstallProc = Start-Process -FilePath $comspec -ArgumentList ('/c ""{0}" /S"' -f $uninstaller.FullName) -PassThru
        if (-not $uninstallProc.WaitForExit(120000)) {
            & taskkill /PID $uninstallProc.Id /T /F *> $null
            Warn '卸载超时（120s）'
        } elseif ($uninstallProc.ExitCode -ne 0) {
            Warn "卸载程序退出码 $($uninstallProc.ExitCode)"
        }
        Start-Sleep -Milliseconds 800
        Get-Process -Name 'OpinionLens' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath (Join-Path $VerifyDir 'OpinionLens.exe')) {
            Warn '卸载后主程序仍存在（可能被占用），请人工检查'
        } else {
            Ok '静默卸载完成'
        }
        # 清理验证目录（保留截图目录 verify-shots）
        if (Test-Path -LiteralPath $VerifyDir) {
            try {
                Remove-Item -LiteralPath $VerifyDir -Recurse -Force -ErrorAction Stop
                Ok ("已清理验证目录：{0}" -f $VerifyDir)
            } catch {
                Warn ("验证目录清理失败（可手动删除）：{0}" -f $VerifyDir)
            }
        }
    }
}

# -----------------------------------------------------------------------------
# 6. 汇总
# -----------------------------------------------------------------------------
Step '构建汇总'
Write-Host "  安装包：$($setup.FullName)"
Write-Host ("  大小：  {0} MB" -f $setupMB)
Write-Host "  SHA256：$setupHash"
Write-Host ("  输出目录：{0}" -f $OutputDir)
if (-not $SkipVerify) { Write-Host ("  截图目录：{0}" -f (Join-Path $Repo 'build\installer\verify-shots')) }
Write-Host ''
Write-Host '完成。' -ForegroundColor Green
