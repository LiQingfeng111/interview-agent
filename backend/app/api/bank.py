"""题库路由：统计、列表、语义检索、导入。

## 为什么题库要进数据库

旧实现直接读 JSONL 文件（645 KB，722 题），每次请求都重新解析。
有三个问题：

1. **无法语义检索** —— 文件里没有向量
2. **每次解析** —— 722 行 JSON 约 15ms，不致命但浪费
3. **无法做"这题问过几次"统计** —— 没有持久化

进 PG 后：向量列可检索、`topic` 可聚合、`role` 可过滤。
JSONL 保留为"源头"，通过 `POST /api/bank/import` 导入。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.core import bridge, errors
from app.deps import admin_user, current_user
from app.rag import ingest
from app.rag.retriever import search_bank
from app.schemas import ApiResponse, BankSearchRequest

log = logging.getLogger("interview.api.bank")

router = APIRouter()


@router.get("/stats", response_model=ApiResponse)
def stats() -> ApiResponse:
    """题库统计：总量、按 role、按 level、按 topic。"""
    try:
        from app.repos.pg import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM bank_questions")
            total = cur.fetchone()[0]
            cur.execute("SELECT role, COUNT(*) FROM bank_questions GROUP BY role")
            by_role = dict(cur.fetchall())
            cur.execute("SELECT level, COUNT(*) FROM bank_questions GROUP BY level")
            by_level = dict(cur.fetchall())
            cur.execute(
                "SELECT COUNT(*) FROM bank_questions WHERE embedding IS NOT NULL"
            )
            embedded = cur.fetchone()[0]
            cur.close()
        return ApiResponse(data={
            "total": int(total), "embedded": int(embedded),
            "by_role": by_role, "by_level": by_level,
        })
    except Exception as e:  # noqa: BLE001
        # 数据库没导入题库时，退回读 JSONL（保证接口不挂）
        log.warning("从 PG 读题库失败，退回 JSONL：%s", e)
        return ApiResponse(data={**bridge.bank_stats(), "source": "jsonl"})


@router.get("", response_model=ApiResponse)
def list_questions(
    role: str = Query(default=""),
    topic: str = Query(default=""),
    level: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    user: dict[str, str] = Depends(current_user),
) -> ApiResponse:
    """分页列题库（诊断用，面试流程不依赖它）。"""
    try:
        from app.repos.pg import get_conn
        conds, params = [], []
        if role:
            conds.append("role = %s")
            params.append(role)
        if topic:
            conds.append("topic = %s")
            params.append(topic)
        if level:
            conds.append("level = %s")
            params.append(level)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM bank_questions {where}", tuple(params))
            total = cur.fetchone()[0]
            cur.execute(
                f"""SELECT qid, role, topic, level, question, points
                    FROM bank_questions {where}
                    ORDER BY qid LIMIT %s OFFSET %s""",
                tuple(params) + (size, (page - 1) * size),
            )
            rows = cur.fetchall()
            cur.close()

        items = [
            {"id": r[0], "role": r[1], "topic": r[2], "level": r[3],
             "question": r[4],
             "points": r[5] if isinstance(r[5], list) else []}
            for r in rows
        ]
        return ApiResponse(data={"items": items, "total": int(total),
                                 "page": page, "size": size})
    except Exception as e:  # noqa: BLE001
        log.warning("列题库失败：%s", e)
        return ApiResponse(data={"items": [], "total": 0, "page": page, "size": size})


@router.post("/search", response_model=ApiResponse)
def search(req: BankSearchRequest, user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """题库语义检索。

    这是"按要素覆盖去重"的基础：给一道题的题干+要点，
    返回语义上最接近的题，分数超阈值就是同一考点。
    """
    hits = search_bank(req.q, k=req.k, role=req.role, threshold=req.threshold)
    return ApiResponse(data={
        "query": req.q,
        "hits": [h.to_dict() for h in hits],
        "count": len(hits),
        "threshold": req.threshold,
    })


@router.post("/import", response_model=ApiResponse)
def import_bank(
    rebuild: bool = Query(default=False, description="true 则清空后重导"),
    user: dict[str, str] = Depends(admin_user),
) -> ApiResponse:
    """把 JSONL 题库导入 PG 并向量化。仅管理员。

    722 题约 116 秒，所以这是同步接口但只给管理员用。
    """
    root = bridge.ensure_legacy_importable()
    path = Path(root) / "data" / "question_bank.jsonl"
    if not path.exists():
        raise errors.AppError(404, 40400, f"题库文件不存在：{path}")

    result = ingest.ingest_bank(path, rebuild=rebuild)
    return ApiResponse(data=result)
