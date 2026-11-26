"""把"绑定地址"这件事变成看得见的东西。

跑这个脚本会做四件事，然后打印一张对照表：
  1. 在 127.0.0.1:8801 起一个服务（只开本机内部）
  2. 在 0.0.0.0:8802   起一个服务（所有网卡都开）
  3. 用 127.0.0.1 分别访问上面两个
  4. 用本机局域网 IP 分别访问上面两个

预期结果（这是重点）：
  127.0.0.1:8801  通      局域网IP:8801  不通
  127.0.0.1:8802  通      局域网IP:8802  通

同一份代码，只改绑定地址，谁能连进来就完全变了。
这就是 --host 的全部含义。
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


class Echo(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"ok": True, "绑定在": self.server.server_address}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def lan_ip() -> str:
    """取一个非回环的本机 IP（UDP connect 不发包，只用来让系统选路）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def serve(host: str, port: int) -> HTTPServer:
    srv = HTTPServer((host, port), Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def probe(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode())
            return f"通  {data['绑定在']}"
    except Exception as e:
        return f"不通 ({type(e).__name__})"


def main() -> int:
    ip = lan_ip()
    print(f"本机局域网 IP = {ip}\n")
    s1 = serve("127.0.0.1", 8801)
    s2 = serve("0.0.0.0", 8802)
    time.sleep(0.5)

    rows = []
    for label, url in (
        ("127.0.0.1:8801", "http://127.0.0.1:8801/"),
        (f"{ip}:8801", f"http://{ip}:8801/"),
        ("127.0.0.1:8802", "http://127.0.0.1:8802/"),
        (f"{ip}:8802", f"http://{ip}:8802/"),
    ):
        rows.append((label, probe(url)))

    print(f"{'访问地址':<24} {'结果'}")
    print("-" * 52)
    for label, res in rows:
        print(f"{label:<24} {res}")

    print()
    print("读法：")
    print("  8801 只绑了 127.0.0.1 → 换个地址访问就不通")
    print("  8802 绑了 0.0.0.0    → 任何本机地址都能访问")
    print("  程序没变，只换了绑定地址。")
    s1.shutdown()
    s2.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
