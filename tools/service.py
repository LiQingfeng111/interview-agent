"""后台服务的生命周期管理：起、停、查状态（Windows 无 systemd 时的最小替代）。

为什么需要：别人家的平台没有黑窗口，因为它是"服务"——由 Docker/systemd/云平台托管，
终端只是安装时用一次。我们的 app 是前台进程，窗口一关就死。
这里用最朴素的办法补上：写 pid 文件 + 用 pythonw 脱离控制台 + 提供 stop/status。

注意 pythonw 的取舍：它没有控制台，所以启动失败时你看不到报错。
因此启动时把 stdout/stderr 重定向到 logs/webui.log——出问题先看这个文件。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(ROOT, "data", "webui.pid")
LOG_FILE = os.path.join(ROOT, "logs", "webui.log")
DEFAULT_PORT = 8770


def _pythonw() -> str:
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def _read_pid() -> int:
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=8)
        return str(pid) in (out.stdout or "")
    except Exception:
        return False


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start(port: int = DEFAULT_PORT, host: str = "127.0.0.1", wait: float = 25.0) -> int:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"已在运行：PID {pid}（http://{host}:{port}）")
        return 0
    if not _port_free(port):
        print(f"端口 {port} 被占用：先 python tools/service.py stop，或换 --port")
        return 1

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    log = open(LOG_FILE, "a", encoding="utf-8")
    log.write(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    log.flush()

    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS: 脱离当前控制台，关掉终端也不影响它
        # CREATE_NO_WINDOW: 不弹新窗口（用 python.exe 时也需要这一条）
        # 用 python.exe 而不是 pythonw：pythonw 的 stdout 拿不到，
        # 启动失败时会变成"静默失败"——日志里只有一行 header，没法排查
        creationflags = 0x00000008 | 0x08000000

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"   # 不加这行，日志是块缓冲，启动阶段永远空白
    env["PYTHONIOENCODING"] = "utf-8"   # 否则中文日志在 Windows 控制台/日志里会乱码

    proc = subprocess.Popen(
        [sys.executable, "webui.py", "--host", host, "--port", str(port)],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=creationflags, close_fds=True, env=env,
    )
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    deadline = time.time() + wait
    while time.time() < deadline:
        if _healthy(port):
            print(f"已启动（后台，无窗口）：PID {proc.pid}")
            print(f"  访问：http://127.0.0.1:{port}")
            print(f"  日志：{LOG_FILE}")
            print(f"  停止：python tools/service.py stop")
            return 0
        if proc.poll() is not None:
            print(f"进程已退出（退出码 {proc.returncode}），请看日志：{LOG_FILE}")
            return 1
        time.sleep(0.5)
    print(f"启动超时（{wait:.0f}s）：请看日志 {LOG_FILE}")
    return 1


def stop() -> int:
    pid = _read_pid()
    if not pid:
        print("没有 pid 文件，可能本来就没在后台运行")
        return 0
    if not _alive(pid):
        print(f"PID {pid} 已不在，清理 pid 文件")
        _clear_pid()
        return 0
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
    for _ in range(20):
        if not _alive(pid):
            break
        time.sleep(0.2)
    _clear_pid()
    print(f"已停止 PID {pid}")
    return 0


def _clear_pid() -> None:
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def status(port: int = DEFAULT_PORT) -> int:
    pid = _read_pid()
    alive = _alive(pid)
    health = _healthy(port)
    print(f"PID      : {pid or '(无)'} {'存活' if alive else '未运行'}")
    print(f"/api/health : {'200 OK' if health else '不通'}")
    print(f"访问地址 : http://127.0.0.1:{port}")
    print(f"日志     : {LOG_FILE}")
    return 0 if (alive and health) else 1


def main(argv: list[str]) -> int:
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    port = DEFAULT_PORT
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if cmd == "start":
        return start(port=port)
    if cmd == "stop":
        return stop()
    if cmd == "restart":
        stop()
        time.sleep(1)
        return start(port=port)
    if cmd == "status":
        return status(port=port)
    print("用法：python tools/service.py [start|stop|restart|status] [--port 8770]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
