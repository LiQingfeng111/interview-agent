"""启动自检：把"模块缺失"这类错误在启动时炸出来，而不是运行时静默降级。

## 为什么需要这个

踩过的坑：`app.repos.documents` 模块不存在，但 `service.start()` 里的
`try/except` 把它吞掉了，只打一行 warning。结果：

- 上传接口 200 ✅
- 状态接口 ready ✅
- 点"开始面试" → 报「简历里没识别到项目」❌

用户完全无法从错误信息推断出"是代码里少了一个文件"。
我作为开发者也是跑真实模型测试时才撞上。

**如果启动时检查一遍所有关键模块，这个问题会在服务启动阶段就暴露。**

## 检查什么

1. 旧核心模块（state_machine / probe_tree / invariants / ledger / evaluator / interviewer）
2. 新后端模块（repos / rag / graph / api）
3. 数据库可写
4. pgvector 扩展可用

失败时：**抛异常阻止启动**（而不是打印 warning 继续跑）。
理由：缺模块的服务跑起来只会产生错得离谱的响应，比不启动更糟。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("interview.selfcheck")

#: 旧核心的必需模块（缺一个都无法出题）
LEGACY_MODULES = [
    "config", "state_machine", "probe_tree", "invariants", "ledger",
    "evaluator", "interviewer", "planner", "resume", "bank", "memory",
]

#: 新后端的必需模块（缺一个都会有功能静默失效）
BACKEND_MODULES = [
    "app.core.errors",
    "app.core.bridge",
    "app.core.runtime",
    "app.core.recover",
    "app.core.registry",
    "app.core.service",
    "app.repos.pg",
    "app.repos.cache",
    "app.repos.documents",       # ← 这个曾缺失导致"上传成功但开场失败"
    "app.rag.embedder",
    "app.rag.splitter",
    "app.rag.retriever",
    "app.rag.bank_select",
    "app.rag.quota",
    "app.rag.boost",
    "app.rag.memory",
    "app.graph.interview_graph",
    "app.graph.checkpointer",
    "app.api.auth",
    "app.api.sessions",
    "app.api.documents",
    "app.api.bank",
    "app.api.diagnostics",
    "app.api.speech",
    "app.api.stream",
]


def check_imports() -> list[str]:
    """逐个 import，返回失败列表。

    这是自检里**最有价值**的一项：它抓过两个真实 bug——
    ① `app.repos.documents` 模块缺失（表现为"上传成功但开场失败"）
    ② 构建镜像时 `*.py` 没复制进容器（表现为"找不到旧核心模块：/app"）

    两个都是"运行时静默降级"，只有启动时报错才能第一时间发现。
    """
    missing: list[str] = []

    # 旧核心要先接入 sys.path
    try:
        from app.core import bridge
        bridge.ensure_legacy_importable()
    except Exception as e:  # noqa: BLE001
        return [f"legacy path: {type(e).__name__}: {e}"]

    for mod in LEGACY_MODULES + BACKEND_MODULES:
        try:
            __import__(mod)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{mod} → {type(e).__name__}: {e}")

    return missing


def check_bank() -> list[str]:
    """题库是否已导入。

    ## 为什么要在启动时自动导入

    Docker 部署时是全新数据库，`bank_questions` 表是空的。
    不自动导入的话，技术问答阶段题库选题永远返回空 →
    面试只能靠模型自由出题，质量明显下降。

    导入本身幂等（`ON CONFLICT DO UPDATE`），且向量化断点续跑
    （只处理 `embedding IS NULL` 的题），所以重复执行安全。

    注意：只在"表为空"时才导入，避免每次启动都扫一遍题库文件。
    """
    problems: list[str] = []
    try:
        from app.repos.pg import get_conn
    except Exception as e:  # noqa: BLE001
        return [f"无法导入 pg: {e}"]

    try:
        with get_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*), COUNT(embedding) FROM bank_questions")
            total, with_vec = cur.fetchone()
            cur.close()
    except Exception as e:  # noqa: BLE001
        return [f"查题库失败：{e}"]

    total = int(total or 0)
    with_vec = int(with_vec or 0)

    # 表非空且向量齐全 → 什么都不做
    if total > 0 and with_vec >= total:
        log.info("题库就绪：%d 题，全部已向量化", total)
        return problems

    # 缺向量：断点续跑补算
    #
    # 踩过的坑：第一版只在 `total == 0` 时导入，结果 Docker 部署时
    # 题库行数有了（722）但 `embedding` 全是 NULL（因为之前降级成
    # hash 向量时维度不匹配写失败），而自检看到"非空"就跳过了。
    # 表现：面试跑到技术问答阶段，题库检索永远返回空。
    #
    # 现在改成："行数不全 或 向量不全"都要处理，`ingest_bank` 本身
    # 就是断点续跑（只处理 `embedding IS NULL`），所以重复执行安全。
    log.warning("题库需要补齐（现有 %d 行，已向量化 %d）…", total, with_vec)
    try:
        from pathlib import Path

        from app.core.bridge import _find_legacy_root
        from app.rag.ingest import ingest_bank

        path = Path(_find_legacy_root()) / "data" / "question_bank.jsonl"
        if not path.exists():
            problems.append(f"题库文件不存在：{path}")
            return problems

        r = ingest_bank(path)
        log.info("题库处理完成：总 %d 题，本次向量化 %d 题，%.1f 秒",
                 r.get("total", 0), r.get("embedded", 0), r.get("seconds", 0))
    except Exception as e:  # noqa: BLE001
        # 题库失败不该阻止启动——面试还能跑，只是技术问答质量下降
        log.exception("题库处理失败（技术问答会退化）")
        problems.append(f"题库处理失败：{type(e).__name__}: {e}")

    return problems
    """逐个 import，返回失败列表。"""
    missing: list[str] = []

    # 旧核心要先接入 sys.path
    try:
        from app.core import bridge
        bridge.ensure_legacy_importable()
    except Exception as e:  # noqa: BLE001
        return [f"legacy path: {type(e).__name__}: {e}"]

    for mod in LEGACY_MODULES + BACKEND_MODULES:
        try:
            __import__(mod)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{mod} → {type(e).__name__}: {e}")

    return missing


def check_database() -> list[str]:
    """数据库连通 + pgvector + 必需表。

    ## 为什么缺失时"自动建"而不是"报错拒绝启动"

    第一版这里只报错，结果 Docker 部署时要人工多跑一步 `bash ops.sh schema`，
    忘了就容器反复重启（实测踩到：日志里一堆 "pgvector 扩展未安装"）。

    现在改成：缺什么就建什么（`init_schema` 本身是幂等的），
    只有"连不上数据库"这种硬故障才拒绝启动。
    这也是容器化部署的基本要求——容器应该能自愈，不能依赖外部编排顺序。
    """
    problems: list[str] = []
    try:
        from app.repos.pg import get_conn
    except Exception as e:  # noqa: BLE001
        return [f"无法导入 pg: {e}"]

    # ① 连通性（这个必须硬失败）
    try:
        with get_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
    except Exception as e:  # noqa: BLE001
        return [f"数据库连接失败：{type(e).__name__}: {e}"]

    # ② 检查扩展与表
    need_tables = {"users", "documents", "doc_chunks", "bank_questions",
                   "sessions", "turns", "reports", "candidate_profile"}
    have_tables: set[str] = set()
    has_vector = False

    try:
        with get_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            has_vector = cur.fetchone() is not None

            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_schema = 'public'
            """)
            have_tables = {r[0] for r in cur.fetchall()}
            cur.close()
    except Exception as e:  # noqa: BLE001
        problems.append(f"查库结构失败：{e}")
        return problems

    lack = need_tables - have_tables

    # ③ 缺什么建什么（幂等）
    if not has_vector or lack:
        log.warning("数据库结构不完整（vector=%s，缺表 %s），自动初始化…",
                    has_vector, sorted(lack) if lack else "无")
        try:
            from app.repos.pg import init_schema
            r = init_schema()
            log.info("自动初始化完成：%d 条语句", r.get("statements", 0))
        except Exception as e:  # noqa: BLE001
            problems.append(
                f"自动初始化失败：{type(e).__name__}: {e}\n"
                f"    请手动执行：bash ops.sh schema"
            )

    return problems


def run(strict: bool = True) -> dict[str, Any]:
    """跑全部检查。

    Args:
        strict: True 时有任何问题就抛异常（生产/启动用）；
                False 只返回结果（测试用）。
    """
    import_problems = check_imports()
    db_problems = check_database()
    bank_problems = check_bank()

    ok = not import_problems and not db_problems

    result = {
        "ok": ok,
        "import_problems": import_problems,
        "db_problems": db_problems,
        "bank_problems": bank_problems,
        "checked_modules": len(LEGACY_MODULES) + len(BACKEND_MODULES),
    }

    if ok:
        log.info("启动自检通过（%d 个模块 + 数据库）", result["checked_modules"])
        return result

    log.error("启动自检失败：")
    for p in import_problems:
        log.error("  [模块] %s", p)
    for p in db_problems:
        log.error("  [数据库] %s", p)
    for p in bank_problems:
        log.error("  [题库] %s", p)

    if strict:
        raise RuntimeError(
            "启动自检失败，拒绝启动：\n"
            + "\n".join(f"  模块: {p}" for p in import_problems)
            + ("\n" if import_problems and db_problems else "")
            + "\n".join(f"  数据库: {p}" for p in db_problems)
        )
    return result
