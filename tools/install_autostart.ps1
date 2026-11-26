# 注册开机自启（登录时自动后台启动，不弹窗口）
#
# 为什么需要：别人家的平台是"服务"，开机就在跑，用户只需打开浏览器。
# Windows 没有 systemd，两种替代：
#   1) 计划任务（schtasks）—— 干净，但部分机器策略会拒绝（实测本机 Access denied）
#   2) 启动文件夹放一个 .vbs —— 不需要管理员，登录即跑（本脚本的兜底方案）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove
#
# 注意：第一版不检查 schtasks 退出码，Access denied 时仍然打印"已注册"，
# 于是用户以为装好了、重启发现没启动——这就是"静默失败"，现在必须先判返回码再宣称成功。

param(
    [int]$Port = 8770,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$py = Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$taskName = "InterviewAgent-$Port"
$startupDir = [Environment]::GetFolderPath("Startup")
$vbsPath = Join-Path $startupDir "InterviewAgent-$Port.vbs"

if ($Remove) {
    schtasks /Delete /TN $taskName /F 2>$null | Out-Null
    if (Test-Path $vbsPath) { Remove-Item $vbsPath -Force }
    Write-Host "已移除：计划任务 $taskName（若存在）+ 启动项 $vbsPath"
    exit 0
}

# ---- 方案 1：计划任务（需要相应策略允许）
# 注意：schtasks 把 "Access is denied" 写到 stderr，在 $ErrorActionPreference="Stop" 下
# 会直接抛终止错误，导致下面的兜底方案根本执行不到（实测）。所以这里必须局部放宽。
$taskOk = $false
try {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    schtasks /Create /TN $taskName /SC ONLOGON /RL LIMITED /F `
        /TR "cmd /c cd /d `"$root`" && `"$py`" `"$root\tools\service.py`" start --port $Port" 2>&1 | Out-Null
    $taskOk = ($LASTEXITCODE -eq 0)
} catch {
    $taskOk = $false
} finally {
    $ErrorActionPreference = $prev
}

if ($taskOk) {
    Write-Host "已注册计划任务：$taskName（登录时自动启动，无窗口）"
} else {
    Write-Host "计划任务注册被拒（退出码 $LASTEXITCODE，通常是权限/策略限制），改用启动文件夹方案…"
    # ---- 方案 2：启动文件夹里的 .vbs，不需要管理员，静默启动
    $vbs = @"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$root"
sh.Run """$py"" ""$root\tools\service.py"" start --port $Port", 0, False
"@
    # 必须用 Unicode(UTF-16)：项目路径里有中文，ASCII/ANSI 写进 .vbs 会变成乱码，
    # cscript 报 "filename, directory name, or volume label syntax is incorrect"（实测踩过一次）
    Set-Content -Path $vbsPath -Value $vbs -Encoding Unicode
    if (Test-Path $vbsPath) {
        Write-Host "已写入启动项：$vbsPath"
        Write-Host "  （下次登录 Windows 时静默启动；不需要管理员权限）"
    } else {
        Write-Host "写入启动项失败，请手动把下面命令加入启动项：" -ForegroundColor Red
        Write-Host "  `"$py`" `"$root\tools\service.py`" start --port $Port"
        exit 1
    }
}

Write-Host ""
Write-Host "端口：$Port"
Write-Host "立即启动一次：python tools\service.py start --port $Port"
Write-Host "卸载：powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove"
