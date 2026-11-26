"""历史与趋势：把"单场面试"变成"训练闭环"。

为什么这是当前最缺的一块：
  现在每场面试都是孤立的——存了 JSON 但没人读，用户练了 5 场也看不到
  "我到底进步了没有""同一类短板是不是反复犯"。一个不复盘的面试工具，
  本质上是一次性玩具，这是"草台班子"最明显的地方。

设计取舍：
  - 不引数据库：直接扫 runs/*.json，学生环境零部署。
  - 只做能算准的指标：维度均分、最深卡点、短板分类次数。
    不做"进步百分比"这种伪精确——样本量根本不够。
"""
from __future__ import annotations

import json
import os
from collections import Counter

from config import RUNS_DIR


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_runs(runs_dir: str = RUNS_DIR) -> list[dict]:
    """按时间正序读出所有会话摘要。"""
    if not os.path.isdir(runs_dir):
        return []
    out: list[dict] = []
    for name in sorted(os.listdir(runs_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(runs_dir, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        out.append(_summarize(data, name))
    return out


def _summarize(data: dict, name: str) -> dict:
    meta = data.get("meta") or {}
    stats = meta.get("stats") or {}
    profile = meta.get("profile") or {}
    sm = meta.get("sm") or {}
    turns = data.get("turns") or []
    # 老记录没有 mode 字段：标 unknown，不参与趋势比较（不能猜成 real）
    mode = profile.get("mode") or "unknown"

    return {
        "file": name,
        "started_at": meta.get("started_at", ""),
        "role": data.get("role", ""),
        "turns": len(turns),
        "avg": _num(stats.get("avg")),
        "dims": {k: _num(v) for k, v in (stats.get("dims") or {}).items()},
        "overall": _num(profile.get("overall")),
        "deepest": _num(sm.get("deepest_layer")) or _deepest_from_turns(turns),
        "weaknesses": [d.get("category", "") for d in (meta.get("diagnosis") or [])],
        "mode": mode,
    }


def _deepest_from_turns(turns: list) -> float:
    lv = 0
    for t in turns:
        try:
            lv = max(lv, int(t.get("ladder_level") or 0))
        except (TypeError, ValueError):
            pass
    return float(lv)


def recurring_weaknesses(runs: list[dict], min_runs: int = 2) -> list[tuple[str, int]]:
    """反复出现的短板才值得报——只出现一次是偶然，不是模式。"""
    counter: Counter[str] = Counter()
    for r in runs:
        for w in set(r.get("weaknesses") or []):
            if w:
                counter[w] += 1
    return [(k, v) for k, v in counter.most_common() if v >= min_runs]


def trend(runs: list[dict], key: str = "avg") -> str:
    """用箭头表示最近几场的走向。样本 <2 时不给结论。"""
    vals = [r.get(key, 0.0) for r in runs if r.get("mode") != "mock"]
    if len(vals) < 2:
        return "样本不足"
    delta = vals[-1] - vals[-2]
    if delta > 0.2:
        return f"↑ +{delta:.2f}"
    if delta < -0.2:
        return f"↓ {delta:.2f}"
    return "→ 持平"


def render_history(runs: list[dict]) -> str:
    if not runs:
        return "  （还没有历史记录。跑一场面试并保存后就能看到趋势。）"

    lines = ["  序号  时间              岗位        轮次  均分  最深  模式   短板"]
    lines.append("  " + "-" * 76)
    for i, r in enumerate(runs, 1):
        weak = "、".join(r["weaknesses"][:2]) or "-"
        lines.append(
            f"  {i:>4}  {r['started_at'][:16]:<16}  {r['role']:<10}  "
            f"{r['turns']:>4}  {r['avg']:>4.2f}  {int(r['deepest']):>4}  "
            f"{r['mode']:<5}  {weak}"
        )

    real = [r for r in runs if r["mode"] == "real"]
    lines.append("")
    lines.append(f"  真实模型场次：{len(real)} / {len(runs)}（mock 与旧记录不参与趋势）")
    if len(real) < 2:
        lines.append("  趋势：真实模型样本不足 2 场，不做比较（mock 分数不参与趋势）")
    else:
        lines.append(f"  最近两场均分：{real[-2]['avg']:.2f} → {real[-1]['avg']:.2f}  {trend(real)}")
        lines.append(f"  最深卡点：{int(real[-2]['deepest'])} → {int(real[-1]['deepest'])} 层")

    rec = recurring_weaknesses(real)
    lines.append("")
    if rec:
        lines.append("  反复出现的短板（≥2 场）：")
        for name, cnt in rec:
            lines.append(f"    - {name}：{cnt} 场")
    else:
        lines.append("  反复出现的短板：暂无（同类短板要 ≥2 场才算模式）")

    lines.append("")
    lines.append("  说明：单场样本量小，趋势只反映『最近两场』，不构成能力结论。")
    return "\n".join(lines)
