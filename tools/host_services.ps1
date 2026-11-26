# ============================================================================
#  interview-agent 宿主机服务（可被 dot-source 的核心逻辑）
#
#  ## 为什么要拆成"可 dot-source"的库而不是独立脚本
#
#  踩过的坑：`install_host_autostart.ps1` 里想"先跑一次再注册自启"，
#  用子进程调本文件，遇到两个 Windows 特有的坑：
#
#    ① `& powershell -File ...`：子进程非零退出码会让父脚本提前终止
#    ② `Start-Process -Wait`：本机环境变量里同时有 `NO_PROXY` / `no_proxy`，
#       PowerShell 复制环境块时大小写冲突 → `Item has already been added`
#    ③ `cmd /c start /wait`：引号语法导致窗口一直挂着不返回
#
#  结论：**别在 PowerShell 里套 PowerShell**。
#  把逻辑写成函数，需要的地方 `& "$root\tools\host_services.ps1"` 直接跑，
#  或者 `. .\host_services.ps1` dot-source 后调函数。
#
#  ## 用法
#
#    # 独立运行
#    powershell -ExecutionPolicy Bypass -File tools\host_services.ps1
#
#    # 在别的脚本里复用
#    . "$root\tools\host_services.ps1"
#    Start-HostServices
#
#  注意：本文件必须保存为 UTF-8 with BOM
#  （Windows PowerShell 5.1 把无 BOM 的 UTF-8 按 ANSI 解码 → 中文路径乱码 + 语法错误）
# ============================================================================

$script:OllamaPort = 11800
$script:FwdListen  = 18336
$script:FwdTarget  = 18326

function Start-HostServices {
    param(
        [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
        [switch]$Quiet
    )

    $ollamaPort = $script:OllamaPort
    $fwdListen  = $script:FwdListen
    $fwdTarget  = $script:FwdTarget
    $ollamaExe  = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"

    function Say($msg, $color = 'Gray') {
        if (-not $Quiet) { Write-Host $msg -ForegroundColor $color }
    }

    Say ""
    Say "=== interview-agent 宿主机服务 ===" Cyan
    Say ""

    # ------------------------------------------------------------ Ollama
    Say "[1/2] Ollama（GPU 向量化，必须监听 0.0.0.0:$ollamaPort）"

    $running = Get-Process ollama -ErrorAction SilentlyContinue
    if ($running) {
        # 已运行但可能监听在 127.0.0.1 —— 外部（WSL/容器）访问不到，
        # 所以必须重启成 0.0.0.0。这是踩过的坑：直接跳过会导致
        # embedding 从 WSL 侧完全不可用。
        Say "  已在运行，重启为 0.0.0.0:$ollamaPort"
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    if (-not (Test-Path $ollamaExe)) {
        Say "  [错误] 找不到 ollama.exe：$ollamaExe" Red
    } else {
        # OLLAMA_HOST 通过 cmd 的 set 传给子进程最稳
        # （直接用 $env: 时 ollama 偶尔读不到，实测）
        $cmd = "set OLLAMA_HOST=0.0.0.0:$ollamaPort && `"$ollamaExe`" serve"
        Start-Process -FilePath 'cmd.exe' -ArgumentList "/c $cmd" `
            -WindowStyle Hidden -ErrorAction SilentlyContinue
        Say "  已启动，等待就绪…"

        $ok = $false
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Seconds 1
            try {
                $r = Invoke-WebRequest -Uri "http://127.0.0.1:$ollamaPort/api/tags" `
                    -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
                if ($r.StatusCode -eq 200) {
                    $n = ($r.Content | ConvertFrom-Json).models.Count
                    Say "  ✓ 就绪，模型数 $n" Green
                    $ok = $true
                    break
                }
            } catch { }
        }
        if (-not $ok) {
            Say "  [警告] 未就绪，embedding 会失败（语义检索不可用）" Yellow
        }
    }

    # ------------------------------------------------------------ 端口转发
    Say ""
    Say "[2/2] 模型网关端口转发 $fwdListen -> $fwdTarget"

    $listening = Get-NetTCPConnection -LocalPort $fwdListen -State Listen `
        -ErrorAction SilentlyContinue
    if ($listening) {
        Say "  已在监听" Green
    } else {
        $fwd = Join-Path $ProjectRoot 'tools\port_forward.cjs'
        if (-not (Test-Path $fwd)) {
            Say "  [错误] 找不到 $fwd" Red
        } else {
            Start-Process -FilePath 'node' -ArgumentList $fwd, $fwdListen, $fwdTarget `
                -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
                -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 3
            $check = Get-NetTCPConnection -LocalPort $fwdListen -State Listen `
                -ErrorAction SilentlyContinue
            if ($check) {
                Say "  ✓ 已启动" Green
            } else {
                Say "  [警告] 启动失败，检查 node 是否在 PATH" Yellow
            }
        }
    }

    # ------------------------------------------------------------ 总结
    Say ""
    $wslIp = (wsl -d Ubuntu -e bash -lc "ip route show default | awk '{print `$3}'" 2>$null)
    if ($wslIp) {
        Say "WSL 访问宿主机请用：$wslIp" Cyan
        Say "  Ollama    http://${wslIp}:$ollamaPort"
        Say "  模型网关  http://${wslIp}:$fwdListen/v1"
    }
    Say ""
}

# 直接运行本文件时执行
if ($MyInvocation.InvocationName -ne '.') {
    Start-HostServices
}
