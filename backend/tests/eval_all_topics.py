"""全话题召回评测：46 个话题逐个算 recall@5，看当前阈值通不通用。

## 为什么要做这个

我之前只测了 3 个查询（注意力/缓存/RAG），就给全部 46 个话题定了阈值 0.42。
这是典型的"用 3 个样本给 46 个群体下结论"。

## 方法

对每个话题（topic），造一条**贴近真实面试口语**的查询，
看该话题下的题目能否进入 top5：

    recall@5 = 命中查询数 / 总查询数

同时统计：
- 每个话题的**最高分**（说明这个话题整体好检还是难检）
- 单话题内 recall（该话题自己的题排进 top5 的比例）

## 查询怎么造（关键）

不能直接拿题干当查询——那样相似度接近 1，测不出问题。
也不能纯靠人工写 46 条（工作量太大且主观）。

做法：**从题干里抽关键词 + 口语化改写模板**，模拟"面试官脑子里想考这个点"。
例如：
    题干："多头注意力中，若 d_model=512、h=8，每个头的 d_k 和 d_v 通常取多少？"
    查询："多头注意力每个头的维度是怎么算的"
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import retriever
from app.repos.pg import get_conn


# 口语化改写模板：把话题名变成"人会问出来的样子"
QUERY_TEMPLATES = [
    "{topic}这块你怎么理解",
    "说说{topic}，实际怎么做的",
    "{topic}有什么坑",
    "{topic}为什么重要",
]

# 话题名 → 更自然的口语查询（人工补的，覆盖主要的）
#
# 为什么需要人工表：模板生成的"XX这块你怎么理解"非常不自然，
# 实测这种机器人腔调的查询相似度普遍低 0.1-0.2。
# 例如"稳定性与可观测性这块你怎么理解"只有 0.419，
# 而真实面试官会说"线上出问题你靠什么发现"。
MANUAL_QUERIES = {
    # ---- ai_algo ----
    "Transformer 注意力机制": "多头注意力里每个头的维度怎么算",
    "微调与 LoRA": "LoRA 微调是怎么做的，参数量多少",
    "评测与幻觉": "怎么评估模型输出有没有幻觉",
    "Agent 工具调用": "Agent 调用工具失败怎么重试",
    "RAG 检索增强生成": "RAG 的检索流程是怎样的",
    "幻觉": "模型瞎编答案怎么处理",

    # ---- backend ----
    "缓存一致性": "缓存和数据库不一致怎么处理",
    "数据库索引": "索引为什么能加速查询",
    "高并发与限流": "高并发下限流怎么做",
    "消息队列": "消息队列怎么保证不丢消息",
    "服务拆分": "微服务拆分的边界怎么定",

    # ---- frontend ----
    "浏览器渲染机制": "浏览器从输入 URL 到渲染经历了什么",
    "数据可视化": "大数据量图表怎么优化性能",
    "前端性能优化": "首屏加载慢怎么优化",
    "状态管理与数据流": "复杂前端状态怎么管理",
    "组件设计与工程化": "可复用组件怎么设计",
    "前端质量与监控": "线上前端错误怎么监控",
    "前端工程化": "前端构建和部署流程怎么做的",

    # ---- agent_app ----
    "上下文工程": "长上下文怎么组织提示词",
    "评估体系": "Agent 的效果怎么评估",
    "结构化输出": "怎么让模型稳定输出 JSON",
    "多 Agent 编排": "多个 Agent 怎么协作分工",
    "工具与沙箱": "工具执行怎么隔离保证安全",
    "Agent": "Agent 和普通调用模型有什么区别",
    "Agent 评估": "怎么评估 Agent 好不好用",

    # ---- general ----
    "项目复盘": "这个项目如果重做你会怎么改",
    "工程素养": "你怎么保证代码质量",
    "学习能力": "最近学了什么新技术",
    "Git 协作": "团队怎么用 Git 协作，分支怎么管",
    "代码评审与协作": "代码评审你关注什么",
    "测试": "你的测试怎么写的，覆盖率多少",
    "稳定性与可观测性": "线上出问题你靠什么发现和定位",
}


def all_topics() -> list[tuple[str, str, int]]:
    """返回 [(role, topic, 题数)]。"""
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("""
            SELECT role, topic, COUNT(*) FROM bank_questions
            GROUP BY role, topic ORDER BY role, COUNT(*) DESC
        """)
        rows = cur.fetchall()
        cur.close()
    return [(r[0], r[1], r[2]) for r in rows]


def make_query(role: str, topic: str) -> str:
    if topic in MANUAL_QUERIES:
        return MANUAL_QUERIES[topic]
    # 模板：取第一个（最口语的）
    return QUERY_TEMPLATES[0].format(topic=topic)


def evaluate(threshold: float | None = None, k: int = 5) -> dict:
    topics = all_topics()
    results = []

    for role, topic, count in topics:
        query = make_query(role, topic)

        # 用非常宽的阈值取候选，看该话题的题排第几
        hits = retriever.search_bank(query, k=200, role=role,
                                     threshold=0.0, use="select")

        # 该话题下的所有 qid
        with get_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT qid FROM bank_questions WHERE role=%s AND topic=%s",
                        (role, topic))
            own = {r[0] for r in cur.fetchall()}
            cur.close()

        if not own:
            continue

        # 该话题的题在 top-k 里占几个
        top_k_ids = {h.id for h in hits[:k]}
        hit_in_topk = len(own & top_k_ids)
        # 第一名是不是本话题
        top1_is_own = hits[0].id in own if hits else False
        # 本话题最高分
        own_scores = [h.score for h in hits if h.id in own]
        best_own = max(own_scores) if own_scores else 0.0

        # 应用真实阈值后还能剩几条（模拟实际使用）
        real_hits = [h for h in hits if h.score >= (threshold or 0.42)]
        usable = len(real_hits)

        results.append({
            "role": role, "topic": topic, "count": count, "query": query,
            "best_own_score": round(best_own, 3),
            "top1_is_own": top1_is_own,
            "hit_in_topk": hit_in_topk,
            "usable_after_threshold": usable,
        })

    n = len(results)
    top1_ok = sum(1 for r in results if r["top1_is_own"])
    topk_ok = sum(1 for r in results if r["hit_in_topk"] > 0)
    empty = [r for r in results if r["usable_after_threshold"] == 0]
    low_score = [r for r in results if r["best_own_score"] < (threshold or 0.42)]

    return {
        "total_topics": n,
        "top1_accuracy": top1_ok / n if n else 0,
        "topic_hit_rate": topk_ok / n if n else 0,
        "empty_topics": empty,
        "low_score_topics": low_score,
        "details": results,
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.42)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default="backend/tests/topic_eval.json")
    args = ap.parse_args()

    print(f"=== 全话题召回评测（阈值 {args.threshold}, top{args.k}）===")
    t0 = time.time()
    r = evaluate(threshold=args.threshold, k=args.k)
    dt = time.time() - t0

    print(f"话题数：{r['total_topics']}   耗时：{dt:.1f} 秒")
    print()
    print(f"第一名命中率（top1 是本话题的题）：{r['top1_accuracy']*100:.0f}%")
    print(f"话题命中率（top{args.k} 里至少一道本话题）：{r['topic_hit_rate']*100:.0f}%")
    print()

    print("=== 逐话题详情 ===")
    print(f"{'role':<10}{'topic':<24}{'题数':>4}{'最高分':>8}{'top1?':>7}{'topK内':>7}{'过阈值后':>9}")
    print("-" * 72)
    for d in r["details"]:
        print(f"{d['role']:<10}{d['topic'][:22]:<24}{d['count']:>4}"
              f"{d['best_own_score']:>8.3f}{'✓' if d['top1_is_own'] else '·':>7}"
              f"{d['hit_in_topk']:>7}{d['usable_after_threshold']:>9}")

    print()
    if r["empty_topics"]:
        print(f"⚠ 阈值过滤后完全为空的话题（{len(r['empty_topics'])} 个）：")
        for d in r["empty_topics"]:
            print(f"    {d['role']}/{d['topic']}  最高分 {d['best_own_score']:.3f}  查询：{d['query']}")
    else:
        print("✓ 没有话题被阈值全部过滤掉")

    if r["low_score_topics"]:
        print()
        print(f"⚠ 最高分低于阈值的话题（{len(r['low_score_topics'])} 个）：")
        for d in r["low_score_topics"]:
            print(f"    {d['role']}/{d['topic']}  {d['best_own_score']:.3f}")

    Path(args.out).write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
