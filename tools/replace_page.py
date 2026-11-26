"""把 webui.py 里 PAGE 那 433 行内嵌 HTML 换成静态资源加载器。

一次性迁移脚本：删掉 PAGE 常量，改成
  · WEB_DIR + _page(name)  : 读 web/*.html
  · _static(path)          : 读 web/ 下静态文件并给 content-type
这样页面改动不再进 Python 字符串。
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "webui.py")

LOADER = '''
# ---------------------------------------------------------------- 静态资源
WEB_DIR = os.path.join(ROOT, "web")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
}


def page_path(name: str) -> str:
    return os.path.join(WEB_DIR, name)


def read_page(name: str) -> str:
    """读页面。页面文件不存在时返回一段可读的错误页，而不是 500。"""
    try:
        with open(page_path(name), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ("<!doctype html><meta charset='utf-8'>"
                f"<h1>页面缺失：{name}</h1>"
                "<p>请确认 web/ 目录完整（web/index.html、web/login.html、web/admin.html）。</p>")


def read_static(rel: str) -> tuple[bytes, str] | None:
    """读静态文件；带路径穿越防护（只允许 web/ 内部）。"""
    rel = rel.replace("\\\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    full = os.path.normpath(os.path.join(WEB_DIR, rel))
    if not full.startswith(os.path.normpath(WEB_DIR) + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    ext = os.path.splitext(full)[1].lower()
    with open(full, "rb") as f:
        return f.read(), MIME.get(ext, "application/octet-stream")
'''


def main() -> int:
    src = open(SRC, encoding="utf-8").read()
    if "WEB_DIR" in src:
        print("已经迁移过（存在 WEB_DIR）")
        return 0
    m = re.search(r'^PAGE = r?""".*?"""\n', src, re.S | re.M)
    if not m:
        print("没找到 PAGE 常量")
        return 1
    src = src[:m.start()] + LOADER.lstrip("\n") + src[m.end():]
    open(SRC, "w", encoding="utf-8").write(src)
    print(f"已替换 PAGE（{len(m.group(0))} 字符）为静态资源加载器")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
