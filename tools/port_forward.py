"""端口转发代理：把 [::]:18336 的请求转发到 127.0.0.1:18326。

## 为什么需要这个

模型网关（DSH 的 relay）只监听 `127.0.0.1:18326`。
WSL 里的后端用宿主 IP（`172.22.48.1`）访问它 → **连不上**（端口没对外暴露）。

标准解法是 `netsh interface portproxy`，但那需要管理员权限。
这个脚本用纯用户态的方式做同样的事：在 `0.0.0.0:18336` 上监听，
把流量转发到 `127.0.0.1:18326`。

## 为什么是纯标准库

1. 不需要装任何依赖
2. 不引入额外攻击面（只做 TCP 搬运，不解析 HTTP）
3. 可以随时 Ctrl+C 停掉

## 用法

    python tools/port_forward.py              # 默认 18336 → 18326
    python tools/port_forward.py 18336 18326
"""

from __future__ import annotations

import socket
import sys
import threading

LISTEN_HOST = "0.0.0.0"
BUFSIZE = 65536


def _pipe(src: socket.socket, dst: socket.socket, tag: str) -> None:
    """单向搬运数据，任一方向断开就关两边。"""
    try:
        while True:
            data = src.recv(BUFSIZE)
            if not data:
                break
            dst.sendall(data)
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client: socket.socket, target_host: str, target_port: int) -> None:
    """处理一个客户端连接：建到目标的上游连接，双向转发。"""
    upstream = None
    try:
        upstream = socket.create_connection((target_host, target_port), timeout=30)
        upstream.settimeout(None)
        client.settimeout(None)

        t1 = threading.Thread(target=_pipe, args=(client, upstream, "up"), daemon=True)
        t2 = threading.Thread(target=_pipe, args=(upstream, client, "down"), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception as e:  # noqa: BLE001
        print(f"  [转发失败] {type(e).__name__}: {e}")
    finally:
        for s in (client, upstream):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def main() -> int:
    listen_port = int(sys.argv[1]) if len(sys.argv) > 1 else 18336
    target_port = int(sys.argv[2]) if len(sys.argv) > 2 else 18326
    target_host = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, listen_port))
    srv.listen(64)

    print(f"端口转发已启动：{LISTEN_HOST}:{listen_port} → {target_host}:{target_port}")
    print("  Ctrl+C 停止")
    print(f"  WSL 里请用：http://<宿主IP>:{listen_port}/v1")
    print()

    count = 0
    try:
        while True:
            client, addr = srv.accept()
            count += 1
            if count % 20 == 1:
                print(f"  [{count}] 来自 {addr[0]}:{addr[1]}")
            threading.Thread(
                target=handle, args=(client, target_host, target_port), daemon=True,
            ).start()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
