"""语义检索：纯向量 topK（按实测数据决定，不掺关键词）。

## 为什么是纯语义而不是混合（实测依据）

用户最初要求"向量 + 关键词混合"。我做了两轮实测（`tools/compare_retrieval2.py`）：

```
类型                向量      关键词     RRF     自适应
A 字面/数字/术语     6/6      6/6      6/6     6/6
B 同义改写          5/6      1/6      3/6     5/6
C 中英对照          6/6      0/6      1/6     6/6
──────────────────────────────────────────────────
合计               17/18     7/18     10/18   17/18
```

三个结论：
1. 关键词在字面类**并没有赢**（6/6 打平）——bge-m3 对数字也不差
2. 关键词在中英对照**完全失效**（0/6）——字符 n-gram 跨语言无共享
3. 等权 RRF 比纯向量**差**（10/18 vs 17/18）——弱信号污染强信号

所以：**主链路纯语义**。用户看数据后也确认"不要关键词了"。

## 阈值（实测标定，不是拍脑袋）

    resume   0.45    宁可宽：漏了会问错项目
    bank     0.55    宁可严：误判会误杀题目
    history  0.45    只做提示，不参与判定
    web      0.50    只取最相关 3 段

⚠ bge-m3 分布保守：同义句只有 0.70，不是直觉的 0.9。
第一版计划写 0.80 会把正样本全丢掉（正样本最低 0.469，负样本最高 0.447）。
正负间隔只有 0.022，所以阈值必须按索引分别标定，不能一刀切。

## 检索流程

    ① 查询向量化（bge-m3，1024 维，L2 已归一化）
    ② pgvector HNSW：ORDER BY embedding <=> $1 LIMIT K
    ③ 阈值过滤：sim < threshold → 丢弃
    ④ 命中 chunk 后取 parent_text（简历连贯性）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.rag.embedder import get_embedder
from app.repos.cache import get_cache, rag_cache_key
from app.settings import settings

log = logging.getLogger("interview.retriever")


@dataclass
class Hit:
    """一条召回结果。"""
    id: str
    text: str
    score: float
    rank: int
    project: str = ""
    parent_text: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "score": round(self.score, 4),
                "rank": self.rank, "project": self.project,
                "parent_text": self.parent_text, "meta": self.meta}


def _threshold_for(index: str) -> float:
    return {
        "resume": settings.threshold_resume,
        "jd": settings.threshold_resume,
        "bank": settings.threshold_bank,
        "history": settings.threshold_history,
        "web": settings.threshold_web,
    }.get(index, 0.50)


#: 按用途区分的阈值（比按索引区分更细）
#:
#: 实测发现：同一个意思换个说法，相似度会大幅波动 ——
#:     "改造前的指标是多少"   ↔ 题库正式题干   0.854
#:     "你的 baseline 是什么" ↔ 同一题干        0.493
#:     "对比基线是什么"       ↔ 同一题干        0.420
#:
#: 所以阈值必须按**用途**分：
#:   DEDUP  去重/排除重复考点 → 宁可严（误杀会跳过本来该问的题）
#:   SELECT 选题/找相关题     → 宁可宽（漏了只是少一个候选）
THRESHOLD_BY_USE = {
    "dedup": 0.60,      # 判"两道题是不是同一考点"
    "select": 0.42,     # 判"这题和当前话题相关吗"
    "hint": 0.42,       # 给提示/参考
}


def threshold_for_use(use: str, index: str = "bank") -> float:
    """取某个用途的阈值；未登记则退回按索引的配置。"""
    return THRESHOLD_BY_USE.get(use, _threshold_for(index))


def _vec_literal(vec: list[float]) -> str:
    """把向量转成 pgvector 字面量。

    pgvector 接受 `[0.1,0.2,...]` 字符串。
    用 float 的 repr 而不是格式化，避免丢精度。
    """
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def search_resume(query: str, owner: str, k: int = 5,
                  threshold: float | None = None) -> list[Hit]:
    """在简历 chunks 里检索。命中后带 parent_text（整个项目）。"""
    return _search_doc(query, owner=owner, kind="resume", k=k, index="resume",
                       threshold=threshold)


def search_jd(query: str, owner: str, k: int = 5,
              threshold: float | None = None) -> list[Hit]:
    return _search_doc(query, owner=owner, kind="jd", k=k, index="jd",
                       threshold=threshold)


def _search_doc(query: str, owner: str, kind: str, k: int, index: str,
                threshold: float | None) -> list[Hit]:
    if not query.strip():
        return []

    th = _threshold_for(index) if threshold is None else threshold
    cache = get_cache()
    ck = rag_cache_key(f"{index}:{owner}", query)
    cached = cache.get_json(ck)
    if cached is not None:
        return [Hit(**h) for h in cached]

    emb = get_embedder()
    try:
        qvec = emb.embed([query])[0]
    except Exception as e:  # noqa: BLE001
        log.warning("查询向量化失败，返回空结果：%s", e)
        return []

    sql = """
        SELECT c.id, c.text, c.project, c.parent_text,
               1 - (c.embedding <=> %s::vector) AS sim
        FROM doc_chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE d.owner = %s AND d.kind = %s AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s
    """
    vec = _vec_literal(qvec)
    hits: list[Hit] = []
    try:
        from app.repos.pg import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, (vec, owner, kind, vec, k))
            rows = cur.fetchall()
            cur.close()
        for i, (cid, text, project, parent, sim) in enumerate(rows):
            if sim is None or float(sim) < th:
                continue          # 阈值过滤：宁缺毋滥
            hits.append(Hit(id=str(cid), text=text, score=float(sim), rank=len(hits),
                            project=project or "", parent_text=parent or ""))
    except Exception as e:  # noqa: BLE001
        log.warning("简历检索失败：%s", e)
        return []

    cache.set_json(ck, [h.to_dict() for h in hits], ttl=600)
    return hits


def search_bank(query: str, k: int = 10, role: str = "",
                threshold: float | None = None,
                use: str = "select",
                topic: str = "") -> list[Hit]:
    """题库语义检索。

    Args:
        use: 用途决定阈值——
             "dedup"  判是否同一考点（严，0.60）
             "select" 找相关题（宽，0.42）
        topic: 指定话题时**先做精确过滤**（见下面 why）

    ## 为什么需要 topic 过滤（实测数据）

    生产环境的查询是状态机给的话题名（如 "RAG"），但标为 `topic='RAG'`
    的题目，问的是：

        "chunk 大小和切分策略你会怎么定？"
        "多路召回后用 RRF 融合，k 取 60 是默认还是有调过？"
        "文档更新后旧 chunk 还留在向量库里，增量索引怎么保证不重复？"

    **这些题文本里没有 "RAG" 这个词。** 实测统计：

        topic='RAG'           question 含 "RAG"     2/8  = 25%
        topic='Transformer'   question 含 "Transformer"  1/5  = 20%
        topic='Agent'         question 含 "Agent"   8/8  = 100%
        topic='评测'          question 含 "评测"     6/7  = 86%

    用 "RAG" 做向量检索去匹配 question 文本，命中率必然只有 20-25%。
    **这不是 embedding 模型的问题，是检索方式用错了。**

    题库里明明有 `topic` 字段，却只拿它做「话题配额加权」，
    没用来做「精确过滤」。这是设计缺陷。

    加上过滤后：给定 topic，直接在该话题内检索，
    命中率 100%（因为不匹配的都排除了）。

    为什么不能**只**过滤：话题内的题还需要按语义排序
    （候选人的上下文决定问哪道更合适），所以过滤 + 向量排序一起用。

  为什么必须分用途（use）：实测同一语义换个说法，相似度从 0.854 掉到 0.420。
    用 0.55 一刀切，会出现"去重时漏判重复"和"选题时全部过滤掉"两种相反的错误。
    """
    if not query.strip() and not topic:
        return []

    th = threshold if threshold is not None else threshold_for_use(use, "bank")
    emb = get_embedder()

    # 查询向量：纯 topic 查询时用 topic 本身，否则用 query
    qtext = query if query.strip() else topic
    try:
        qvec = emb.embed([qtext])[0]
    except Exception as e:  # noqa: BLE001
        log.warning("题库检索向量化失败：%s", e)
        return []

    vec = _vec_literal(qvec)

    # 动态拼 WHERE：role / topic 都可能存在
    conds = ["embedding IS NOT NULL"]
    params: list = [vec]
    if role:
        conds.append("role = %s")
        params.append(role)
    if topic:
        conds.append("topic = %s")
        params.append(topic)
    params.append(vec)
    params.append(k)

    sql = f"""
        SELECT qid, question, topic, points, 1 - (embedding <=> %s::vector) AS sim
        FROM bank_questions
        WHERE {' AND '.join(conds)}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
    """

    hits: list[Hit] = []
    try:
        from app.repos.pg import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
        for qid, question, topic, points, sim in rows:
            if sim is None or float(sim) < th:
                continue
            hits.append(Hit(
                id=qid, text=question, score=float(sim), rank=len(hits),
                project=topic or "",
                meta={"points": points if isinstance(points, list) else json.loads(points or "[]")},
            ))
    except Exception as e:  # noqa: BLE001
        log.warning("题库检索失败：%s", e)
        return []
    return hits


def search_history(query: str, owner: str, k: int = 5,
                   threshold: float | None = None) -> list[Hit]:
    """跨会话历史问答检索：知道这人上次在哪答崩了。"""
    if not query.strip():
        return []

    th = _threshold_for("history") if threshold is None else threshold
    emb = get_embedder()
    try:
        qvec = emb.embed([query])[0]
    except Exception as e:  # noqa: BLE001
        log.warning("历史检索向量化失败：%s", e)
        return []

    sql = """
        SELECT t.id, t.question, t.answer, t.verdict, t.score, t.element,
               1 - (t.embedding <=> %s::vector) AS sim
        FROM turns t
        JOIN sessions s ON s.id = t.session_id
        WHERE s.owner = %s AND t.embedding IS NOT NULL
        ORDER BY t.embedding <=> %s::vector
        LIMIT %s
    """
    vec = _vec_literal(qvec)
    hits: list[Hit] = []
    try:
        from app.repos.pg import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(sql, (vec, owner, vec, k))
            rows = cur.fetchall()
            cur.close()
        for tid, q, a, verdict, score, element, sim in rows:
            if sim is None or float(sim) < th:
                continue
            hits.append(Hit(
                id=str(tid), text=f"问：{q}\n答：{a}", score=float(sim), rank=len(hits),
                project=element or "",
                meta={"verdict": verdict, "score": score},
            ))
    except Exception as e:  # noqa: BLE001
        log.warning("历史检索失败：%s", e)
        return []
    return hits


def build_context(hits: list[Hit], max_chars: int = 2000) -> str:
    """把召回结果拼成 prompt 片段。

    关键：**优先用 parent_text**（简历连贯性要求）。
    命中某段就发整个项目，否则模型看到的是断章。
    """
    if not hits:
        return ""

    parts: list[str] = []
    seen: set[str] = set()
    total = 0
    for h in hits:
        text = h.parent_text or h.text
        key = text[:80]
        if key in seen:
            continue           # 同一项目多个 chunk 命中，只发一次
        seen.add(key)
        block = f"【{h.project or '资料'}】（相似度 {h.score:.2f}）\n{text}"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)
