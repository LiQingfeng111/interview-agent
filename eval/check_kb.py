"""知识库质量检查：量化"联网备课到底捞回了多少有用东西"。

为什么单独写这个脚本？
因为"我接了联网搜索"这句话毫无信息量。能说出
「4 个岗位 18 个话题，备课保留率 78%，Agent 应用岗话题覆盖 5/5」
才是有工程感的表达——这也正是简历上该写的东西。

用法：
    python eval/check_kb.py                # 检查全部岗位（会联网，慢）
    python eval/check_kb.py --role agent_app
    python eval/check_kb.py --cached       # 只读缓存，不联网
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import ROLE_KB
from knowledge import enrich_role, load_cached, relevance_score, role_topics, topic_queries
from search import provider_name, web_search


def check_role(role_key: str, cached_only: bool = False) -> dict:
    topics = role_topics(role_key)
    cached = load_cached(role_key)
    rows = []

    for topic in topics:
        got = (cached.get("topics") or {}).get(topic) or []
        if not cached_only and not got:
            data = enrich_role(role_key, use_search=True, force=True)
            got = (data.get("topics") or {}).get(topic) or []

        # 重新算一遍原始命中情况，用于报告"保留率"
        raw, kept = 0, 0
        if not cached_only:
            for q in topic_queries(role_key, topic)[:1]:
                rs = web_search(q, limit=5)
                raw += len(rs)
                kept += sum(1 for r in rs if relevance_score(r, q) >= 3)

        rows.append({
            "topic": topic,
            "kept": len(got),
            "raw": raw,
            "sample": (got[0]["title"][:50] if got else ""),
        })

    return {
        "role": role_key,
        "name": ROLE_KB[role_key]["name"],
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default=None)
    ap.add_argument("--cached", action="store_true", help="只读缓存，不联网")
    args = ap.parse_args()

    roles = [args.role] if args.role else list(ROLE_KB.keys())
    print(f"搜索后端：{provider_name()}")
    if not args.cached:
        print("（联网检查，每个话题会发一次请求，可能需要一两分钟）")
    print("=" * 74)

    total_topics = total_ok = 0
    total_raw = total_kept = 0
    for role in roles:
        res = check_role(role, cached_only=args.cached)
        print(f"\n【{res['name']}】{role}")
        for row in res["rows"]:
            total_topics += 1
            ok = row["kept"] > 0
            total_ok += int(ok)
            total_raw += row["raw"]
            total_kept += min(row["raw"], row["kept"]) if row["raw"] else 0
            mark = "OK " if ok else "空 "
            extra = f" 保留{row['kept']}" if args.cached else f" 保留{row['kept']}/{row['raw']}"
            print(f"  [{mark}] {row['topic']:<20}{extra}  {row['sample']}")

    print("\n" + "=" * 74)
    cov = total_ok / max(1, total_topics)
    print(f"话题覆盖：{total_ok}/{total_topics} = {cov:.0%}")
    if total_raw:
        print(f"结果保留率：{total_kept}/{total_raw} = {total_kept / total_raw:.0%}")
    gate = 0.8
    print(f"门槛 {gate:.0%}：" + ("PASS" if cov >= gate else "FAIL"))
    return 0 if cov >= gate else 1


if __name__ == "__main__":
    sys.exit(main())
