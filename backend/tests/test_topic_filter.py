"""topic 过滤测试。

## 要锁住什么

这是本次检索实验最重要的发现：

    topic='RAG' 的题，问的是 "chunk 大小怎么定"、"RRF 的 k 取多少"
    → question 文本里没有 "RAG" 这个词
    → 用 "RAG" 做向量检索匹配 question，命中率只有 20-25%

实测对比（37 个话题，看 top-10 里同话题占比）：

    A. 纯向量 k=10（旧行为）        47%
    B. 纯向量 k=30（只放宽召回）     33%  ← 更差
    C. topic 过滤 + 向量 k=10       100%

**关键教训：只放宽召回不加过滤，只会引入噪声。**

## 测试分层（重要）

CI 环境**没有 Ollama**（`OLLAMA_BASE_URL=http://127.0.0.1:1`），
所以不能写成"调 search_bank 然后断言结果"——那在 CI 里必然失败。

分成两层：
  · **纯逻辑层**（不碰 DB / 不碰向量）：`_looks_like_topic` 的匹配规则
  · **集成层**（需要 PG + 向量）：真的检索，用 `_ready()` 跳过

这个分层是踩过的坑：第一版全写成集成测试，CI 直接红了三个。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("IA_JWT_SECRET", "test-secret-32-bytes-long-enough")
os.environ.setdefault("IA_REDIS", "0")


def _ready() -> bool:
    """PG + 已导入向量 + 向量服务可用。

    三个条件都要满足，否则 search_bank 会返回空（不是抛异常），
    导致断言失败而不是 skip。
    """
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM bank_questions WHERE embedding IS NOT NULL")
            n = cur.fetchone()[0]
            cur.close()
        if int(n) < 100:
            return False
    except Exception:  # noqa: BLE001
        return False

    # 向量服务也得在（CI 里指向 127.0.0.1:1，必定不通）
    try:
        from app.rag.embedder import get_embedder
        get_embedder().embed(["探测"])
        return True
    except Exception:  # noqa: BLE001
        return False


integration = pytest.mark.skipif(
    not _ready(),
    reason="需要 PG + 题库向量 + 向量服务（bash ops.sh up && bash ops.sh bank）",
)


# ============================================================================
# 纯逻辑层：_looks_like_topic 的匹配规则（不需要任何外部依赖）
# ============================================================================

class TestLooksLikeTopic:
    """`_looks_like_topic()` 的判定规则。

    这个函数决定"要不要把 query 当话题做精确过滤"。
    判错会导致两种相反的事故：
      · 该过滤时没过滤 → 命中率低（本次要修的问题）
      · 不该过滤时过滤了 → 过滤后为空，选不出题
    """

    @staticmethod
    def _fn():
        from app.rag.bank_select import _looks_like_topic
        return _looks_like_topic

    def test_exact_match_from_known_set(self, monkeypatch):
        """恰好等于已知话题 → True。"""
        import app.rag.bank_select as bs

        monkeypatch.setattr(bs, "_TOPIC_CACHE", {"RAG", "Agent", "Transformer"})
        f = self._fn()

        assert f("RAG") is True
        assert f("Agent") is True
        assert f("Transformer") is True

    def test_rejects_project_name(self, monkeypatch):
        """项目名不该被当成话题。

        生产里 query 可能是项目名（"课程问答助手"）。
        误判成话题会导致过滤后一条不剩。
        """
        import app.rag.bank_select as bs

        monkeypatch.setattr(bs, "_TOPIC_CACHE", {"RAG", "Agent"})
        f = self._fn()

        assert f("课程问答助手") is False
        assert f("我的电商系统") is False

    def test_rejects_partial_match(self, monkeypatch):
        """部分匹配不算 —— 必须精确。

        "RAG 检索优化怎么做" 是具体问题，不是话题名。
        如果模糊匹配，会把问题误当话题。
        """
        import app.rag.bank_select as bs

        monkeypatch.setattr(bs, "_TOPIC_CACHE", {"RAG"})
        f = self._fn()

        assert f("RAG 检索优化怎么做") is False
        assert f("RAG 检索") is False
        assert f("关于 RAG") is False

    def test_rejects_empty(self, monkeypatch):
        import app.rag.bank_select as bs

        monkeypatch.setattr(bs, "_TOPIC_CACHE", {"RAG"})
        f = self._fn()

        assert f("") is False
        assert f("   ") is False

    def test_strips_whitespace(self, monkeypatch):
        """两边空格不影响判定。"""
        import app.rag.bank_select as bs

        monkeypatch.setattr(bs, "_TOPIC_CACHE", {"RAG"})
        f = self._fn()

        assert f("  RAG  ") is True

    def test_case_sensitive(self, monkeypatch):
        """大小写敏感 —— "rag" 不等于 "RAG"。

        题库里的话题名是固定的，大小写不同说明不是同一个。
        这里刻意不做模糊匹配，避免误判。
        """
        import app.rag.bank_select as bs

        monkeypatch.setattr(bs, "_TOPIC_CACHE", {"RAG"})
        f = self._fn()

        assert f("rag") is False

    def test_empty_topic_set(self, monkeypatch):
        """话题表为空（读库失败时）→ 一律不过滤。"""
        import app.rag.bank_select as bs

        monkeypatch.setattr(bs, "_TOPIC_CACHE", set())
        f = self._fn()

        assert f("RAG") is False


# ============================================================================
# SQL 构造层：topic 真的进了 WHERE（用假 cursor 验证，不需要真 DB）
# ============================================================================

class TestSqlConstruction:
    """验证 `search_bank` 把 topic 拼进了 SQL。

    为什么不连真库：CI 没有 Ollama，向量化就会失败，
    根本走不到 SQL 那一步。所以这里 mock 掉 embedder 和 DB，
    只验证 SQL 字符串和参数。
    """

    @staticmethod
    def _run_capture(**kwargs):
        """跑 search_bank，捕获它执行的 SQL 和参数。"""
        import app.rag.retriever as rt

        captured = {}

        class FakeCursor:
            def execute(self, sql, params=None):
                captured["sql"] = sql
                captured["params"] = params

            def fetchall(self):
                return []

            def close(self):
                pass

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeEmbedder:
            def embed(self, texts):
                return [[0.1] * 1024 for _ in texts]

        orig_emb = rt.get_embedder
        orig_pg = None
        try:
            rt.get_embedder = lambda: FakeEmbedder()

            import app.repos.pg as pgmod
            orig_pg = pgmod.get_conn
            pgmod.get_conn = lambda: FakeConn()

            rt.search_bank(**kwargs)
        finally:
            rt.get_embedder = orig_emb
            if orig_pg is not None:
                import app.repos.pg as pgmod
                pgmod.get_conn = orig_pg

        return captured

    def test_topic_appears_in_where_clause(self):
        """传了 topic，SQL 里必须有 topic = %s。"""
        cap = self._run_capture(query="RAG", k=5, topic="RAG")

        assert "sql" in cap, "没有执行 SQL"
        assert "topic = %s" in cap["sql"], f"SQL 里没有 topic 过滤：{cap['sql']}"
        assert "RAG" in cap["params"], f"参数里没有话题值：{cap['params']}"

    def test_no_topic_means_no_filter(self):
        """不传 topic，SQL 里不该有 topic 条件。"""
        cap = self._run_capture(query="RAG", k=5)

        assert "topic = %s" not in cap["sql"], "不该有 topic 过滤"

    def test_topic_and_role_both_in_where(self):
        """topic + role 同时出现。"""
        cap = self._run_capture(query="RAG", k=5, topic="RAG", role="ai_algo")

        assert "topic = %s" in cap["sql"]
        assert "role = %s" in cap["sql"]
        assert "RAG" in cap["params"]
        assert "ai_algo" in cap["params"]

    def test_empty_topic_no_filter(self):
        """topic 传空串 → 不加条件。"""
        cap = self._run_capture(query="RAG", k=5, topic="")

        assert "topic = %s" not in cap["sql"]


# ============================================================================
# 集成层：真的检索（需要 PG + 向量服务）
# ============================================================================

@integration
def test_topic_filter_returns_only_that_topic():
    """指定 topic 后，结果全部来自该话题。"""
    from app.rag import retriever

    hits = retriever.search_bank("RAG", k=10, role="", use="select", topic="RAG")

    assert hits, "应该能召回到 RAG 的题"
    wrong = [h for h in hits if (h.project or "") != "RAG"]
    assert not wrong, f"混入了其他话题：{[h.project for h in wrong]}"


@integration
def test_topic_filter_improves_purity():
    """topic 过滤后纯度必须显著高于不过滤。

    这是本次修复的核心价值：47% → 100%。
    """
    from app.rag import retriever

    query = "RAG"

    plain = retriever.search_bank(query, k=10, role="", use="select")
    plain_purity = (
        sum(1 for h in plain if (h.project or "") == "RAG") / len(plain)
        if plain else 0
    )

    filtered = retriever.search_bank(query, k=10, role="", use="select", topic="RAG")
    filt_purity = (
        sum(1 for h in filtered if (h.project or "") == "RAG") / len(filtered)
        if filtered else 0
    )

    assert filt_purity == 1.0, f"过滤后纯度应为 100%，实际 {filt_purity:.0%}"
    assert filt_purity > plain_purity, (
        f"过滤后（{filt_purity:.0%}）应优于不过滤（{plain_purity:.0%}）"
    )


@integration
def test_nonexistent_topic_returns_empty():
    """不存在的话题 → 空结果（让调用方知道要退化）。"""
    from app.rag import retriever

    hits = retriever.search_bank("查询", k=5, role="", use="select",
                                 topic="__不存在的__话题__")
    assert hits == []


@integration
def test_pick_with_topic_query_returns_same_topic():
    """当 query 是话题名时，选出的题应该来自该话题。"""
    from app.rag import bank_select

    picked = bank_select.pick(
        query="数据库索引",
        role="",
        asked_qids=[],
        turn=3,
        topic_counts={},
        total_turns=16,
    )
    if picked is not None:
        assert picked.topic == "数据库索引", (
            f"应选同话题的题，实际选了 {picked.topic!r}"
        )
