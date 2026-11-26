# 为什么别人家的平台没有那个黑窗口

## 一、一句话

**别人跑的是"服务"，我们跑的是"前台进程"。**
黑窗口不是界面的附属品，它是那个进程的**生命周期本身**——窗口关掉，进程结束，网页就打不开了。

---

## 二、四种形态的区别（这才是问题所在）

| 形态 | 谁在托管进程 | 用户看到什么 | 关掉终端会怎样 |
|---|---|---|---|
| 前台控制台（我们之前） | 你自己 | 一个黑窗口 | **服务死掉** |
| 后台进程 + 启动项（我们现在） | Windows 登录项 | 什么都没有 | 不受影响 |
| Docker 容器 | Docker 守护进程（开机自启） | 什么都没有 | 不受影响 |
| 云主机 + systemd | init（PID 1） | 什么都没有 | 不受影响 |

别人的项目 README 第一行往往写的是：

```bash
docker compose up -d      # -d = detached，脱离控制台
```

那个 `-d` 就是"没有窗口"的全部秘密：**进程交给守护进程托管**。
他们的终端只用来安装一次，之后由 Docker/systemd 负责重启、开机自启、日志收集。

我们没有 Docker（本机没装），所以之前只能前台跑——**窗口就是进程本身**。

---

## 三、这一版怎么解决的（已实测）

### 1. `tools/service.py`：起 / 停 / 查

```powershell
python tools/service.py start     # 后台启动（DETACHED_PROCESS + CREATE_NO_WINDOW）
python tools/service.py status    # PID / 健康检查 / 访问地址 / 日志路径
python tools/service.py stop      # 按 pid 文件精确停止
python tools/service.py restart
```

实测：

```
已启动（后台，无窗口）：PID 27696
访问：http://127.0.0.1:8770  （会先跳 /login）
日志：logs/webui.log
```

窗口关闭后再查：`PID 27696 存活 / api/health 200 OK` —— **进程活着，网页还在**。

### 2. 双击即用（不弹窗）

| 文件 | 作用 |
|---|---|
| `7-start-background.bat` | 后台启动 + 自动开浏览器，窗口可以关掉 |
| `8-stop-background.bat` | 停止后台服务 |

### 3. 开机自启（真·不用管）

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1
# 卸载
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Remove
```

两条路，按权限自动选：

1. **计划任务**（`schtasks /SC ONLOGON`）—— 干净，但本机策略拒绝了（`Access is denied`）
2. **启动文件夹 `.vbs`** —— 不需要管理员，登录即静默启动

实测本机走的是第 2 条，已写入：

```
C:\Users\L2522\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\InterviewAgent-8770.vbs
```

用 `cscript` 模拟"下次登录"跑了一遍：`PID 27696 存活 / 200 OK`。

---

## 四、这一路上踩的三个坑（都不是功能问题，是"静默失败"）

| 坑 | 现象 | 原因 |
|---|---|---|
| `pythonw` 拿不到输出 | 日志只有一行 header，启动失败看不出来 | pythonw 没有 stdout；改用 `python.exe + CREATE_NO_WINDOW` |
| 日志永远是空的 | 进程明明在跑 | 子进程 stdout 块缓冲；加 `PYTHONUNBUFFERED=1` |
| 启动项写了却起不来 | `cscript` 报 "filename ... syntax is incorrect" | `.vbs` 用 ASCII 写入，项目路径含中文被打乱；改用 UTF-16（`-Encoding Unicode`） |
| 注册失败却说成功 | `schtasks` 返回 Access denied，脚本仍打印"已注册" | `$ErrorActionPreference="Stop"` 下未判返回码；现在先判 `$LASTEXITCODE` 再宣称成功 |

**共同点**：它们都不会让测试变红，只会让"用户以为好了，其实没好"。

---

## 五、还差什么（诚实）

- **没有进程守护**：进程崩溃不会自动重启。Docker/systemd 有 `restart: unless-stopped`，我们只有"开机启动"
- **日志不会轮转**：`logs/webui.log` 会一直长；长期跑需要按天切分
- **单机单进程**：多人真的同时用时，会话仍存在各自进程内存里
- **镜像未构建**：`Dockerfile` 在，本机没 Docker，`docker compose up -d` 这条"最正统的路"没验证过

---

## 六、后来：Docker 真的装上了（Windows → WSL → 容器）

前面那节说的"本机没 Docker"，后来被推翻了一半：

```powershell
# 这条路失败：非管理员会话，winget 卡在提权，12 分钟 CPU 只跑 1.7 秒
winget install --id Docker.DockerDesktop

# 这条路成功：WSL 里的 Ubuntu 不需要 Windows 管理员权限
wsl -d Ubuntu -u root -e apt-get install -y docker.io docker-compose-v2
wsl -d Ubuntu -u root -e systemctl enable --now docker
```

装上之后 `9-docker-up.bat` 就是完整的托管路径，实测：

```
docker ps → interview-agent | Up (healthy)
Windows  → curl 127.0.0.1:8770/api/health → {"ok": true}
容器日志 → [LLM] 已连接本地模型：lobsterai/deepseek-flash · http://host.docker.internal:18326/v1
重启测试 → systemctl restart docker 后容器自动回到 healthy（restart: unless-stopped）
```

**这才是"关掉一切也不影响"的形态**：进程的父级不是任何终端，而是 dockerd；
dockerd 由 systemd 托管；systemd 随 WSL 启动。

### 装的过程踩的四个坑

| 坑 | 现象 | 原因与解法 |
|---|---|---|
| winget 装 Docker Desktop 卡死 | 12 分钟无输出、CPU 1.7s、无 docker 命令 | 安装程序要 UAC 提权；本会话审批被禁用且账号非管理员 → 改走 WSL |
| WSL 里 DNS 全废 | `lookup registry-1.docker.io: no such host` | `/etc/resolv.conf` 指向 `10.255.255.254`（NAT 模式不支持 localhost 代理）→ `/etc/wsl.conf` 关自动生成 + 写 `223.5.5.5` |
| Docker Hub 连不上 | `dial tcp 192.133.77.59:443: i/o timeout` | 直连被墙 → `/etc/docker/daemon.json` 配 `registry-mirrors`（daocloud、1ms.run） |
| 强杀 WSL 后 Docker 坏了 | `parent snapshot does not exist`、`No such container: 1aa10e…` | 未干净的关机留下损坏的 buildkit 缓存与容器元数据 → `docker builder prune -af`、清 `/var/lib/docker/containers/*` 后重建 |

### 现在的两种形态并存

| 方式 | 谁托管 | 需要什么 | 适用 |
|---|---|---|---|
| `7-start-background.bat` | Windows 登录项 | 无 | 自己电脑上随手用 |
| `9-docker-up.bat` | dockerd + systemd（WSL） | 装一次 docker.io | 想要"关掉一切也活着"、可复现、能换机器 |

两条都验证过，不是"二选一的理论"。