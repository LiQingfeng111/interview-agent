"""PostgreSQL 访问：连接池 + 建表。

## 为什么现在才用连接池

旧实现用 SQLite，`sqlite3.connect()` 每次都新建（文件库够快，代价低）。
换 PG 后每次连接要走 TCP + 认证（约 5-15ms），
一个面试 20 次请求就是 300ms 纯开销。所以必须池化。

## 为什么不用 SQLAlchemy

理由和"不用 Flask"类似：这一层只需要 `execute(sql, params)`，
ORM 带来的是模型定义、关系映射、迁移工具一整套。
但**迁移确实是需要的**（表会演进），所以后面用 Alembic 单独管 SQL 迁移，
不让 ORM 插手运行时。

例外：如果后面发现手写 SQL 维护不动，再引 SQLAlchemy 也不迟——
Repository 接口已经把访问方式隔离了。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from app.settings import settings

log = logging.getLogger("interview.pg")

_pool: Any = None

#: 建表语句（幂等）。顺序有意义：被引用的表在前。
SCHEMA_SQL = [
    # ---------------------------------------------------------- 用户
    """
    CREATE TABLE IF NOT EXISTS users (
        id          BIGSERIAL PRIMARY KEY,
        username    VARCHAR(64) UNIQUE NOT NULL,
        pwd_hash    TEXT NOT NULL,
        role        VARCHAR(16) NOT NULL DEFAULT 'user',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_login  TIMESTAMPTZ
    )
    """,
    # ---------------------------------------------------------- 文档（简历/JD）
    """
    CREATE TABLE IF NOT EXISTS documents (
        id          VARCHAR(40) PRIMARY KEY,
        owner       VARCHAR(64) NOT NULL,
        kind        VARCHAR(16) NOT NULL,          -- resume / jd
        filename    TEXT NOT NULL,
        sha256      VARCHAR(64) NOT NULL,
        size_bytes  BIGINT NOT NULL DEFAULT 0,
        text        TEXT NOT NULL DEFAULT '',
        status      VARCHAR(16) NOT NULL DEFAULT 'pending',
        error       TEXT NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (owner, sha256)
    )
    """,
    # ---------------------------------------------------------- 文档切片 + 向量
    """
    CREATE TABLE IF NOT EXISTS doc_chunks (
        id           BIGSERIAL PRIMARY KEY,
        document_id  VARCHAR(40) NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        seq          INT NOT NULL,
        project      TEXT NOT NULL DEFAULT '',     -- 所属项目名（用于取 parent 上下文）
        text         TEXT NOT NULL,
        parent_text  TEXT NOT NULL DEFAULT '',     -- 整个项目的完整描述（简历连贯性）
        embedding    vector(1024)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc ON doc_chunks(document_id, seq)",
    # ---------------------------------------------------------- 题库（从 JSONL 导入）
    """
    CREATE TABLE IF NOT EXISTS bank_questions (
        qid         VARCHAR(64) PRIMARY KEY,
        role        VARCHAR(32) NOT NULL,
        topic       TEXT NOT NULL,
        level       VARCHAR(16) NOT NULL,
        question    TEXT NOT NULL,
        points      JSONB NOT NULL DEFAULT '[]'::jsonb,
        tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
        embedding   vector(1024)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bank_role ON bank_questions(role)",
    # ---------------------------------------------------------- 会话（热态快照）
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id           VARCHAR(40) PRIMARY KEY,
        owner        VARCHAR(64) NOT NULL,
        role         VARCHAR(32) NOT NULL DEFAULT '',
        persona      VARCHAR(32) NOT NULL DEFAULT '',
        phase        VARCHAR(32) NOT NULL DEFAULT '',
        status       VARCHAR(16) NOT NULL DEFAULT 'running',
        max_turns    INT NOT NULL DEFAULT 16,
        total_turns  INT NOT NULL DEFAULT 0,
        finish_reason TEXT NOT NULL DEFAULT '',
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner, created_at DESC)",
    # 成本累计列（会话级汇总，重启后仍能读到）
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS total_tokens BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS total_cost_cny DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS llm_calls INT NOT NULL DEFAULT 0",
    # ---------------------------------------------------------- 每轮问答（含向量，供跨会话记忆）
    """
    CREATE TABLE IF NOT EXISTS turns (
        id          BIGSERIAL PRIMARY KEY,
        session_id  VARCHAR(40) NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        seq         INT NOT NULL,
        question    TEXT NOT NULL,
        answer      TEXT NOT NULL DEFAULT '',
        verdict     VARCHAR(16) NOT NULL DEFAULT '',
        score       DOUBLE PRECISION NOT NULL DEFAULT 0,
        phase       VARCHAR(32) NOT NULL DEFAULT '',
        element     VARCHAR(32) NOT NULL DEFAULT '',
        tokens_in   INT NOT NULL DEFAULT 0,
        tokens_out  INT NOT NULL DEFAULT 0,
        cost_cny    DOUBLE PRECISION NOT NULL DEFAULT 0,
        degraded    BOOLEAN NOT NULL DEFAULT FALSE,
        embedding   vector(1024),
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (session_id, seq)
    )
    """,
    # 五个评分维度单独存列。
    #
    # 为什么必须单独列而不是塞 JSON：
    # ① 报告页雷达图要按维度聚合（`AVG(accuracy)` 这种），JSON 里查不方便
    # ② 这是项目最核心的产出数据（"5 维评分"），值得一等公民待遇
    #
    # 踩过的坑：第一版没这几列，只把 avg 写进 score，
    # 结果报告页雷达图永远是 0（前端从 turns 重建时拿不到维度分）。
    "ALTER TABLE turns ADD COLUMN IF NOT EXISTS accuracy DOUBLE PRECISION",
    "ALTER TABLE turns ADD COLUMN IF NOT EXISTS depth DOUBLE PRECISION",
    "ALTER TABLE turns ADD COLUMN IF NOT EXISTS tradeoff DOUBLE PRECISION",
    "ALTER TABLE turns ADD COLUMN IF NOT EXISTS boundary DOUBLE PRECISION",
    "ALTER TABLE turns ADD COLUMN IF NOT EXISTS clarity DOUBLE PRECISION",
    # 维度分也可能为空（早期数据），给个默认值便于聚合
    "UPDATE turns SET accuracy = COALESCE(accuracy, score), depth = COALESCE(depth, score) "
    "WHERE accuracy IS NULL AND score > 0",
    # ---------------------------------------------------------- 事件账本（只追加）
    """
    CREATE TABLE IF NOT EXISTS events (
        session_id  VARCHAR(40) NOT NULL,
        seq         INT NOT NULL,
        type        VARCHAR(48) NOT NULL,
        payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
        prev_hash   VARCHAR(64) NOT NULL DEFAULT '',
        hash        VARCHAR(64) NOT NULL DEFAULT '',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, seq)
    )
    """,
    # ---------------------------------------------------------- 报告
    """
    CREATE TABLE IF NOT EXISTS reports (
        session_id  VARCHAR(40) PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
        markdown    TEXT NOT NULL DEFAULT '',
        structured  JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ---------------------------------------------------------- 长期记忆
    """
    CREATE TABLE IF NOT EXISTS candidate_profile (
        username        VARCHAR(64) PRIMARY KEY,
        weak_elements   JSONB NOT NULL DEFAULT '{}'::jsonb,
        strong_topics   JSONB NOT NULL DEFAULT '[]'::jsonb,
        weak_topics     JSONB NOT NULL DEFAULT '[]'::jsonb,
        sessions_count  INT NOT NULL DEFAULT 0,
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ---------------------------------------------------------- 向量索引
    # HNSW：查询快、写入稍慢，10 万条以内都够用
    # m=16 / ef_construction=64 是 pgvector 建议的默认档
    """
    CREATE INDEX IF NOT EXISTS idx_doc_chunks_vec ON doc_chunks
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_bank_vec ON bank_questions
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_turns_vec ON turns
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """,
]


def _make_pool():
    """创建连接池。psycopg_pool 缺失时退化为每次新建连接（开发可用）。"""
    global _pool
    if _pool is not None:
        return _pool

    dsn = settings.database_url
    try:
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(
            dsn,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            timeout=10,
            kwargs={"autocommit": False},
        )
        _pool.wait(timeout=10)
        log.info("PG 连接池已建立 min=%d max=%d", settings.db_pool_min, settings.db_pool_max)
    except Exception as e:  # noqa: BLE001
        log.warning("连接池不可用，退化为单连接模式：%s", e)
        _pool = _SingleConn(dsn)
    return _pool


class _SingleConn:
    """最小可用替代：每次需要时新建连接。

    为什么要有它：演示环境可能没装 psycopg_pool，
    这时候不该整个服务起不来——性能差但能用。
    """

    def __init__(self, dsn: str):
        self.dsn = dsn

    def connection(self):
        return _ConnCtx(self.dsn)

    def close(self):
        pass


class _ConnCtx:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = None

    def __enter__(self):
        import psycopg
        self.conn = psycopg.connect(self.dsn, autocommit=False)
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if self.conn is None:
            return
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()


@contextmanager
def get_conn() -> Iterator[Any]:
    """取一个连接（上下文管理器，自动提交/回滚/归还）。"""
    pool = _make_pool()
    with pool.connection() as conn:
        yield conn


def init_schema() -> dict[str, Any]:
    """建扩展 + 建表 + 建索引。返回执行摘要。"""
    results = []
    with get_conn() as c:
        cur = c.cursor()
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            results.append("CREATE EXTENSION vector")
        except Exception as e:  # noqa: BLE001
            log.exception("创建 vector 扩展失败")
            raise RuntimeError(f"pgvector 扩展不可用：{e}") from e

        for i, sql in enumerate(SCHEMA_SQL):
            one_line = " ".join(sql.split())[:70]
            try:
                cur.execute(sql)
                results.append(f"[{i:02d}] ok  {one_line}")
            except Exception as e:  # noqa: BLE001
                results.append(f"[{i:02d}] ERR {one_line} → {e}")
                raise
        cur.close()
    return {"ok": True, "statements": len(results), "detail": results}


def pg_health() -> dict[str, Any]:
    """健康检查：连通性 + 表数量 + 向量扩展版本。"""
    try:
        with get_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.execute("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
            tables = cur.fetchone()[0]
            cur.execute("""
                SELECT extversion FROM pg_extension WHERE extname = 'vector'
            """)
            row = cur.fetchone()
            vec = row[0] if row else None
            cur.close()
        return {"ok": True, "tables": int(tables), "pgvector": vec}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
