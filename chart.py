"""多维雷达图：把 5 个评分维度画成一张图，报告里能一眼看出偏科。

两条路径，互相当备份：
  1. **在线 API**（QuickChart）：`https://quickchart.io/chart?c=<config>` 返回 PNG。
     不用注册、不用装包，报告发给别人也能打开。
  2. **本地 SVG**：自己拼坐标系画多边形，**不依赖网络**。
     在线 API 挂了、或者没网，报告照样有图。

为什么两套都要：面试作品集最怕"图裂了"。只有在线 API，评审打开是空白；
只有本地 SVG，丢进文档不如 PNG 方便。所以默认先试 API，失败静默降级。

诚实边界：SVG 版是简版（网格 + 两条折线），不追求像素级美观。
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request

# 报告里的中文维度名（图上直接显示中文，评审判断更快）
DIM_CN = {
    "accuracy": "准确性",
    "depth": "深度",
    "tradeoff": "权衡",
    "boundary": "边界",
    "clarity": "表达",
    "element_coverage": "要素覆盖",
}
TARGET_SCORE = 3.0          # 合格线：3 分（partial 的下沿）
FULL_SCORE = 5.0


def radar_config(dims: dict, target: float = TARGET_SCORE,
                 title: str = "五维能力雷达") -> dict:
    """生成 Chart.js 的 radar 配置（QuickChart 与浏览器都用这个结构）。"""
    keys = [k for k in DIM_CN if k in (dims or {})]
    labels = [DIM_CN[k] for k in keys]
    values = [round(float(dims.get(k) or 0), 2) for k in keys]
    return {
        "type": "radar",
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "label": "本场得分",
                    "data": values,
                    "borderColor": "#4e79a7",
                    "backgroundColor": "rgba(78,121,167,0.25)",
                    "pointBackgroundColor": "#4e79a7",
                    "borderWidth": 2,
                },
                {
                    "label": f"合格线 {target:g}",
                    "data": [target] * len(labels),
                    "borderColor": "#9aa4b2",
                    "backgroundColor": "rgba(154,164,178,0.08)",
                    "borderDash": [6, 4],
                    "pointRadius": 0,
                    "borderWidth": 1,
                },
            ],
        },
        "options": {
            "title": {"display": True, "text": title},
            "legend": {"position": "bottom"},
            "scale": {
                "ticks": {"beginAtZero": True, "max": FULL_SCORE, "stepSize": 1},
                "pointLabels": {"fontSize": 14},
            },
        },
    }


def quickchart_url(config: dict, width: int = 560, height: int = 420,
                   background: str = "white") -> str:
    """拼 QuickChart 的图片地址（Chart.js 配置塞进查询参数）。"""
    payload = json.dumps(config, ensure_ascii=False)
    query = urllib.parse.urlencode({
        "c": payload, "w": width, "h": height, "bkg": background, "f": "png",
    })
    return "https://quickchart.io/chart?" + query


def fetch_png(url: str, timeout: int = 20) -> bytes | None:
    """拉在线图片。失败返回 None（不抛异常：报告不能因为图崩了就打不出来）。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "interview_agent/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        return data if data[:8] == b"\x89PNG\r\n\x1a\n" else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


# ---------------------------------------------------------------- 本地 SVG
def local_svg(dims: dict, target: float = TARGET_SCORE, size: int = 420,
              title: str = "五维能力雷达") -> str:
    """手写 SVG 雷达图：五边形网格 + 两条折线。不依赖任何库和网络。"""
    keys = [k for k in DIM_CN if k in (dims or {})]
    labels = [DIM_CN[k] for k in keys]
    values = [float(dims.get(k) or 0) for k in keys]
    n = max(1, len(labels))
    cx = cy = size / 2
    r = size * 0.34

    def point(i: int, ratio: float) -> tuple[float, float]:
        ang = -math.pi / 2 + 2 * math.pi * i / n
        return cx + r * ratio * math.cos(ang), cy + r * ratio * math.sin(ang)

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
             f'viewBox="0 0 {size} {size}" font-family="Segoe UI, sans-serif">',
             f'<rect width="{size}" height="{size}" fill="#ffffff"/>',
             f'<text x="{cx}" y="24" text-anchor="middle" font-size="15" '
             f'fill="#2b3440">{title}</text>']

    # 网格：4 圈
    for ring in range(1, 5):
        pts = " ".join(f"{point(i, ring / 4)[0]:.1f},{point(i, ring / 4)[1]:.1f}"
                       for i in range(n))
        parts.append(f'<polygon points="{pts}" fill="none" stroke="#e3e8ef" stroke-width="1"/>')
    # 轴线
    for i in range(n):
        x, y = point(i, 1.0)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
                     f'stroke="#e3e8ef" stroke-width="1"/>')

    # 合格线（虚线）
    t_pts = " ".join(f"{point(i, min(1.0, target / FULL_SCORE))[0]:.1f},"
                     f"{point(i, min(1.0, target / FULL_SCORE))[1]:.1f}" for i in range(n))
    parts.append(f'<polygon points="{t_pts}" fill="none" stroke="#9aa4b2" '
                 f'stroke-width="1" stroke-dasharray="6 4"/>')

    # 数据多边形
    d_pts = " ".join(f"{point(i, min(1.0, max(0.0, v / FULL_SCORE)))[0]:.1f},"
                     f"{point(i, min(1.0, max(0.0, v / FULL_SCORE)))[1]:.1f}"
                     for i, v in enumerate(values))
    parts.append(f'<polygon points="{d_pts}" fill="rgba(78,121,167,0.25)" '
                 f'stroke="#4e79a7" stroke-width="2"/>')
    for i, v in enumerate(values):
        x, y = point(i, min(1.0, max(0.0, v / FULL_SCORE)))
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#4e79a7"/>')
        parts.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" text-anchor="middle" '
                     f'font-size="11" fill="#4e79a7">{v:.1f}</text>')

    # 标签
    for i, lab in enumerate(labels):
        x, y = point(i, 1.18)
        anchor = "middle"
        if x > cx + 6:
            anchor = "start"
        elif x < cx - 6:
            anchor = "end"
        parts.append(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
                     f'font-size="13" fill="#43506b">{lab}</text>')

    parts.append(f'<text x="{cx}" y="{size - 12}" text-anchor="middle" font-size="11" '
                 f'fill="#8b95a5">实线=本场得分 · 虚线=合格线 {target:g} · 满分 {FULL_SCORE:g}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def write_chart_files(dims: dict, outdir: str = "reports",
                      stem: str = "radar", title: str = "五维能力雷达",
                      online: bool = True) -> dict:
    """出图：优先在线 PNG，失败用本地 SVG。返回各产物路径。"""
    os.makedirs(outdir, exist_ok=True)
    config = radar_config(dims, title=title)
    out = {"svg": "", "png": "", "url": "", "source": "local-svg"}
    out["url"] = quickchart_url(config)

    svg_path = os.path.join(outdir, f"{stem}.svg")
    with open(svg_path, "w", encoding="utf-8") as f:
        f.write(local_svg(dims, title=title))
    out["svg"] = svg_path

    if online:
        png = fetch_png(out["url"])
        if png:
            png_path = os.path.join(outdir, f"{stem}.png")
            with open(png_path, "wb") as f:
                f.write(png)
            out["png"] = png_path
            out["source"] = "quickchart"
    return out


def report_html(dims: dict, stats: dict | None = None, audit: dict | None = None,
                title: str = "面试报告", svg_markup: str = "") -> str:
    """单页 HTML 报告：内嵌 SVG（离线可看）+ 关键数字。"""
    svg = svg_markup or local_svg(dims, title="五维能力雷达")
    rows = ""
    for k, v in (stats or {}).items():
        rows += f"<tr><td>{k}</td><td>{v}</td></tr>"
    audit_rows = ""
    for k, v in (audit or {}).items():
        audit_rows += f"<tr><td>{k}</td><td>{v}</td></tr>"
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
 body{{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:#f6f8fb;
      color:#2b3440;margin:0;padding:28px}}
 .card{{background:#fff;border-radius:14px;padding:20px 24px;margin-bottom:18px;
        box-shadow:0 2px 14px rgba(20,30,50,.06);max-width:900px}}
 h1{{font-size:20px;margin:0 0 6px}} h2{{font-size:15px;margin:18px 0 8px;color:#43506b}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 td{{border-bottom:1px solid #eef1f6;padding:6px 4px}} td:first-child{{color:#6b7688;width:38%}}
 .chart{{text-align:center}}
</style></head><body>
<div class="card"><h1>{title}</h1>
<div class="chart">{svg}</div></div>
<div class="card"><h2>各维度得分</h2><table>{rows}</table></div>
<div class="card"><h2>审计</h2><table>{audit_rows}</table></div>
</body></html>"""
