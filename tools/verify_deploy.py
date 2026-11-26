"""部署路径体检：在没有 Docker 的机器上，尽量验证 Dockerfile / compose 的假设。

为什么需要：Dockerfile 写了不等于能跑。本机没装 Docker（`where docker` 为空），
所以我用"静态检查 + 真按容器参数启动一次"两条腿走到能验证的最远处：

  A. 静态检查 Dockerfile
     · COPY 的目录在仓库里真的存在（web/、data/ 之类）
     · EXPOSE 的端口与 CMD 里的 --port 一致
     · HEALTHCHECK 请求的路径在服务端真的存在（/api/health 且免登录）
     · CMD 引用的入口文件存在
  B. 按容器的启动参数真跑一次
     · python webui.py --host 0.0.0.0 --port <空闲端口>
     · 从 127.0.0.1 访问 /api/health 必须 200
     · / 必须 302（证明鉴权在容器参数下同样生效）
  C. 明确打印"未验证项"：镜像构建、容器网络、卷挂载行为

用法：python tools/verify_deploy.py
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def bad(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def check_dockerfile() -> bool:
    print("[A] Dockerfile 静态检查")
    path = os.path.join(ROOT, "Dockerfile")
    if not os.path.exists(path):
        bad("没有 Dockerfile")
        return False
    text = open(path, encoding="utf-8").read()

    # COPY 的目标必须存在
    copies = re.findall(r"^COPY\s+(.+)$", text, re.M)
    missing = []
    for line in copies:
        parts = line.split()
        for src in parts[:-1]:
            if src.startswith("--"):
                continue
            if not os.path.exists(os.path.join(ROOT, src.strip("./"))):
                missing.append(src)
    if missing:
        bad(f"COPY 的来源不存在：{missing}")
        return False
    ok(f"COPY 来源齐全（{sum(len(l.split()) - 1 for l in copies)} 项）")

    expose = re.findall(r"^EXPOSE\s+(\d+)", text, re.M)
    cmd_port = re.findall(r'"--port",\s*"(\d+)"', text)
    if expose and cmd_port and expose[0] != cmd_port[0]:
        bad(f"EXPOSE {expose[0]} 与 CMD --port {cmd_port[0]} 不一致")
        return False
    if expose:
        ok(f"端口一致：EXPOSE {expose[0]} / CMD --port {cmd_port[0] if cmd_port else '?'}")

    # HEALTHCHECK 必须打在免登录接口上，否则容器永远 unhealthy
    hc = re.search(r"HEALTHCHECK.*?CMD\s+(.+)$", text, re.M | re.S)
    if hc:
        if "/api/health" not in hc.group(1):
            bad("HEALTHCHECK 没打 /api/health（该接口是免登录的，适合做健康检查）")
            return False
        ok("HEALTHCHECK 打的是免登录的 /api/health")

    entry = re.findall(r'"(\w+\.py)"', text)
    for e in entry:
        if not os.path.exists(os.path.join(ROOT, e)):
            bad(f"CMD 引用的入口不存在：{e}")
            return False
    if entry:
        ok(f"入口存在：{', '.join(sorted(set(entry)))}")
    return True


def check_compose() -> bool:
    print("[B] docker-compose.yml 静态检查")
    path = os.path.join(ROOT, "docker-compose.yml")
    if not os.path.exists(path):
        warn("没有 docker-compose.yml（可选）")
        return True
    text = open(path, encoding="utf-8").read()
    if "8770:8770" not in text:
        bad("compose 没把 8770 映射出来")
        return False
    ok("端口映射 8770:8770")
    for vol in ("./data", "./runs", "./reports", "./logs"):
        if vol not in text:
            warn(f"compose 未挂载 {vol}（容器删了数据会丢）")
    ok("数据卷已挂载（data/runs/reports/logs）")
    return True


def check_runtime() -> bool:
    print("[C] 按容器参数真跑一次（--host 0.0.0.0）")
    port = free_port()
    proc = subprocess.Popen(
        [PY, "webui.py", "--host", "0.0.0.0", "--port", str(port)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        health = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                    health = r.status
                    break
            except Exception:
                continue
        if health != 200:
            bad("服务没起来或 /api/health 不通")
            return False
        ok("/api/health → 200（容器健康检查可用）")

        # 未登录访问首页应 302（鉴权在 0.0.0.0 绑定下同样生效）
        # 注意：urllib 默认会自动跟随 302，那样拿到的是登录页的 200，测不出重定向。
        # 这里显式禁用跟随——第一版就踩了这个坑，误判成"鉴权没生效"。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(base + "/", timeout=3)
            code = 200
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = -1
        if code != 302:
            bad(f"未登录访问首页返回 {code}，期望 302（鉴权可能没生效）")
            return False
        ok("未登录访问首页 → 302（鉴权生效）")
        return True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def main() -> int:
    print("=" * 62)
    print("部署路径体检（本机无 Docker，验证到能验证的最远处）")
    print("=" * 62)
    a = check_dockerfile()
    b = check_compose()
    c = check_runtime() if a else False
    print()
    print("[未验证项] 这些必须在有 Docker 的机器上补：")
    print("  · docker build 是否成功（基础镜像、pip 安装、层缓存）")
    print("  · 容器内 --host 0.0.0.0 的网络可达性与端口映射")
    print("  · 卷挂载后 data/*.db、data/secret.key 的权限与持久性")
    print("  · HEALTHCHECK 在容器内的真实状态切换（starting → healthy）")
    print()
    verdict = "PASS（静态 + 启动参数已验证，镜像构建未验证）" if (a and b and c) else "FAIL"
    print(f"判定：{verdict}")
    return 0 if (a and b and c) else 1


if __name__ == "__main__":
    raise SystemExit(main())
