"""验证 Docker 路径（Windows 无 docker CLI 时也能测到这里能测的部分）。

本机实际情况：Windows 侧没有 Docker，也没有管理员权限装 Docker Desktop；
但 WSL 里的 Ubuntu 有 docker 引擎（apt 装的 docker.io + docker-compose-v2）。
所以这条路径是：Windows 脚本 → wsl → dockerd → 容器。

这个脚本检查四件事：
  1. WSL 发行版存在，且能解析出项目路径（wslpath，中文路径最容易出错的地方）
  2. dockerd 在跑
  3. 容器在跑且 HEALTHCHECK 是 healthy
  4. 从 Windows 侧能访问 127.0.0.1:8770/api/health，且未登录首页返回 302
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
DISTRO = "Ubuntu"


def wsl(*args: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(["wsl", "-d", DISTRO, "-u", "root", "-e", *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def check_wslpath() -> bool:
    code, out = wsl("wslpath", "-a", ROOT + ".")
    line = (out or "").strip().splitlines()[0] if (out or "").strip() else ""
    ok = code == 0 and line.startswith("/mnt/")
    print(f"  [{'OK' if ok else 'FAIL'}] WSL 路径解析：{line or '(空)'}")
    return ok


def check_daemon() -> bool:
    code, out = wsl("docker", "info", "--format", "{{.ServerVersion}}")
    first = (out or "").strip().splitlines()[0] if (out or "").strip() else ""
    ok = code == 0 and bool(first)
    print(f"  [{'OK' if ok else 'FAIL'}] dockerd：{first if ok else '未运行'}")
    return ok


def check_container(wait: float = 45.0) -> bool:
    """容器起来后 HEALTHCHECK 有一段 starting 期，必须轮询等待。

    第一版是"看一眼就判"，结果第一次跑门禁刚好撞在 starting 上误报 FAIL。
    健康检查本身是异步的，测它就要等它。
    """
    deadline = time.time() + wait
    code, out = wsl("docker", "ps", "--format", "{{.Names}}|{{.Status}}")
    ok_run = code == 0 and "interview-agent" in out and "Up" in out
    healthy = False
    shown = "(无)"
    while time.time() < deadline:
        code2, out2 = wsl("docker", "inspect", "--format", "{{.State.Health.Status}}", "interview-agent")
        # wsl 会在 stderr 里夹一条代理警告，不能整段字符串比较（实测踩过）
        if code2 == 0 and "healthy" in (out2 or ""):
            healthy = True
            shown = "healthy"
            break
        shown = "starting/未就绪" if "starting" in (out2 or "") else "(无)"
        time.sleep(1.5)
    print(f"  [{'OK' if ok_run else 'FAIL'}] 容器运行：{out.strip().splitlines()[0] if out.strip() else '无'}")
    print(f"  [{'OK' if healthy else 'FAIL'}] HEALTHCHECK：{out2.strip() or '(无)'}")
    return ok_run and healthy


def _no_redirect_get(url: str) -> int:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(url, timeout=4)
        return 200
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return -1


def check_http() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8770/api/health", timeout=4) as r:
            body = json.loads(r.read().decode("utf-8"))
        ok_health = r.status == 200 and body.get("ok") is True
    except Exception as e:
        print(f"  [FAIL] /api/health 不可达：{e}")
        return False
    code = _no_redirect_get("http://127.0.0.1:8770/")
    ok_auth = code == 302
    print(f"  [{'OK' if ok_health else 'FAIL'}] Windows → /api/health：{body}")
    print(f"  [{'OK' if ok_auth else 'FAIL'}] 未登录访问 / → {code}（容器绑 0.0.0.0，应 302 到登录页）")
    return ok_health and ok_auth


def main() -> int:
    print("=" * 62)
    print("Docker 路径体检（Windows 脚本 → WSL → dockerd → 容器）")
    print("=" * 62)
    print("[A] WSL 与路径")
    if not check_wslpath():
        print("\n判定：FAIL（WSL 或路径解析不可用）")
        return 1
    print("[B] 守护进程")
    if not check_daemon():
        print("\n判定：FAIL（dockerd 未运行：wsl -d Ubuntu -u root -e systemctl start docker）")
        return 1
    print("[C] 容器状态")
    if not check_container():
        print("\n判定：FAIL（容器未就绪：双击 9-docker-up.bat 或看 docker compose logs）")
        return 1
    print("[D] 从 Windows 访问")
    if not check_http():
        print("\n判定：FAIL（HTTP 不通或鉴权未生效）")
        return 1
    print()
    print("判定：PASS（构建、健康检查、端口映射、鉴权 均已实测）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
