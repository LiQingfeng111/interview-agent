"""把 webui.py 内嵌的 HTML 拆成 web/ 下的静态资源（一次性迁移脚本）。

为什么要拆：一个 886 行的 Python 文件里塞着 CSS 和 JS，
改一行样式要在 Python 字符串里找位置，编辑器不给补全、git diff 全是整行变更。
拆成 web/index.html + app.css + app.js 之后，前端才是"能改的东西"。

拆完 webui.py 只留：路由、鉴权、JSON 接口；页面里通过 /static/ 拉资源。
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "webui.py")
WEB = os.path.join(ROOT, "web")


def main() -> int:
    src = open(SRC, encoding="utf-8").read()
    m = re.search(r'PAGE\s*=\s*r?"""(.*?)"""', src, re.S)
    if not m:
        print("没找到 PAGE 变量，可能已经拆过了")
        return 1
    page = m.group(1)
    os.makedirs(WEB, exist_ok=True)

    css = re.search(r"<style>(.*?)</style>", page, re.S)
    js = re.search(r"<script>(.*?)</script>", page, re.S)
    if css:
        open(os.path.join(WEB, "app.css"), "w", encoding="utf-8").write(css.group(1).strip() + "\n")
    if js:
        open(os.path.join(WEB, "app.js"), "w", encoding="utf-8").write(js.group(1).strip() + "\n")

    html = page
    if css:
        html = html.replace(css.group(0), '<link rel="stylesheet" href="/static/app.css">')
    if js:
        html = html.replace(js.group(0), '<script src="/static/app.js"></script>')
    open(os.path.join(WEB, "index.html"), "w", encoding="utf-8").write(html.strip() + "\n")

    print(f"已拆出：web/index.html {len(html)} 字符")
    print(f"        web/app.css    {len(css.group(1)) if css else 0} 字符")
    print(f"        web/app.js     {len(js.group(1)) if js else 0} 字符")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
