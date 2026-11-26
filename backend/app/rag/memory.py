"""三层记忆：短期 / 工作 / 长期。

## 三层各管什么

| 层 | 存什么 | 生命周期 | 存储 |
|---|---|---|---|
| **短期** | 最近 N 轮原始问答 | 单场面试 | 图状态 `recent_qa` |
| **工作** | 事实锚点、矛盾、要素覆盖 | 单场面试 | 旧 `memory.py`（已有） |
| **长期** | 跨会话：这人反复失败的要素/话题 | 永久 | PG `candidate_profile` + 向量 |

## 为什么要分三层（而不是一个对话历史）

三个需求互斥：
- **短期**要"完整"（模型需要上下文），所以只留最近几轮（token 有限）
- **工作**要"精确"（判定要用"71%→82%"这种锚点），所以抽取成结构化事实
- **长期**要"提炼"（跨会话不能塞全部历史），所以只存结论

## 长期记忆的读写时机

- **写**：面试结束时批量写（不打断主链路）
- **读**：开场时投影成"上次的薄弱点"注入 plan 节点

为什么不一结束就写：报告生成已经要 1-2 秒，再加一次 DB 写会让用户等更久。
放后台线程做。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

log = logging.getLogger("interview.memory")


# ============================================================================
# 长期记忆：candidate_profile
# ============================================================================

def load_profile(user: str) -> dict[str, Any]:
    """读某个用户的长期画像。没有则返回空结构。"""
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """SELECT weak_elements, strong_topics, weak_topics,
                          sessions_count, updated_at
                   FROM candidate_profile WHERE username = %s""",
                (user,),
            )
            row = cur.fetchone()
            cur.close()
        if not row:
            return {"weak_elements": {}, "strong_topics": [], "weak_topics": [],
                    "sessions_count": 0}
        return {
            "weak_elements": _as_dict(row[0]),
            "strong_topics": _as_list(row[1]),
            "weak_topics": _as_list(row[2]),
            "sessions_count": int(row[3] or 0),
            "updated_at": row[4].isoformat() if row[4] else "",
        }
    except Exception as e:  # noqa: BLE001
        log.warning("读长期画像失败：%s", e)
        return {"weak_elements": {}, "strong_topics": [], "weak_topics": [],
                "sessions_count": 0}


def update_profile(user: str, state: dict[str, Any]) -> dict[str, Any]:
    """面试结束时更新长期画像（累加，不覆盖）。

    累加而非覆盖的理由：单场面试的结论噪声很大
    （可能这一场恰好问到他不熟的），累计 3-5 场才稳定。
    """
    if not user:
        return {}

    old = load_profile(user)
    weak_elements = dict(old.get("weak_elements") or {})
    strong = set(old.get("strong_topics") or [])
    weak = set(old.get("weak_topics") or [])

    # 从本场的 turns 里统计（图状态里有 recent_qa，但完整数据在 PG）
    sid = state.get("session_id", "")
    turns = _load_turns(sid)

    for t in turns:
        element = str(t.get("element") or "").strip()
        verdict = str(t.get("verdict") or "")
        topic = str(t.get("phase") or "")

        # 要素维度：fail 记为薄弱
        if element and verdict == "fail":
            weak_elements[element] = int(weak_elements.get(element, 0)) + 1
        elif element and verdict == "pass" and int(weak_elements.get(element, 0)) > 0:
            # 这次答好了，弱项计数减一（会慢慢"康复"）
            weak_elements[element] = max(0, int(weak_elements[element]) - 1)

    # 话题维度：按本场平均分判断强/弱
    avg = _avg_score(turns)
    if turns and avg >= 3.2:
        for t in turns:
            tp = str(t.get("phase") or "")
            if tp:
                strong.add(tp)
    elif turns and avg <= 2.0:
        for t in turns:
            tp = str(t.get("phase") or "")
            if tp and tp not in strong:
                weak.add(tp)

    result = {
        "weak_elements": {k: v for k, v in weak_elements.items() if v > 0},
        "strong_topics": sorted(strong)[:10],
        "weak_topics": sorted(weak - strong)[:10],
        "sessions_count": int(old.get("sessions_count", 0)) + 1,
    }

    _save_profile(user, result)
    return result


def update_profile_async(user: str, state: dict[str, Any]) -> None:
    """后台线程更新（不阻塞请求）。"""
    def _run():
        try:
            update_profile(user, state)
        except Exception as e:  # noqa: BLE001
            log.warning("长期记忆更新失败：%s", e)

    threading.Thread(target=_run, daemon=True, name=f"profile-{user}").start()


def _save_profile(user: str, data: dict[str, Any]) -> None:
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """INSERT INTO candidate_profile
                     (username, weak_elements, strong_topics, weak_topics, sessions_count, updated_at)
                   VALUES (%s, %s::jsonb, %s::jsonb, %s::jsonb, %s, now())
                   ON CONFLICT (username) DO UPDATE SET
                     weak_elements = EXCLUDED.weak_elements,
                     strong_topics = EXCLUDED.strong_topics,
                     weak_topics = EXCLUDED.weak_topics,
                     sessions_count = EXCLUDED.sessions_count,
                     updated_at = now()""",
                (user,
                 json.dumps(data["weak_elements"], ensure_ascii=False),
                 json.dumps(data["strong_topics"], ensure_ascii=False),
                 json.dumps(data["weak_topics"], ensure_ascii=False),
                 data["sessions_count"]),
            )
            cur.close()
    except Exception as e:  # noqa: BLE001
        log.warning("写长期画像失败：%s", e)


# ============================================================================
# 工作记忆：跨会话的历史问答检索
# ============================================================================

def recall_history(user: str, query: str, k: int = 3) -> str:
    """从历史问答里召回"类似问题他以前怎么答的"。

    用途：如果这人上次在"失败方案"上答崩了，这次可以更早追问这一点。
    """
    if not query.strip():
        return ""
    try:
        from app.rag import retriever
        hits = retriever.search_history(query, owner=user, k=k)
        if not hits:
            return ""
        parts = []
        for h in hits:
            verdict = h.meta.get("verdict", "")
            parts.append(f"【历史问答 · {verdict}】\n{h.text[:300]}")
        return "\n\n".join(parts)
    except Exception as e:  # noqa: BLE001
        log.warning("历史召回失败：%s", e)
        return ""


def profile_digest(user: str) -> str:
    """把长期画像压成一段提示词，注入 plan 节点。

    为什么要这段：没有它，每场面试都从零开始，
    会重复问这个人已经答得很好的点，或者漏掉他反复答崩的点。
    """
    p = load_profile(user)
    n = p.get("sessions_count", 0)
    if n <= 0:
        return ""

    lines = [f"这是该候选人的第 {n + 1} 场面试。历史数据："]
    we = p.get("weak_elements") or {}
    if we:
        top = sorted(we.items(), key=lambda x: -x[1])[:3]
        lines.append("反复没讲清的要素：" + "、".join(f"{k}（{v} 次）" for k, v in top))
    st = p.get("strong_topics") or []
    if st:
        lines.append("已经表现不错的话题（不用重复深挖）：" + "、".join(st[:3]))
    wt = p.get("weak_topics") or []
    if wt:
        lines.append("明显薄弱的话题（建议多问）：" + "、".join(wt[:3]))
    lines.append("请据此调整追问重点，但不要直接说出这些统计数字。")
    return "\n".join(lines)


# ============================================================================
# 短期记忆：最近 N 轮
# ============================================================================

def push_recent(recent: list[dict], question: str, answer: str,
                verdict: str = "", keep: int = 6) -> list[dict]:
    """把一轮追加到短期记忆（只留最近 keep 轮）。"""
    out = list(recent or [])
    out.append({"q": question, "a": answer, "verdict": verdict})
    return out[-keep:]


def render_recent(recent: list[dict], max_chars: int = 800) -> str:
    """把短期记忆渲染成提示词片段。"""
    if not recent:
        return ""
    lines = []
    total = 0
    for r in recent[-3:]:
        block = f"问：{str(r.get('q', ''))[:80]}\n答：{str(r.get('a', ''))[:150]}"
        if r.get("verdict"):
            block += f"　[{r['verdict']}]"
        if total + len(block) > max_chars:
            break
        lines.append(block)
        total += len(block)
    return "\n".join(lines)


# ============================================================================
# 内部
# ============================================================================

def _load_turns(sid: str) -> list[dict]:
    """读某场的全部问答（含五维分数，报告页雷达图要用）。"""
    if not sid:
        return []
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            # 用 SELECT * 的显式列名而不是 *：维度列是后加的，
            # 老库可能没有 → 用 information_schema 判断一下更稳
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'turns' AND table_schema = 'public'
            """)
            have = {r[0] for r in cur.fetchall()}
            dim_cols = [k for k in
                        ("accuracy", "depth", "tradeoff", "boundary", "clarity")
                        if k in have]
            dim_sql = (", " + ", ".join(dim_cols)) if dim_cols else ""

            cur.execute(
                f"""SELECT question, answer, verdict, score, phase, element{dim_sql}
                    FROM turns WHERE session_id = %s ORDER BY seq""",
                (sid,),
            )
            rows = cur.fetchall()
            cur.close()

        out = []
        for r in rows:
            item = {
                "question": r[0], "answer": r[1], "verdict": r[2],
                "score": float(r[3] or 0), "phase": r[4], "element": r[5],
            }
            if dim_cols:
                item["dims"] = {k: float(r[6 + i]) for i, k in enumerate(dim_cols)
                                if r[6 + i] is not None}
            out.append(item)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("读 turns 失败：%s", e)
        return []


def _avg_score(turns: list[dict]) -> float:
    scores = [t["score"] for t in turns if t.get("score")]
    return sum(scores) / len(scores) if scores else 0.0


def _as_dict(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v or "{}")
    except Exception:  # noqa: BLE001
        return {}


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v or "[]")
        return out if isinstance(out, list) else []
    except Exception:  # noqa: BLE001
        return []
