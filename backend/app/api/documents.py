"""文档路由：上传简历/JD，切片，向量化，查进度。

## 为什么上传要异步

实测：一份简历切 15-20 块，向量化每块 161ms → 约 3 秒。
722 题全量约 116 秒。

同步做的话，前端上传按钮转 3 秒用户以为卡死。
所以：
    POST /api/documents/upload  → 立即返回 {document_id, status: pending}
    GET  /api/documents/{id}/status → 轮询 {status, progress, total}
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.core import errors
from app.deps import current_user
from app.rag import ingest
from app.schemas import ApiResponse
from app.settings import settings

log = logging.getLogger("interview.api.documents")

router = APIRouter()

ALLOWED_SUFFIX = [".pdf", ".docx", ".md", ".markdown", ".txt"]


@router.post("/upload", response_model=ApiResponse)
async def upload(
    file: UploadFile = File(...),
    kind: str = Form(default="resume"),
    user: dict[str, str] = Depends(current_user),
) -> ApiResponse:
    """上传简历或 JD。

    kind: resume（简历）| jd（岗位描述）
    """
    if kind not in ("resume", "jd"):
        raise errors.unknown_role(kind, ["resume", "jd"])

    filename = file.filename or "unnamed"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        raise errors.file_unsupported(suffix, ALLOWED_SUFFIX)

    raw = await file.read()
    size_mb = len(raw) / 1024 / 1024
    if size_mb > settings.upload_max_mb:
        raise errors.file_too_large(size_mb, settings.upload_max_mb)

    if not raw:
        raise errors.file_too_large(0, settings.upload_max_mb)

    # 抽文本（PDF/DOCX 在这里就要解析，失败要立即报）
    try:
        text = ingest.extract_text(filename, raw)
    except Exception as e:  # noqa: BLE001
        log.warning("解析 %s 失败：%s", filename, e)
        raise errors.AppError(422, 41004,
                             f"简历解析失败：{e}", {"filename": filename}) from e

    if not text.strip():
        raise errors.resume_no_project()

    doc_id = ingest.save_document(user["user"], kind, filename, raw)

    # 后台向量化
    ingest.ingest_document_async(doc_id, kind, text)

    return ApiResponse(data={
        "id": doc_id,
        "kind": kind,
        "filename": filename,
        "size_bytes": len(raw),
        "text_chars": len(text),
        "status": "pending",
    })


@router.get("/{doc_id}/status", response_model=ApiResponse)
def status(doc_id: str, user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """查向量化进度：pending → parsing → embedding → ready / failed。"""
    st = ingest.task_status(doc_id)
    return ApiResponse(data={"id": doc_id, **st})


@router.get("", response_model=ApiResponse)
def list_documents(kind: str = Query(default=""), user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """列出当前用户的文档。"""
    try:
        from app.repos.pg import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            if kind:
                cur.execute(
                    """SELECT d.id, d.kind, d.filename, d.size_bytes, d.status, d.created_at,
                              (SELECT COUNT(*) FROM doc_chunks c WHERE c.document_id = d.id)
                       FROM documents d WHERE d.owner = %s AND d.kind = %s
                       ORDER BY d.created_at DESC""",
                    (user["user"], kind),
                )
            else:
                cur.execute(
                    """SELECT d.id, d.kind, d.filename, d.size_bytes, d.status, d.created_at,
                              (SELECT COUNT(*) FROM doc_chunks c WHERE c.document_id = d.id)
                       FROM documents d WHERE d.owner = %s
                       ORDER BY d.created_at DESC""",
                    (user["user"],),
                )
            rows = cur.fetchall()
            cur.close()
        items = [
            {"id": r[0], "kind": r[1], "filename": r[2], "size_bytes": r[3],
             "status": r[4], "created_at": r[5].isoformat() if r[5] else "",
             "chunks": r[6]}
            for r in rows
        ]
        return ApiResponse(data={"items": items})
    except Exception as e:  # noqa: BLE001
        log.warning("列文档失败：%s", e)
        return ApiResponse(data={"items": []})


@router.post("/{doc_id}/reingest", response_model=ApiResponse)
def reingest(doc_id: str, user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """重新切片 + 向量化（换模型或改了切片策略后用）。"""
    try:
        from app.repos.pg import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT kind, text FROM documents WHERE id = %s AND owner = %s",
                        (doc_id, user["user"]))
            row = cur.fetchone()
            cur.close()
        if not row:
            raise errors.session_missing_id()
        kind, text = row
        ingest.ingest_document_async(doc_id, kind, text or "")
        return ApiResponse(data={"id": doc_id, "status": "queued"})
    except errors.AppError:
        raise
    except Exception as e:  # noqa: BLE001
        raise errors.db_unavailable(str(e)) from e
