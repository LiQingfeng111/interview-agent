"""诊断路由：RAG 检索可视化、阈值调参、组件自检。

## 为什么需要

三个真实场景：

1. **调阈值**：0.45 是 18 条样例标定出来的，太薄（正负间隔 0.022）。
   上线后要能随时用真实数据复算——这个接口给"给定 query，看召回什么、各多少分"。

2. **排查"为什么问了这个问题"**：面试官问了奇怪的问题，
   要能回查"当时简历检索到了哪一段、分数多少"。

3. **回归验证**：换 embedding 模型后，用同一批 query 对比召回变化。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.deps import admin_user, current_user
from app.rag import retriever
from app.rag.embedder import get_embedder
from app.schemas import ApiResponse, RagSearchRequest
from app.settings import settings

log = logging.getLogger("interview.api.diagnostics")

router = APIRouter()


@router.get("/thresholds", response_model=ApiResponse)
def thresholds() -> ApiResponse:
    """当前检索阈值（改配置后能立即看到生效值）。"""
    return ApiResponse(data={
        "resume": settings.threshold_resume,
        "bank": settings.threshold_bank,
        "history": settings.threshold_history,
        "web": settings.threshold_web,
        "note": "bge-m3 分布保守，同义句约 0.70；阈值按索引分别标定",
    })


@router.post("/rag/search", response_model=ApiResponse)
def rag_search(req: RagSearchRequest,
               user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """给一个 query，看它在某个索引里召回什么、分数多少。

    index: resume / jd / bank / history
    """
    idx = req.index
    if idx in ("resume", "jd"):
        hits = (retriever.search_resume if idx == "resume" else retriever.search_jd)(
            req.q, owner=user["user"], k=req.k, threshold=req.threshold)
    elif idx == "bank":
        hits = retriever.search_bank(req.q, k=req.k,
                                     threshold=None if req.threshold is None
                                     else req.threshold)
    elif idx == "history":
        hits = retriever.search_history(req.q, owner=user["user"], k=req.k,
                                        threshold=req.threshold)
    else:
        return ApiResponse(code=400, message=f"未知索引：{idx}（可选 resume/jd/bank/history）")

    context = retriever.build_context(hits)
    return ApiResponse(data={
        "index": idx,
        "query": req.q,
        "k": req.k,
        "threshold": req.threshold if req.threshold is not None
        else retriever._threshold_for(idx),
        "hits": [h.to_dict() for h in hits],
        "count": len(hits),
        "context": context,
        "context_chars": len(context),
    })


@router.post("/embed", response_model=ApiResponse)
def embed_texts(payload: dict[str, Any],
                user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """把文本变成向量（用于人工核对相似度）。

    典型用法：验证"召回71%→82%"和"recall 0.71→0.82"到底多像。
    """
    texts = payload.get("texts") or []
    if not isinstance(texts, list) or not texts:
        return ApiResponse(code=400, message="texts 需要是非空字符串数组")

    emb = get_embedder()
    try:
        vecs = emb.embed([str(t) for t in texts])
    except Exception as e:  # noqa: BLE001
        return ApiResponse(code=503, message=f"向量化失败：{e}")

    def cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return round(dot / (na * nb), 4) if na and nb else 0.0

    pairs = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            pairs.append({"a": str(texts[i])[:40], "b": str(texts[j])[:40],
                          "cosine": cos(vecs[i], vecs[j])})

    return ApiResponse(data={
        "dim": len(vecs[0]) if vecs else 0,
        "count": len(vecs),
        "pairs": pairs,
    })


@router.get("/schema", response_model=ApiResponse)
def schema_info(user: dict[str, str] = Depends(admin_user)) -> ApiResponse:
    """数据库表结构与行数（运维用）。"""
    try:
        from app.repos.pg import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public' ORDER BY table_name
            """)
            tables = [r[0] for r in cur.fetchall()]
            detail = {}
            for t in tables:
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    detail[t] = cur.fetchone()[0]
                except Exception:  # noqa: BLE001
                    detail[t] = -1
            cur.close()
        return ApiResponse(data={"tables": detail})
    except Exception as e:  # noqa: BLE001
        return ApiResponse(code=503, message=f"数据库不可用：{e}")


@router.post("/schema/init", response_model=ApiResponse)
def schema_init(user: dict[str, str] = Depends(admin_user)) -> ApiResponse:
    """建表 + 建向量索引（幂等）。部署时调一次。"""
    try:
        from app.repos.pg import init_schema
        return ApiResponse(data=init_schema())
    except Exception as e:  # noqa: BLE001
        log.exception("建表失败")
        return ApiResponse(code=503, message=f"建表失败：{e}")
