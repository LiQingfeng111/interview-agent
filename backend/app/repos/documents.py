"""文档读取：从 PG 取简历/JD 文本。

## 这个模块为什么差点成为"隐形 bug"

`core/service.py` 里有这么一段：

```python
try:
    from app.repos.documents import load_document_text
    doc = load_document_text(req.document_id, user)
    if doc:
        resume_text = doc
except Exception as e:
    log.warning("读文档失败：%s", e)
```

而这个模块**当时不存在**。后果：
- `import` 失败 → 被 `except` 吞掉 → 只打一行 warning
- `resume_text` 保持空 → 抛「简历里没识别到项目」
- 用户看到的现象：**上传成功了，但点开始面试报错**

诊断难度在于：上传接口 200、状态接口 ready、只有开场失败，
看起来像"简历内容解析不出来"，实际是"根本没去读"。

教训：**`except` 里不要吞 ImportError**。要么让它在启动时炸（Fail Fast），
要么至少用 `log.exception` 而不是 `log.warning`。
后面会把这条加进启动自检。

## 接口

- `load_document_text(doc_id, owner)` → str | None
- `load_chunks(doc_id, owner)` → list[dict]

都做 owner 校验：不能读别人的文档。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("interview.documents")


def load_document_text(doc_id: str, owner: str) -> str | None:
    """取文档全文。

    Args:
        doc_id: documents 表的主键
        owner: 当前用户（做归属校验，防越权读别人的简历）

    Returns:
        纯文本；文档不存在 / 不属于该用户 / 读取失败 → None
    """
    if not doc_id or not owner:
        return None

    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT text, status FROM documents WHERE id = %s AND owner = %s",
                (doc_id, owner),
            )
            row = cur.fetchone()
            cur.close()
    except Exception as e:  # noqa: BLE001
        log.exception("读取文档失败 doc_id=%s", doc_id)
        return None

    if not row:
        log.warning("文档不存在或无权访问：doc_id=%s owner=%s", doc_id, owner)
        return None

    text, status = row
    if not text or not str(text).strip():
        log.warning("文档 %s 的文本为空（status=%s），可能还在解析中", doc_id, status)
        return None

    return str(text)


def load_chunks(doc_id: str, owner: str) -> list[dict[str, Any]]:
    """取文档切片（含 parent_text，供 RAG 用）。

    一般不需要直接调它——检索走 `rag/retriever.py` 的向量查询更快。
    这里主要用于诊断和"没有向量时退化为全文"的场景。
    """
    if not doc_id or not owner:
        return []

    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """SELECT ch.seq, ch.project, ch.text, ch.parent_text
                   FROM doc_chunks ch
                   JOIN documents d ON d.id = ch.document_id
                   WHERE d.id = %s AND d.owner = %s
                   ORDER BY ch.seq""",
                (doc_id, owner),
            )
            rows = cur.fetchall()
            cur.close()
    except Exception as e:  # noqa: BLE001
        log.exception("读取切片失败 doc_id=%s", doc_id)
        return []

    return [
        {"seq": r[0], "project": r[1] or "", "text": r[2] or "", "parent_text": r[3] or ""}
        for r in rows
    ]


def latest_document(owner: str, kind: str = "resume") -> dict[str, Any] | None:
    """取某用户最新的某类文档（进程重启恢复时用）。

    为什么要有"最新"这个语义：恢复会话时不知道当初用的是哪份简历，
    用最新的最合理（也是 `recover.py` 的策略）。
    """
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """SELECT id, filename, text, status, created_at
                   FROM documents
                   WHERE owner = %s AND kind = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (owner, kind),
            )
            row = cur.fetchone()
            cur.close()
    except Exception as e:  # noqa: BLE001
        log.exception("查最新文档失败 owner=%s", owner)
        return None

    if not row:
        return None
    return {
        "id": row[0], "filename": row[1], "text": row[2] or "",
        "status": row[3],
        "created_at": row[4].isoformat() if row[4] else "",
    }


def document_status(doc_id: str, owner: str) -> dict[str, Any] | None:
    """查处理状态（上传后前端轮询用）。"""
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """SELECT d.status, d.error,
                          (SELECT COUNT(*) FROM doc_chunks ch WHERE ch.document_id = d.id),
                          (SELECT COUNT(*) FROM doc_chunks ch
                            WHERE ch.document_id = d.id AND ch.embedding IS NOT NULL)
                   FROM documents d WHERE d.id = %s AND d.owner = %s""",
                (doc_id, owner),
            )
            row = cur.fetchone()
            cur.close()
    except Exception as e:  # noqa: BLE001
        log.exception("查文档状态失败 doc_id=%s", doc_id)
        return None

    if not row:
        return None
    return {"status": row[0], "error": row[1] or "",
            "chunks": row[2], "embedded": row[3]}
