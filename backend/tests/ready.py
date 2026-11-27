"""测试就绪判定：统一"能不能跑语义检索相关的测试"。

## 为什么需要这个共用模块（CI 红过）

CI 环境**故意**把 `OLLAMA_BASE_URL` 指向 `http://127.0.0.1:1`（不可达）——
这样不需要在 CI 里跑 Ollama。

但原来的 `_ready()` 只检查了「PG 有向量」，没检查「向量服务能不能用」。
结果：

    CI: _ready() = True  → 测试继续跑
        → search_bank 内部向量化失败
        → 返回空列表（不抛异常）
        → 断言 hits 为空 → **FAILED**

真实数据：本地有 Ollama 时全绿，CI 里 8 条红。
我是在推送后看 CI 日志才发现的 —— 本地永远复现不了。

## 为什么三个条件都要检查

1. **PG 连得上** —— 否则查询直接异常
2. **题库有向量** —— 否则查不到东西（`embedding IS NOT NULL` 过滤后为空）
3. **向量服务可用** —— 否则 `embed()` 失败，`search_bank` 返回空

前两条原来就有，缺的是第三条。

## 用法

    from tests.ready import semantic_ready

    pytestmark = pytest.mark.skipif(
        not semantic_ready(), reason="需要 PG + 题库向量 + 向量服务")
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("tests.ready")


def pg_ready() -> bool:
    """PG 连得上且有题库向量。"""
    try:
        from app.repos.pg import get_conn, pg_health
        if not pg_health().get("ok"):
            return False
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM bank_questions WHERE embedding IS NOT NULL")
            n = cur.fetchone()[0]
            cur.close()
        return int(n) >= 100
    except Exception:  # noqa: BLE001
        return False


def embedding_ready() -> bool:
    """向量服务真的能返回向量。

    ## 为什么必须真调一次

    只看 `OLLAMA_BASE_URL` 环境变量不够 —— 配了也可能服务没起。
    只 try import 也不够 —— 导入成功不代表服务活着。

    必须真发一次 embed 请求。成本约 160ms，一次性，可接受。
    """
    # CI 里显式关掉（省 1 秒，且 CI 本来就没有）
    if os.environ.get("IA_SKIP_EMBED_CHECK") in ("1", "true", "yes"):
        return False

    try:
        from app.rag.embedder import get_embedder
        emb = get_embedder()
        vecs = emb.embed(["就绪探测"])
        return bool(vecs) and len(vecs[0]) > 0
    except Exception as e:  # noqa: BLE001
        log.info("向量服务不可用（相关测试将跳过）：%s", e)
        return False


def semantic_ready() -> bool:
    """语义检索测试的全部前置条件。

    顺序有讲究：先查 PG（快，本地失败），再查向量服务（慢，要发请求）。
    这样 PG 没起时能快速跳过。
    """
    if not pg_ready():
        return False
    return embedding_ready()
