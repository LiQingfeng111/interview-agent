"""三层记忆测试：短期窗口、长期画像累加、画像注入。

不需要 PG 的部分用纯单元测试；
需要 PG 的部分加 skipif（和 retriever 测试同样的策略）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import memory


# ---------------------------------------------------------------- 短期
def test_push_recent_keeps_window():
    recent = []
    for i in range(10):
        recent = memory.push_recent(recent, f"问题{i}", f"回答{i}", "pass", keep=6)
    assert len(recent) == 6, f"应只留 6 轮，实际 {len(recent)}"
    assert recent[-1]["q"] == "问题9"
    assert recent[0]["q"] == "问题4"


def test_render_recent_limits_chars():
    recent = [{"q": "问" * 100, "a": "答" * 300, "verdict": "pass"} for _ in range(5)]
    text = memory.render_recent(recent, max_chars=800)
    assert len(text) <= 900, f"渲染超长：{len(text)}"
    assert "问：" in text


def test_render_recent_empty():
    assert memory.render_recent([]) == ""
    assert memory.render_recent(None) == ""


# ---------------------------------------------------------------- 长期（需 PG）
def _pg_ready() -> bool:
    try:
        from app.repos.pg import pg_health
        return bool(pg_health().get("ok"))
    except Exception:  # noqa: BLE001
        return False


pg_only = pytest.mark.skipif(not _pg_ready(), reason="需要 PostgreSQL")


@pg_only
def test_profile_accumulates():
    """连续两场面试，sessions_count 应累加。"""
    user = "memtest_user"
    _clean(user)

    p1 = memory.update_profile(user, {"session_id": ""})
    assert p1["sessions_count"] == 1

    p2 = memory.update_profile(user, {"session_id": ""})
    assert p2["sessions_count"] == 2

    loaded = memory.load_profile(user)
    assert loaded["sessions_count"] == 2

    _clean(user)


@pg_only
def test_profile_digest_only_after_history():
    """没有历史时不给提示（避免第一场就看到"第 2 场"）。"""
    user = "digest_user"
    _clean(user)
    assert memory.profile_digest(user) == ""

    memory.update_profile(user, {"session_id": ""})
    d = memory.profile_digest(user)
    assert "第 2 场" in d, f"应提示这是第 2 场，实际：{d[:80]}"
    _clean(user)


@pg_only
def test_profile_digest_mentions_weak_elements():
    user = "weak_user"
    _clean(user)

    # 手工塞一个弱要素
    memory._save_profile(user, {
        "weak_elements": {"失败方案": 3, "边界": 1},
        "strong_topics": ["RAG 检索增强生成"],
        "weak_topics": ["缓存一致性"],
        "sessions_count": 2,
    })
    d = memory.profile_digest(user)
    assert "失败方案" in d
    assert "缓存一致性" in d
    assert "RAG" in d
    _clean(user)


@pg_only
def test_load_profile_missing_returns_empty():
    p = memory.load_profile("never_existed_user_xyz")
    assert p["sessions_count"] == 0
    assert p["weak_elements"] == {}


def _clean(user: str) -> None:
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM candidate_profile WHERE username = %s", (user,))
            cur.close()
    except Exception:  # noqa: BLE001
        pass
