# =============================================================================
# 版本号一键升级（OpinionLens）
#
# 版本号唯一来源是 app/desktop/package.json；本脚本把它同步到所有需要跟随的位置，
# 避免出现「界面显示 0.6.0 / package.json 0.2.2 / 报告 0.3.0」这种三处不一致。
#
# 用法（仓库根目录）：
#   pwsh -NoProfile -File scripts/bump_version.ps1 -Version 0.4.1
#   pwsh -NoProfile -File scripts/bump_version.ps1 -Version 0.5.0 -SkipChangelog
#
# 同步范围：
#   1. app/desktop/package.json            "version"（唯一来源）
#   2. app/desktop/package-lock.json        顶部与 packages[""].version（前两处）
#   3. app/sidecar/version.py              FALLBACK_VERSION（sidecar 缺失 FS_APP_VERSION 时的回退）
#   4. README.md                           徽章 release-<ver>-blue
#   5. CHANGELOG.md                        顶部插入新版本小节（可 -SkipChangelog）
#
# 升级后建议按顺序跑：
#   .\app\scripts\selftest.ps1        # 其中「版本号一致性」断言会校验 package.json 与 version.py 一致
#   .\app\scripts\build_installer.ps1 # 出安装包（NSIS）
#   git commit && git tag v<ver> && git push --tags
# =============================================================================
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Date = (Get-Date -Format 'yyyy-MM-dd'),
    [switch]$SkipChangelog,
    [string]$Summary = '（待补充）'
)

$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $PSScriptRoot
function Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
function Note([string]$Text) { Write-Host "    $Text" -ForegroundColor DarkGray }

$target = $Version.Trim().TrimStart('v', 'V')
if ($target -notmatch '^\d+\.\d+\.\d+([\-+][0-9A-Za-z\.\-]+)?$') {
    throw "版本号格式不合法：$Version（应形如 0.4.0 / 0.4.0-beta.1）"
}

$pkgPath = Join-Path $Project 'app\desktop\package.json'
$lockPath = Join-Path $Project 'app\desktop\package-lock.json'
$verPyPath = Join-Path $Project 'app\sidecar\version.py'
$readmePath = Join-Path $Project 'README.md'
$changelogPath = Join-Path $Project 'CHANGELOG.md'
foreach ($p in @($pkgPath, $lockPath, $verPyPath, $readmePath)) {
    if (-not (Test-Path $p)) { throw "缺少文件：$p" }
}

# ---------------------------------------------------------------- 1. package.json
$pkgRaw = Get-Content $pkgPath -Raw -Encoding utf8
if ($pkgRaw -notmatch '"version"\s*:\s*"([^"]+)"') { throw 'package.json 里找不到 version 字段' }
$oldVersion = $Matches[1]
if ($oldVersion -eq $target) { throw "版本号已经是 $target，无需修改" }
Step "package.json：$oldVersion -> $target"
$pkgNew = [regex]::Replace($pkgRaw, '"version"\s*:\s*"[^"]+"', '"version": "' + $target + '"', 1)
Set-Content -LiteralPath $pkgPath -Value $pkgNew -Encoding utf8 -NoNewline

# ---------------------------------------------------------------- 2. package-lock.json
$lockRaw = Get-Content $lockPath -Raw -Encoding utf8
$script:lockIndex = 0
$lockNew = [regex]::Replace($lockRaw, '"version"\s*:\s*"[^"]+"', {
        param($m)
        $script:lockIndex++
        if ($script:lockIndex -le 2) { return '"version": "' + $target + '"' }
        return $m.Value
    })
if ($script:lockIndex -lt 2) { throw "package-lock.json 里 version 字段少于 2 处（实际 $($script:lockIndex)），请检查文件" }
Step "package-lock.json：前 2 处 version -> $target"
Set-Content -LiteralPath $lockPath -Value $lockNew -Encoding utf8 -NoNewline

# ---------------------------------------------------------------- 3. app/sidecar/version.py
$verRaw = Get-Content $verPyPath -Raw -Encoding utf8
if ($verRaw -notmatch 'FALLBACK_VERSION\s*=\s*"([^"]+)"') { throw 'version.py 里找不到 FALLBACK_VERSION' }
Step "app/sidecar/version.py：$($Matches[1]) -> $target"
$verNew = [regex]::Replace($verRaw, 'FALLBACK_VERSION\s*=\s*"[^"]+"', 'FALLBACK_VERSION = "' + $target + '"', 1)
Set-Content -LiteralPath $verPyPath -Value $verNew -Encoding utf8 -NoNewline

# ---------------------------------------------------------------- 4. README 徽章
$readmeRaw = Get-Content $readmePath -Raw -Encoding utf8
if ($readmeRaw -match 'badge/release-[\d\.]+-blue') {
    Step "README 徽章：release-$target"
    $readmeNew = [regex]::Replace($readmeRaw, 'badge/release-[\d\.]+-blue', 'badge/release-' + $target + '-blue', 1)
    Set-Content -LiteralPath $readmePath -Value $readmeNew -Encoding utf8 -NoNewline
}
else {
    Note 'README 里没找到 release 徽章，跳过'
}

# ---------------------------------------------------------------- 5. CHANGELOG
if (-not $SkipChangelog) {
    $lines = Get-Content $changelogPath -Encoding utf8
    $anchor = -1
    for ($i = 0; $i -lt [Math]::Min(20, $lines.Count); $i++) {
        if ($lines[$i] -match '^##\s+') { $anchor = $i; break }
    }
    if ($anchor -lt 0) { $anchor = 1 }
    $stub = @(
        "## $target · $Date",
        '',
        '**新增**',
        "- $Summary",
        '',
        ''
    )
    Step "CHANGELOG.md：在第一个版本小节前插入 $target 条目"
    $out = @()
    $out += $lines[0..($anchor - 1)]
    $out += $stub
    if ($anchor -lt $lines.Count) { $out += $lines[$anchor..($lines.Count - 1)] }
    Set-Content -LiteralPath $changelogPath -Value $out -Encoding utf8
}
else {
    Note '按 -SkipChangelog 跳过 CHANGELOG'
}

# ---------------------------------------------------------------- 汇总
Step '完成'
Note "应用版本：$oldVersion -> $target"
Note '提示：'
Note '  1) 跑 .\app\scripts\selftest.ps1（含「版本号一致性」断言）'
Note '  2) 跑 .\app\scripts\build_installer.ps1 重新出安装包'
Note "  3) git commit && git tag v$target && git push --tags；GitHub Release 用 tag 名（v$target）"
Note '  4) 应用内「设置 → 关于 → 软件更新」比较的就是 package.json 的 version 与 Release tag'
