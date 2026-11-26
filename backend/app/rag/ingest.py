"""向量化任务：把文档切片写库并算 embedding。

## 为什么是"任务"而不是同步接口

722 题全量向量化实测约 116 秒（161ms/条）。上传简历也类似：
一份 5 页简历切出 15-20 块，约 3-5 秒。

如果同步做，前端上传按钮要转 5 秒，用户以为卡死了。
所以拆成：
    POST /documents/upload  → 立即返回 document_id + status=pending
    GET  /documents/{id}/status → 轮询进度（pending → parsing → embedding → ready）

这样前端能显示"正在解析…正在向量化 8/15…"，体验完全不同。

## 断点续跑

`doc_chunks` 表里有 `embedding` 列，为空表示还没算。
任务重启时只处理 `WHERE embedding IS NULL` 的块，
不会重复算已完成的（省时间也省钱）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.rag.embedder import get_embedder
from app.rag.splitter import split, split_bank_question
from app.settings import settings

log = logging.getLogger("interview.ingest")

#: 内存任务表（进度查询用）。生产可换 Redis。
_TASKS: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------- 文档
def save_document(owner: str, kind: str, filename: str, raw: bytes) -> str:
    """保存原始文件并建记录，返回 document_id。"""
    sha = hashlib.sha256(raw).hexdigest()
    doc_id = "d_" + uuid.uuid4().hex[:16]

    # 去重：同一用户同一文件不重复存
    from app.repos.pg import get_conn
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM documents WHERE owner = %s AND sha256 = %s",
            (owner, sha),
        )
        row = cur.fetchone()
        if row:
            cur.close()
            return row[0]

        cur.execute(
            """INSERT INTO documents
               (id, owner, kind, filename, sha256, size_bytes, status)
               VALUES (%s, %s, %s, %s, %s, %s, 'pending')""",
            (doc_id, owner, kind, filename, sha, len(raw)),
        )
        cur.close()

    # 原始文件落盘（便于复盘"当初传的是什么"）
    d = Path(settings.upload_dir)
    d.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower()
    (d / f"{doc_id}{suffix}").write_bytes(raw)
    return doc_id


def extract_text(filename: str, raw: bytes) -> str:
    """从文件抽文本。支持 PDF / DOCX / MD / TXT。"""
    suffix = Path(filename).suffix.lower()

    if suffix in (".md", ".markdown", ".txt"):
        for enc in ("utf-8", "gbk", "utf-16"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "ignore")

    if suffix == ".pdf":
        try:
            import fitz  # pymupdf
        except ImportError as e:
            raise RuntimeError(
                "PDF 解析需要 pymupdf，请执行：pip install pymupdf"
            ) from e
        doc = fitz.open(stream=raw, filetype="pdf")
        pages = [p.get_text() for p in doc]
        doc.close()
        return "\n".join(pages)

    if suffix == ".docx":
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        import re
        # 粗暴去掉标签：docx 的正文在 <w:t> 里
        texts = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml)
        return "\n".join(t for t in texts if t.strip())

    raise ValueError(f"不支持的文件类型：{suffix}")


def ingest_document(doc_id: str, kind: str, text: str) -> dict[str, Any]:
    """切片 + 向量化 + 写库。同步执行，调用方放在后台线程里跑。"""
    from app.repos.pg import get_conn

    t0 = time.time()
    _set_status(doc_id, "parsing", progress=0)

    chunks = split(kind, text)
    if not chunks:
        _set_status(doc_id, "failed", error="切不出任何段落，请检查文件内容")
        return {"ok": False, "chunks": 0}

    # 先写文本（embedding 留空），这样即使向量化中断，文本也在
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM doc_chunks WHERE document_id = %s", (doc_id,))
        for c in chunks:
            cur.execute(
                """INSERT INTO doc_chunks (document_id, seq, project, text, parent_text)
                   VALUES (%s, %s, %s, %s, %s)""",
                (doc_id, c.seq, c.project, c.text, c.parent_text),
            )
        cur.execute("UPDATE documents SET text = %s WHERE id = %s", (text, doc_id))
        cur.close()

    _set_status(doc_id, "embedding", progress=0, total=len(chunks))

    # 再算向量（分批）
    emb = get_embedder()
    done = 0
    batch = 8
    for i in range(0, len(chunks), batch):
        part = chunks[i:i + batch]
        try:
            vecs = emb.embed([c.text for c in part])
        except Exception as e:  # noqa: BLE001
            log.warning("向量化第 %d 批失败：%s", i // batch, e)
            _set_status(doc_id, "failed", error=f"向量化失败：{e}")
            return {"ok": False, "chunks": len(chunks), "embedded": done}

        with get_conn() as conn:
            cur = conn.cursor()
            for c, v in zip(part, vecs):
                cur.execute(
                    "UPDATE doc_chunks SET embedding = %s::vector "
                    "WHERE document_id = %s AND seq = %s",
                    (_vec(v), doc_id, c.seq),
                )
            cur.close()
        done += len(part)
        _set_status(doc_id, "embedding", progress=done, total=len(chunks))

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE documents SET status = 'ready' WHERE id = %s", (doc_id,))
        cur.close()

    cost = time.time() - t0
    _set_status(doc_id, "ready", progress=len(chunks), total=len(chunks))
    log.info("文档 %s 完成：%d 块，%.1f 秒", doc_id, len(chunks), cost)
    return {"ok": True, "chunks": len(chunks), "embedded": done,
            "seconds": round(cost, 1)}


def ingest_document_async(doc_id: str, kind: str, text: str) -> None:
    """后台线程执行，立即返回。"""
    def _run():
        try:
            ingest_document(doc_id, kind, text)
        except Exception as e:  # noqa: BLE001
            log.exception("文档处理失败 %s", doc_id)
            _set_status(doc_id, "failed", error=str(e))

    t = threading.Thread(target=_run, daemon=True, name=f"ingest-{doc_id}")
    t.start()


# ---------------------------------------------------------------- 题库
def ingest_bank(bank_jsonl: Path, rebuild: bool = False) -> dict[str, Any]:
    """把 722 题题库导入 PG 并向量化。

    断点续跑：只处理 `embedding IS NULL` 的题。
    """
    from app.repos.pg import get_conn

    items = []
    for line in bank_jsonl.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            items.append(json.loads(line))
    log.info("题库共 %d 题", len(items))

    if rebuild:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("TRUNCATE bank_questions")
            cur.close()

    # 1) 先写文本
    with get_conn() as conn:
        cur = conn.cursor()
        for it in items:
            cur.execute(
                """INSERT INTO bank_questions (qid, role, topic, level, question, points, tags)
                   VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                   ON CONFLICT (qid) DO UPDATE SET
                     role = EXCLUDED.role, topic = EXCLUDED.topic,
                     level = EXCLUDED.level, question = EXCLUDED.question,
                     points = EXCLUDED.points, tags = EXCLUDED.tags""",
                (it.get("id"), it.get("role", ""), it.get("topic", ""),
                 it.get("level", ""), it.get("question", ""),
                 json.dumps(it.get("points") or [], ensure_ascii=False),
                 json.dumps(it.get("tags") or [], ensure_ascii=False)),
            )
        cur.close()

    # 2) 找还缺向量的
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT qid, question, points FROM bank_questions WHERE embedding IS NULL")
        pending = cur.fetchall()
        cur.close()

    log.info("待向量化 %d 题", len(pending))
    if not pending:
        return {"ok": True, "total": len(items), "embedded": 0}

    emb = get_embedder()
    done = 0
    batch = 8
    t0 = time.time()
    for i in range(0, len(pending), batch):
        part = pending[i:i + batch]
        texts = []
        for qid, question, points in part:
            pts = points if isinstance(points, list) else json.loads(points or "[]")
            texts.append(split_bank_question(question, pts))
        vecs = emb.embed(texts)
        with get_conn() as conn:
            cur = conn.cursor()
            for (qid, _, _), v in zip(part, vecs):
                cur.execute(
                    "UPDATE bank_questions SET embedding = %s::vector WHERE qid = %s",
                    (_vec(v), qid),
                )
            cur.close()
        done += len(part)
        if done % 80 == 0 or done == len(pending):
            log.info("题库向量化 %d/%d（%.0f 秒）", done, len(pending), time.time() - t0)

    return {"ok": True, "total": len(items), "embedded": done,
            "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- 内部
def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


def _set_status(doc_id: str, status: str, progress: int = 0,
                total: int = 0, error: str = "") -> None:
    _TASKS[doc_id] = {"status": status, "progress": progress,
                      "total": total, "error": error, "ts": time.time()}
    try:
        from app.repos.pg import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE documents SET status = %s, error = %s WHERE id = %s",
                        (status, error, doc_id))
            cur.close()
    except Exception:  # noqa: BLE001
        pass


def task_status(doc_id: str) -> dict[str, Any]:
    if doc_id in _TASKS:
        return _TASKS[doc_id]
    try:
        from app.repos.pg import get_conn
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, error FROM documents WHERE id = %s", (doc_id,))
            row = cur.fetchone()
            cur.close()
        if row:
            return {"status": row[0], "progress": 0, "total": 0, "error": row[1] or ""}
    except Exception:  # noqa: BLE001
        pass
    return {"status": "unknown", "progress": 0, "total": 0, "error": ""}
