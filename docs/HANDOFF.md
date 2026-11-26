# 怎么让别人在他电脑上打开你的东西

三种方式。按"对方会不会装东西"选，别三样都做。

---

## 方式一：把网址给他（最省事，不需要他装任何东西）

**适合**：就想让他看一眼、试一轮；不关心他的记录存在哪。

1. 你这边把服务跑起来，监听所有网卡：

```powershell
双击 7-start-background.bat
# 或者要对外时用容器（容器默认就绑 0.0.0.0）
双击 9-docker-up.bat
```

2. 查你这台机器的局域网 IP：

```powershell
ipconfig | Select-String "IPv4"
# 例如 192.168.1.23
```

3. 把 `http://192.168.1.23:8770` 发给他。同一个 WiFi / 同一局域网内他直接能打开。

注意：

- 服务会从"单机免登录"自动切到"需要登录"（因为不再只监听 127.0.0.1），他会先看到登录/注册页
- 他的面试记录存在**你的**电脑上，不在他电脑上
- 换到外网（不在同一个网络）需要额外做端口映射或用内网穿透，这一步本项目没做

---

## 方式二：给他一个镜像文件（不需要联网拉镜像，最稳）

**适合**：他要自己长期用、公司/学校网络拉不了镜像、你要交作业。

### 你这边：导出两个文件

```powershell
双击 11-export-image.bat
```

产物（在 `dist\` 目录）：

```
interview-agent.tar           45 MB     ← 整个运行环境（Python 3.11 + 代码 + 依赖）
interview-agent.tar.sha256     0.1 KB   ← 校验值，防止传坏
```

实测：

```
文件大小: 45M
sha256: 5e2e878fee9f125ef6e4c3e9eefd77e44ec8d68be2fc291a29d074fa347cb065
```

### 他那边：三步

1. 装一次 Docker（Windows 上最省事的是 WSL 里装，不需要管理员）：

```powershell
wsl --install -d Ubuntu                     # 已经装过就跳过
wsl -d Ubuntu -u root -e apt-get install -y docker.io
```

2. 把 `interview-agent.tar` 和 `interview-agent.tar.sha256` 放到项目文件夹的 `dist\` 下
3. 双击 `12-import-and-run.bat`

脚本会自动做六件事，实测输出：

```
[1/6] Check WSL distro Ubuntu
[2/6] Make sure dockerd is running
[3/6] Resolve paths inside WSL
[4/6] Verify checksum (if .sha256 exists)
      dist/interview-agent.tar: OK          ← 校验不通过就不往下走
[5/6] docker load
      Loaded image: interview-agent:local
[6/6] Start container (data kept in .\data)
Ready: http://127.0.0.1:8770
```

打开浏览器 → 注册（第一个账号自动是管理员）→ 开始面试。

---

## 方式三：给源码，让他自己构建

**适合**：他要改代码；或者你想让仓库看起来"标准"。

```powershell
# 他那边
git clone <你的仓库>    # 或者直接拷文件夹
双击 9-docker-up.bat     # 会自动 docker compose up -d --build
```

区别：这条路**首次要联网拉基础镜像**（`python:3.11-slim`，我们配了国内镜像源），
而且构建要几分钟（`pip install` 那几个可选依赖最慢，实测 149 秒）。

---

## 三方式对比

| | 方式一 给网址 | 方式二 给镜像 | 方式三 给源码 |
|---|---|---|---|
| 他要装东西吗 | 不用 | 要装 Docker | 要装 Docker |
| 要联网吗 | 不用（同局域网） | 不用 | 首次要 |
| 首次要等多久 | 0 | 约 1 分钟（load 45 MB） | 3–5 分钟（构建） |
| 记录存在谁电脑上 | 你的 | 他的 | 他的 |
| 他能改代码吗 | 不能 | 不能 | 能 |
| 能长期用吗 | 你关机就没了 | 能 | 能 |

---

## 最容易踩的三个坑

| 坑 | 现象 | 原因 |
|---|---|---|
| tar 传坏了 | `docker load` 报 `unexpected EOF` | 传输中断；靠 `.sha256` 校验提前发现（脚本已内置） |
| 数据没了 | 重启容器后面试记录清空 | 忘了挂 `-v ./data:/app/data`；`12-import-and-run.bat` 已默认挂好四个目录 |
| 端口被占 | `port is already allocated` | 本机已有程序占 8770；改 `-p 8771:8770` 或先停旧的 |

---

## 一句话选择

- 只想让他看看 → **方式一**
- 让他自己用、你不想管 → **方式二**
- 他要改、或者要进仓库 → **方式三**

---

## 实测补充：局域网地址的真实边界（重要）

"把网址给他"这件事，我原本写得过于乐观。实测三种情况：

| 服务怎么跑 | 127.0.0.1 能访问 | 局域网 IP（10.4.163.79）能访问 |
|---|---|---|
| Windows 上 `python webui.py --host 0.0.0.0` | 能 | **能**（实测 200） |
| 容器（WSL NAT 模式） | 能 | **不能** |
| 容器（WSL mirrored 模式，试过） | 能 | **不能**（未解决） |

原因：WSL2 默认是 NAT，容器发布的端口只转发到 Windows 的 loopback；
`netstat` 里 Windows 侧没有 `0.0.0.0:8770` 的监听，所以别人连不上。

**所以要让别人通过网址访问，用这条：**

```powershell
# 直接跑在 Windows 上，绑所有网卡
python webui.py --host 0.0.0.0 --port 8770
# 或双击 7-start-background.bat 后，确认监听地址是 0.0.0.0
netstat -ano | findstr :8770      # 应看到 0.0.0.0:8770  LISTENING
```

容器这条路要给别人访问，还需要额外一步（本项目未做，属已知缺口）：
`netsh interface portproxy`（需管理员）、或 WSL 的 mirrored 网络（试过未通）、
或内网穿透 / 一台公网服务器。

## 实测补充：镜像里到底有什么

```bash
# 源码可见，明文
docker run --rm interview-agent:local sh -c 'ls /app/*.py | head -3; head -2 /app/config.py'
# → 列出全部 .py，config.py 可读（464 行）

# tar 里是 OCI 布局 + gzip 压缩的层
tar -tf dist/interview-agent.tar | head -3
# → blobs/sha256/...
head -c 4 <某个 blob> | xxd -p → 1f8b0800  (= gzip)
```

所以 194 MB → 45 MB 不是"少了东西"，是**同一个东西换了压缩状态**：

| 数字 | 含义 |
|---|---|
| `docker image ls` 的 194MB | 解压后占磁盘的大小（所有层解开求和） |
| tar 文件的 45MB | 压缩后的层（gzip），传输用 |

并且：`.dockerignore` 曾经漏掉 `data/users.db` 和 `data/secret.key`，
导致镜像里 baked 进了一份账号库（7 个账号 + 哈希）。现已修掉并验证：

```
docker run --rm interview-agent:local sh -c 'test -f /app/data/users.db && echo 含 || echo 不含'
→ 不含
```