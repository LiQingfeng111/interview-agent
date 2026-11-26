# ============================================================================
#  interview-agent 一键部署：启动宿主机服务 + 注册开机自启
#
#  ## 为什么需要开机自启
#
#  Ollama（GPU 推理）和模型网关的端口转发都在 Windows 侧，且只监听
#  127.0.0.1。WSL 里的后端访问不到 —— 每次重启机器都要手动敲两条命令，
#  忘了就会看到 "embedding 降级 / 模型连不上"。
#
#  ## 实现方式
#
#  用 Windows「启动」文件夹放一个 .vbs（登录即跑、不需要管理员）。
#  没走 `schtasks` 是因为本机策略会 Access denied（沿用
#  tools/install_autostart.ps1 里已验证过的方案）。
#
#  ## 用法
#
#    安装 + 立即启动：powershell -ExecutionPolicy Bypass -File tools\install_host_autostart.ps1
#    卸载：          powershell -ExecutionPolicy Bypass -File tools\install_host_autostart.ps1 -Remove
#    只跑不装：      powershell -ExecutionPolicy Bypass -File tools\install_host_autostart.ps1 -Once
#
#  注意：必须保存为 UTF-8 with BOM（PowerShell 5.1 的 ANSI 解码会把中文路径搞坏）
# ============================================================================

param(
    [switch]$Remove,
    [switch]$Once
)

$ErrorActionPreference = 'Continue'

$root       = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$coreScript = Join-Path $root 'tools\host_services.ps1'
$startupDir = [Environment]::GetFolderPath('Startup')
$vbsPath    = Join-Path $startupDir 'interview-agent-host.vbs'

# ---------------------------------------------------------------- 卸载
if ($Remove) {
    if (Test-Path $vbsPath) {
        Remove-Item $vbsPath -Force
        Write-Host "已移除启动项：$vbsPath" -ForegroundColor Green
    } else {
        Write-Host "启动项不存在，无需移除"
    }
    exit 0
}

if (-not (Test-Path $coreScript)) {
    Write-Host "[错误] 找不到 $coreScript" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 立即启动
# 关键：**dot-source** 而不是起子进程。
#
# 踩过的坑（三个都试过，都不行）：
#   ① `& powershell -File host_services.ps1`
#      子进程的非零退出码会让父脚本提前终止，后面的注册逻辑不执行
#   ② `Start-Process -Wait ...`
#      本机环境变量同时有 NO_PROXY / no_proxy，PowerShell 复制环境块
#      时大小写冲突 → "Item has already been added"
#   ③ `cmd /c start /wait ...`
#      引号语法导致窗口挂着不返回（实测卡了 10 分钟）
#
# dot-source 是在当前进程里加载函数定义，最干净、没有子进程开销。
. $coreScript
Start-HostServices -ProjectRoot $root

if ($Once) {
    Write-Host ""
    Write-Host "（-Once 模式，不注册开机自启）"
    exit 0
}

# ---------------------------------------------------------------- 注册自启
Write-Host ""
Write-Host "=== 注册开机自启 ===" -ForegroundColor Cyan

# .vbs 用 WScript.Shell 静默运行（0 = 隐藏窗口）。
# 必须用 Unicode(UTF-16) 写：路径含中文，ANSI 会乱码（实测过）。
$vbs = @"
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File ""$coreScript""", 0, False
"@

try {
    Set-Content -Path $vbsPath -Value $vbs -Encoding Unicode -ErrorAction Stop
    Write-Host "  ✓ 已写入启动项：$vbsPath" -ForegroundColor Green
    Write-Host "    下次登录 Windows 会静默启动 Ollama + 端口转发"
    Write-Host ""
    Write-Host "  卸载：powershell -ExecutionPolicy Bypass -File tools\install_host_autostart.ps1 -Remove"
} catch {
    Write-Host "  ✗ 写入失败：$_" -ForegroundColor Red
    Write-Host "  手动把下面这行加入启动项：" -ForegroundColor Yellow
    Write-Host "  powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$coreScript`""
    exit 1
}
