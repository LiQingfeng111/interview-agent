# ============================================================================
#  interview-agent 交付状态汇总
#
#  ## 为什么用 .ps1 而不是 .bat（第三次踩同一个坑）
#
#  这个任务我先写了 .bat 两次，都因为编码/解析问题失败：
#    第一次：中文注释被 cmd 当命令执行（代码页 936 vs UTF-8）
#    第二次：改成纯 ASCII，但 `setlocal enabledelayedexpansion` 里的
#           `!VAR!` 语法和 `for /f` 嵌套在 PowerShell 转调时被破坏
#
#  结论：**bat 不适合这种要拼接多段输出的脚本**。
#  PowerShell 对 UTF-8 中文、字符串格式化、JSON 解析都友好得多，
#  而且 gh CLI 的 --json / --jq 能直接用。
#
#  ## 用法
#
#    powershell -ExecutionPolicy Bypass -File tools\status.ps1
#
#  注意：本文件必须保存为 UTF-8 with BOM
#  （Windows PowerShell 5.1 会把无 BOM 的 UTF-8 按 ANSI 解码，中文变乱码）
# ============================================================================

$ErrorActionPreference = 'Continue'

$Repo   = 'LiQingfeng111/interview-agent'
$Root   = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Line { param($Char = '=') Write-Host ($Char * 60) }

Line
Write-Host '  interview-agent  交付状态' -ForegroundColor Cyan
Line
Write-Host ''

# ---------------------------------------------------------------- 同步状态
Write-Host '▸ 同步状态' -ForegroundColor Cyan
$local  = (git rev-parse HEAD 2>$null)
$remote = (git rev-parse origin/main 2>$null)
$count  = (git rev-list --count HEAD 2>$null)

if ($local -and $remote -and $local -eq $remote) {
    Write-Host ("  ✓ 本地与远端一致（{0}）" -f $local.Substring(0, 8)) -ForegroundColor Green
} else {
    Write-Host ("  ✗ 不一致  本地={0} 远端={1}" -f `
        $(if ($local) { $local.Substring(0,8) } else { '?' }), `
        $(if ($remote) { $remote.Substring(0,8) } else { '?' })) -ForegroundColor Red
}
Write-Host ("  提交数: {0}" -f $count)
Write-Host ''

# ---------------------------------------------------------------- CI
Write-Host '▸ CI（最近 3 次）' -ForegroundColor Cyan
$runs = & gh run list --repo $Repo --limit 3 `
    --json conclusion,displayTitle,createdAt 2>$null | ConvertFrom-Json

if ($runs) {
    foreach ($r in $runs) {
        $mark = if ($r.conclusion -eq 'success') { '✓' } else { '✗' }
        $color = if ($r.conclusion -eq 'success') { 'Green' } else { 'Red' }
        # 标题里的中文在控制台可能显示不全，截断一下
        $title = $r.displayTitle
        if ($title.Length -gt 44) { $title = $title.Substring(0, 44) + '…' }
        Write-Host ("  $mark {0,-8} {1}" -f $r.conclusion, $title) -ForegroundColor $color
    }
} else {
    Write-Host '  （拿不到 CI 记录）' -ForegroundColor Yellow
}
Write-Host ''

# ---------------------------------------------------------------- badge
Write-Host '▸ README badge' -ForegroundColor Cyan
$ciBadge = "https://github.com/$Repo/actions/workflows/ci.yml/badge.svg"
try {
    $svg = (Invoke-WebRequest -Uri $ciBadge -TimeoutSec 25 -UseBasicParsing).Content
    if ($svg -is [byte[]]) { $svg = [Text.Encoding]::UTF8.GetString($svg) }
    if ($svg -match '>passing<') { $state = 'passing'; $c = 'Green' }
    elseif ($svg -match '>failing<') { $state = 'failing'; $c = 'Red' }
    else { $state = 'unknown'; $c = 'Yellow' }
    Write-Host ("  CI: {0}" -f $state) -ForegroundColor $c
} catch {
    Write-Host '  CI: 取不到（网络问题）' -ForegroundColor Yellow
}

$shields = @(
    'python-3.12-blue',
    'vue-3-42b883',
    'license-MIT-green'
)
$ok = 0
foreach ($s in $shields) {
    try {
        $r = Invoke-WebRequest -Uri "https://img.shields.io/badge/$s" `
            -TimeoutSec 20 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok++ }
    } catch { }
}
Write-Host ("  shields: {0}/{1} 可访问" -f $ok, $shields.Count)
Write-Host ''

# ---------------------------------------------------------------- 截图
Write-Host '▸ 截图' -ForegroundColor Cyan
$imgs = Get-ChildItem 'docs\images' -Filter '*.png' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch '^readme-' }
Write-Host ("  {0} 张（README 全部引用）" -f $imgs.Count)
$imgs | ForEach-Object { Write-Host ("    {0}" -f $_.Name) }
Write-Host ''

# ---------------------------------------------------------------- 密钥
Write-Host '▸ 密钥审计' -ForegroundColor Cyan

# 本地追踪文件
$envFile = Join-Path $Root '.env'
$leak = 0
if (Test-Path $envFile) {
    $key = (Select-String -Path $envFile -Pattern '^LLM_API_KEY=(.+)$' |
            Select-Object -First 1).Matches.Groups[1].Value
    if ($key -and $key.Length -ge 16) {
        $prefix = $key.Substring(0, 16)
        $tracked = git ls-files 2>$null
        foreach ($f in $tracked) {
            $full = Join-Path $Root $f
            if ((Test-Path $full) -and (Get-Item $full).Length -lt 1MB) {
                try {
                    if ((Get-Content $full -Raw -ErrorAction Stop).Contains($prefix)) {
                        Write-Host ("  !!! {0}" -f $f) -ForegroundColor Red
                        $leak++
                    }
                } catch { }
            }
        }
    }
}
if ($leak -eq 0) {
    Write-Host '  ✓ 本地追踪文件无密钥' -ForegroundColor Green
}

# 远端 .env
try {
    $r = Invoke-WebRequest -Uri "https://raw.githubusercontent.com/$Repo/main/.env" `
        -TimeoutSec 15 -UseBasicParsing -ErrorAction Stop
    Write-Host '  ✗ 远端存在 .env（危险）' -ForegroundColor Red
} catch {
    if ($_.Exception.Response.StatusCode.value__ -eq 404) {
        Write-Host '  ✓ 远端无 .env' -ForegroundColor Green
    } else {
        Write-Host '  ? 远端 .env 检查失败（网络）' -ForegroundColor Yellow
    }
}
Write-Host ''

Line
Write-Host ("  https://github.com/{0}" -f $Repo) -ForegroundColor Cyan
Line
