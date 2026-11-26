"""联网通道快速自检（会真的发请求，手动跑）。

用途：换网络环境后先跑这个，确认"site: 限定检索 + 页面抓取"还能用。
判据不是"有没有返回"，而是"返回的东西是不是本领域的技术资料"。

跑法：python -m eval.smoke_web
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from websearch import gather, search

CASES = [
    ("FAISS 索引 取舍", "tech", True),
    ("RAG 召回率 优化", "tech", True),
    ("评估体系", "fact", False),      # 这个大概率抓不到本领域资料，应诚实报 ok=False
]


def main() -> int:
    print("=" * 72)
    print("联网通道自检（真实请求）")
    print("=" * 72)
    failures = 0
    for term, kind, expect_ok in CASES:
        res = search(term, kind=kind, limit=5)
        flag = "OK" if res["ok"] == expect_ok else "UNEXPECTED"
        if res["ok"] != expect_ok:
            failures += 1
        print(f"\n[{flag}] {term} ({kind})  ok={res['ok']} 质量={res['quality']}")
        print(f"        查询：{res['queries']}")
        for r in res["results"][:3]:
            print(f"        - {(r.get('title') or '')[:52]}")
        pack = gather(term, kind=kind, max_chars=200)
        print(f"        资料摘要：{(pack['text'] or '(空)')[:120]}")

    print("\n" + "-" * 72)
    print("结论：" + ("通道正常" if failures == 0 else f"{failures} 项不符合预期（可能被反爬/站点改版）"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
