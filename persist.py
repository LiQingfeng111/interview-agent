"""会话持久化：SQLite 存会话，重启后还能接着答。

为什么这是必须补的（对标 real-mock 等项目的 SQLite 方案）：
  现在会话只活在进程内存里，关窗口 = 全丢。面试是 20~40 分钟的事，
  中途断网、误关、机器重启都很常见；一丢就是"报告都没了"。
  文件里只有报告，正文对话与评分记录一起没了。

设计取舍：
  - 只存**能重建会话的最小集**：简历项目、岗位、回合（问/答/评分）、账本路径。
    不存模型对象、不存锁；恢复时重新构造 InterviewSession 再灌回合。
  - 用 JSON 列存结构，避免 SQLite 表结构随字段改动迁移。
  - WAL 模式：边写边读不打架；单用户场景够用，不需要上服务。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("IA_SESSIONS_DB") or os.path.join(ROOT, "data", "sessions.db")

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    sid         TEXT PRIMARY KEY,
    user        TEXT DEFAULT 'local',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    role        TEXT,
    phase       TEXT,
    finish_reason TEXT,
    turns       INTEGER DEFAULT 0,
    avg_score   REAL DEFAULT 0,
    payload     TEXT NOT NULL      -- 完整会话快照（JSON）
);
"""

# 索引单独建：`sessions(user, ...)` 依赖 user 列，
# 老库没有这一列时先建索引会直接报 no such column（踩过一次，整个后台 500）
INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user, updated_at DESC);
"""

SCHEMA = TABLE_SQL + INDEX_SQL


def _conn(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(TABLE_SQL)
    # 老库升级：user 列是后加的，不迁移会让已有库一查就报 no such column
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "user" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN user TEXT")
    conn.executescript(INDEX_SQL)
    return conn


def snapshot(session) -> dict:
    """把会话拍成可存的最小快照。"""
    turns = []
    for t in getattr(session, "turns", []):
        ev = getattr(t, "evaluation", None)
        turns.append({
            "idx": t.idx, "phase": t.phase, "question": t.question, "answer": t.answer,
            "project": getattr(t, "project", ""), "ladder_level": getattr(t, "ladder_level", 0),
            "eval": ev.to_dict() if ev else None,
        })
    meta = session.result().to_dict().get("meta", {}) if hasattr(session, "result") else {}
    return {
        "role": getattr(session, "role_key", ""),
        "persona": getattr(session, "persona", "balanced"),
        "max_turns": getattr(session, "max_turns", 16),
        "projects": [p.name for p in (getattr(session, "projects", None) or [])],
        "phase": getattr(session.sm, "phase", ""),
        "finish_reason": getattr(session, "finish_reason", ""),
        "turns": turns,
        "ledger_path": getattr(getattr(session, "ledger", None), "path", ""),
        "cards": getattr(session, "cards", []),
        # 当前待答的问题必须存：否则恢复后 step() 不知道在等哪个问题的回答，
        # 那一轮会被静默跳过（实测：恢复后继续答，轮次不增长）
        "pending_question": getattr(session, "_pending_question", ""),
        "pending_target": getattr(session, "_pending_target", ""),
        # stats 必须存：跨会话趋势要按维度聚合，
        # 之前只存了 coverage_ratio，导致 trend() 的 dim_avg 永远是空 dict
        "meta": {k: meta.get(k) for k in ("coverage_ratio", "stats", "profile",
                                          "diagnosis", "briefing")},
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save(session, path: str = DB_PATH, user: str = "local") -> str:
    """写入/覆盖一条会话（每轮调用一次，成本就是一次小事务）。"""
    data = snapshot(session)
    sid = getattr(getattr(session, "ledger", None), "session_id", "") or \
        getattr(session, "_sid", "")
    stats = (session.result().to_dict().get("meta") or {}).get("stats") or {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _conn(path) as conn:
        conn.execute(
            """INSERT INTO sessions (sid, user, created_at, updated_at, role, phase,
                                     finish_reason, turns, avg_score, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(sid) DO UPDATE SET
                 user=excluded.user, updated_at=excluded.updated_at, phase=excluded.phase,
                 finish_reason=excluded.finish_reason, turns=excluded.turns,
                 avg_score=excluded.avg_score, payload=excluded.payload""",
            (sid or f"sid_{int(time.time())}", user or "local", now, now, data["role"],
             data["phase"], data["finish_reason"], len(data["turns"]),
             float(stats.get("avg") or 0), json.dumps(data, ensure_ascii=False)),
        )
    return sid


def load(sid: str, path: str = DB_PATH) -> dict | None:
    with _conn(path) as conn:
        row = conn.execute(
            "SELECT payload FROM sessions WHERE sid=?", (sid,)).fetchone()
    return json.loads(row[0]) if row else None


def list_sessions(limit: int = 20, path: str = DB_PATH, user: str = "") -> list[dict]:
    """列会话。传 user 就只列这个人的（多用户隔离），不传就是管理员视角。"""
    sql = """SELECT sid, user, created_at, updated_at, role, phase, finish_reason,
                    turns, avg_score FROM sessions"""
    args: tuple = ()
    if user:
        sql += " WHERE user=?"
        args = (user,)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args = args + (limit,)
    with _conn(path) as conn:
        rows = conn.execute(sql, args).fetchall()
    keys = ("sid", "user", "created_at", "updated_at", "role", "phase",
            "finish_reason", "turns", "avg_score")
    return [dict(zip(keys, r)) for r in rows]


def user_stats(path: str = DB_PATH) -> list[dict]:
    """后台用：每个用户多少场、平均分多少、最近一次是什么时候。"""
    with _conn(path) as conn:
        rows = conn.execute(
            """SELECT COALESCE(user,'') AS u, COUNT(*) AS n,
                      ROUND(AVG(avg_score), 2) AS avg_score,
                      MAX(updated_at) AS last_at, SUM(turns) AS turns
               FROM sessions GROUP BY u ORDER BY n DESC""").fetchall()
    return [{"user": r[0] or "local", "sessions": r[1], "avg_score": r[2],
             "last_at": r[3], "turns": r[4] or 0} for r in rows]
    keys = ("sid", "created_at", "updated_at", "role", "phase",
            "finish_reason", "turns", "avg_score")
    return [dict(zip(keys, r)) for r in rows]


def delete(sid: str, path: str = DB_PATH) -> bool:
    with _conn(path) as conn:
        cur = conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
    return cur.rowcount > 0


def restore(sid: str, llm, projects, path: str = DB_PATH):
    """按快照重建一个可继续的会话对象。

    注意：这里不重放模型调用，只把已有回合灌回去。
    接着答题时新的一轮会照常走模型——历史不重算，避免花两倍钱。
    """
    from schemas import Evaluation
    from session import InterviewSession

    data = load(sid, path)
    if not data:
        return None
    from resume import Project
    projs = projects or [Project(name=n, summary="", stack=[], highlights=[])
                         for n in (data.get("projects") or [])]
    sess = InterviewSession(
        llm=llm, projects=projs, role_key=data.get("role") or "agent_app",
        max_turns=int(data.get("max_turns") or 16), verbose=False, save=False,
        persona=data.get("persona") or "balanced",
    )
    from schemas import Turn
    for t in data.get("turns") or []:
        ev = None
        if t.get("eval"):
            try:
                ev = Evaluation.from_dict(t["eval"])
            except Exception:
                ev = None
        sess.turns.append(Turn(idx=t.get("idx", 0), phase=t.get("phase", ""),
                               question=t.get("question", ""), answer=t.get("answer", ""),
                               evaluation=ev, ladder_level=t.get("ladder_level", 0),
                               project=t.get("project", "")))
    sess.sm.phase = data.get("phase") or sess.sm.phase
    sess.cards = data.get("cards") or []
    sess.finish_reason = data.get("finish_reason") or ""
    sess._pending_question = data.get("pending_question") or ""
    sess._pending_target = data.get("pending_target") or ""
    return sess

def trend(user: str = "", limit: int = 30, path: str = DB_PATH) -> dict:
    """跨会话趋势：最近 N 场的均分走势 + 各维度走势。

    抄的是 Samcoderg78 的"跨会话记住薄弱项"，但我们不重跑模型：
    分数和维度都已经在 payload 里，聚合只是读库。
    为什么值得做：单场分数噪声很大（同一段答䅁换措辞能差 0.5），
    只有拉成时间线才看得出"有没有变强"。
    """
    sql = """SELECT sid, user, role, updated_at, avg_score, payload FROM sessions"""
    args: tuple = ()
    if user:
        sql += " WHERE user=?"
        args = (user,)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args = args + (limit,)
    with _conn(path) as conn:
        rows = conn.execute(sql, args).fetchall()

    points = []
    dims_series: dict[str, list[float]] = {}
    for sid, u, role, at, avg, payload in rows:
        try:
            data = json.loads(payload or "{}")
        except Exception:
            data = {}
        stats = ((data.get("meta") or {}).get("stats") or {})
        dims = stats.get("dims") or {}
        points.append({"sid": sid, "user": u or "local", "role": role or "",
                       "at": at, "avg": float(avg or 0),
                       "turns": len(data.get("turns") or []),
                       "dims": dims})
        for k, v in (dims or {}).items():
            if isinstance(v, (int, float)):
                dims_series.setdefault(k, []).append(float(v))

    points.reverse()          # 时间正序，画图直接用
    return {
        "user": user or "all",
        "count": len(points),
        "points": points,
        "avg_series": [p["avg"] for p in points],
        "dim_avg": {k: round(sum(v) / len(v), 2) for k, v in dims_series.items() if v},
    }