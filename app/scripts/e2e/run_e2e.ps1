<#
.SYNOPSIS
  OpinionLens e2e 冒烟：拉起真实 Electron（CDP 端口）→ 跑 app/scripts/e2e/e2e_smoke.js → 必杀进程。

.DESCRIPTION
  - 启动前移除 ELECTRON_RUN_AS_NODE（DSH / 某些环境会注入，否则 electron.exe 会以 node 模式运行）。
  - 用 app/desktop/node_modules/.bin/electron.cmd 启动：`electron.cmd . --dev --remote-debugging-port=<port>`
    （-NoSidecar 追加 --no-sidecar；CI 无 MediaCrawler，必须用该开关）。
  - 轮询 CDP（http://127.0.0.1:<port>/json/list）直到 renderer 的 index.html 目标出现。
  - 跑 e2e_smoke.js，finally 里按「启动 PID 进程树 + 命令行含该端口的 electron.exe」清理，
    并断言残留为 0（残留 >0 时退出码 1）。
  - 日志：build/e2e/<时间戳>/ 下 cdp.log / results.json / summary.json / electron.*.log / smoke.log。

  ⚠ 应用有单实例锁：运行前请先关闭本机已打开的 OpinionLens，否则新实例会立刻退出（脚本会打印提示）。

.PARAMETER Port
  CDP 调试端口；默认 0 = 自动挑一个空闲端口。

.PARAMETER NoSidecar
  不启动 Python sidecar（`--no-sidecar`）。CI 必须开启；此时 sidecar 相关断言记为 SKIP。

.PARAMETER TimeoutSeconds
  等待 CDP 端口就绪的上限（默认 90 秒）。

.EXAMPLE
  pwsh -File app/scripts/e2e/run_e2e.ps1 -NoSidecar

.EXAMPLE
  pwsh -File app/scripts/e2e/run_e2e.ps1
#>
#Requires -Version 7.0
[CmdletBinding()]
param(
  [int]$Port = 0,
  [switch]$NoSidecar,
  [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')).Path
$desktopDir = Join-Path $repoRoot 'app\desktop'
$electronCmd = Join-Path $desktopDir 'node_modules\.bin\electron.cmd'
$smokeScript = Join-Path $PSScriptRoot 'e2e_smoke.js'
$mode = if ($NoSidecar) { 'no-sidecar' } else { 'full' }

function Write-Step([string]$message) { Write-Host "[e2e] $message" }
function Write-Warn2([string]$message) { Write-Host "[e2e][warn] $message" -ForegroundColor Yellow }

# ---------------------------------------------------------------- 前置检查
if (-not (Test-Path -LiteralPath $electronCmd)) {
  throw "未找到 electron.cmd：$electronCmd（先在 app/desktop 执行 npm ci）"
}
if (-not (Test-Path -LiteralPath $smokeScript)) {
  throw "未找到 e2e_smoke.js：$smokeScript"
}
if (Test-Path Env:ELECTRON_RUN_AS_NODE) {
  Write-Step '移除环境变量 ELECTRON_RUN_AS_NODE（否则 Electron 以 Node 模式运行）'
  Remove-Item Env:ELECTRON_RUN_AS_NODE -ErrorAction SilentlyContinue
}

if ($Port -le 0) {
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
  $listener.Start()
  $Port = [int]$listener.LocalEndpoint.Port
  $listener.Stop()
  Write-Step "未指定 -Port，自动选择空闲端口 $Port"
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$artifactDir = Join-Path $repoRoot "build\e2e\$stamp"
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
$cdpLog = Join-Path $artifactDir 'cdp.log'
$resultJson = Join-Path $artifactDir 'results.json'
$summaryJson = Join-Path $artifactDir 'summary.json'
$stdoutLog = Join-Path $artifactDir 'electron.stdout.log'
$stderrLog = Join-Path $artifactDir 'electron.stderr.log'
$smokeLog = Join-Path $artifactDir 'smoke.log'

# ---------------------------------------------------------------- 进程辅助
function Get-ElectronIdsForPort([int]$port) {
  $pattern = "remote-debugging-port=$port(\s|$)"
  $ids = @()
  foreach ($procInfo in (Get-CimInstance Win32_Process -Filter "Name='electron.exe'" -ErrorAction SilentlyContinue)) {
    if ($procInfo.CommandLine -and $procInfo.CommandLine -match $pattern) {
      $ids += [int]$procInfo.ProcessId
    }
  }
  return $ids
}

function Get-DescendantIds([int[]]$roots) {
  $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
  $children = @{}
  foreach ($procInfo in $all) {
    $parent = [int]$procInfo.ParentProcessId
    if (-not $children.ContainsKey($parent)) { $children[$parent] = New-Object System.Collections.Generic.List[int] }
    $children[$parent].Add([int]$procInfo.ProcessId)
  }
  $found = New-Object System.Collections.Generic.List[int]
  $queue = New-Object System.Collections.Generic.Queue[int]
  foreach ($root in $roots) { if ($root -gt 0) { $queue.Enqueue($root) } }
  $seen = New-Object System.Collections.Generic.HashSet[int]
  foreach ($root in $roots) { if ($root -gt 0) { [void]$seen.Add($root) } }
  while ($queue.Count -gt 0) {
    $current = $queue.Dequeue()
    if (-not $children.ContainsKey($current)) { continue }
    foreach ($childId in $children[$current]) {
      if ($seen.Add($childId)) {
        $found.Add($childId)
        $queue.Enqueue($childId)
      }
    }
  }
  return $found
}

function Invoke-E2eCleanup([int]$launcherPid, [int]$port) {
  $candidates = New-Object System.Collections.Generic.HashSet[int]
  if ($launcherPid -gt 0) { [void]$candidates.Add($launcherPid) }
  foreach ($id in (Get-ElectronIdsForPort $port)) { [void]$candidates.Add($id) }
  $roots = @($candidates)
  foreach ($id in (Get-DescendantIds $roots)) { [void]$candidates.Add($id) }

  $killed = New-Object System.Collections.Generic.List[int]
  foreach ($id in $candidates) {
    $existing = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($existing) {
      try {
        Stop-Process -Id $id -Force -ErrorAction Stop
        $killed.Add($id)
      } catch {
        Write-Warn2 "结束进程 $id 失败：$($_.Exception.Message)"
      }
    }
  }

  $deadline = (Get-Date).AddSeconds(15)
  while ((Get-Date) -lt $deadline) {
    $left = @(Get-ElectronIdsForPort $port)
    $launcherAlive = $launcherPid -gt 0 -and [bool](Get-Process -Id $launcherPid -ErrorAction SilentlyContinue)
    if ($left.Count -eq 0 -and -not $launcherAlive) { break }
    Start-Sleep -Milliseconds 300
  }

  $leftover = @(Get-ElectronIdsForPort $port)
  $launcherAlive = $launcherPid -gt 0 -and [bool](Get-Process -Id $launcherPid -ErrorAction SilentlyContinue)
  return [pscustomobject]@{
    killed          = @($killed)
    leftoverPids    = @($leftover)
    launcherAlive   = [bool]$launcherAlive
    globalElectron  = @(Get-Process electron -ErrorAction SilentlyContinue).Count
  }
}

# ---------------------------------------------------------------- 启动 + 执行
$startedAt = Get-Date
$launchPid = 0
$nodeExit = 1
$earlyError = ''
$cdpReady = $false
$cleanup = $null
$resultsPayload = $null

try {
  $electronArgs = @('.', '--dev', "--remote-debugging-port=$Port")
  if ($NoSidecar) { $electronArgs += '--no-sidecar' }
  $commandLine = 'node_modules\.bin\electron.cmd ' + ($electronArgs -join ' ')
  Write-Step "启动：cmd /d /s /c `"$commandLine`"（cwd=$desktopDir）"

  $proc = Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/d', '/s', '/c', $commandLine) `
    -WorkingDirectory $desktopDir `
    -PassThru `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog
  $launchPid = [int]$proc.Id
  Write-Step "Electron 启动器 PID=$launchPid，等待 CDP 端口 $Port（上限 ${TimeoutSeconds}s）"

  $deadline = (Get-Date).AddSeconds([Math]::Max(10, $TimeoutSeconds))
  $cdpBody = $null
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 400
    if ($proc.HasExited) { break }
    try {
      # 只用原始响应做就绪判断（目标发现交给 e2e_smoke.js，避免 PowerShell 反序列化数组的形态差异）
      $cdpBody = (Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/list" -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop).Content
    } catch {
      $cdpBody = $null
    }
    if ($cdpBody -and $cdpBody -match 'index\.html' -and $cdpBody -match 'webSocketDebuggerUrl') { break }
    $cdpBody = $null
  }

  if (-not $cdpBody) {
    $hint = 'CDP 端口未就绪'
    if ($proc.HasExited) {
      $hint = "Electron 启动器已退出（exit=$($proc.ExitCode)）；常见原因：本机已有 OpinionLens 实例占用单实例锁，请先关闭应用"
    }
    $tailOut = if (Test-Path $stdoutLog) { (Get-Content -LiteralPath $stdoutLog -Tail 10 -ErrorAction SilentlyContinue) -join ' | ' } else { '' }
    $tailErr = if (Test-Path $stderrLog) { (Get-Content -LiteralPath $stderrLog -Tail 10 -ErrorAction SilentlyContinue) -join ' | ' } else { '' }
    throw "$hint。stdout=$tailOut stderr=$tailErr"
  }
  $cdpReady = $true
  Write-Step "CDP /json/list 已出现 renderer 目标（index.html），端口 $Port 就绪"

  $env:E2E_MODE = $mode
  $env:E2E_CDP_LOG = $cdpLog
  $env:E2E_RESULT_PATH = $resultJson
  Write-Step "执行 e2e_smoke.js（mode=$mode）"
  & node $smokeScript $Port 2>&1 | Tee-Object -FilePath $smokeLog
  $nodeExit = $LASTEXITCODE
  if ($null -eq $nodeExit) { $nodeExit = 1 }
} catch {
  $earlyError = $_.Exception.Message
  Write-Host "[e2e][fail] $earlyError" -ForegroundColor Red
} finally {
  Write-Step '清理 Electron / sidecar 进程'
  $cleanup = Invoke-E2eCleanup -launcherPid $launchPid -port $Port
  if ($cleanup.leftoverPids.Count -gt 0) {
    Write-Host "[e2e][fail] 仍有 electron 进程占用端口 ${Port}：$($cleanup.leftoverPids -join ', ')" -ForegroundColor Red
  } else {
    Write-Step "进程清理完成（killed=$($cleanup.killed.Count)，端口残留=0，全局 electron.exe=$($cleanup.globalElectron)）"
  }

  if (Test-Path -LiteralPath $resultJson) {
    try { $resultsPayload = Get-Content -LiteralPath $resultJson -Raw | ConvertFrom-Json } catch { $resultsPayload = $null }
  }
  $counts = [ordered]@{ pass = 0; fail = 0; skip = 0; total = 0 }
  $resultRows = @()
  if ($resultsPayload -and $resultsPayload.counts) {
    $counts.pass = [int]$resultsPayload.counts.pass
    $counts.fail = [int]$resultsPayload.counts.fail
    $counts.skip = [int]$resultsPayload.counts.skip
    $counts.total = $counts.pass + $counts.fail + $counts.skip
  }
  if ($resultsPayload -and $resultsPayload.results) { $resultRows = @($resultsPayload.results) }

  $finishedAt = Get-Date
  $finalExit = if ($earlyError) { 1 } elseif ($nodeExit -ne 0) { [int]$nodeExit } elseif ($cleanup.leftoverPids.Count -gt 0 -or $cleanup.launcherAlive) { 1 } else { 0 }

  $summary = [ordered]@{
    mode              = $mode
    port              = $Port
    timeoutSeconds    = $TimeoutSeconds
    startedAt         = $startedAt.ToString('o')
    finishedAt        = $finishedAt.ToString('o')
    durationMs        = [int]($finishedAt - $startedAt).TotalMilliseconds
    exitCode          = $finalExit
    nodeExitCode      = [int]$nodeExit
    cdpReady          = $cdpReady
    error             = $earlyError
    counts            = $counts
    results           = $resultRows
    cleanup           = [ordered]@{
      killedIds      = $cleanup.killed
      leftoverPids   = $cleanup.leftoverPids
      launcherAlive  = $cleanup.launcherAlive
      globalElectron = $cleanup.globalElectron
    }
    artifacts         = [ordered]@{
      directory  = $artifactDir
      cdpLog     = $cdpLog
      results    = $resultJson
      summary    = $summaryJson
      smokeLog   = $smokeLog
      stdoutLog  = $stdoutLog
      stderrLog  = $stderrLog
    }
  }
  try {
    ($summary | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $summaryJson -Encoding utf8
  } catch {
    Write-Warn2 "写入 summary.json 失败：$($_.Exception.Message)"
  }

  Write-Host ''
  Write-Host ("[e2e] 结果：mode={0} pass={1} fail={2} skip={3} exit={4}" -f $mode, $counts.pass, $counts.fail, $counts.skip, $finalExit)
  Write-Host "[e2e] 产物：$artifactDir"

  $script:FinalExit = $finalExit
}

if ($null -eq $script:FinalExit) { $script:FinalExit = 1 }
exit $script:FinalExit
