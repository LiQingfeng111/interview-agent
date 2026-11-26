"""核心模块桥：把旧 `interview_agent/` 的规则层接入新 backend。

## 为什么要桥接而不是重写

旧核心（12,113 行）里有这个项目真正的价值：
`state_machine`（阶段流转）、`probe_tree`（五要素追踪）、
`invariants`（8 条审计规则）、`ledger`（只追加账本）、
`evaluator` 的阈值逻辑、722 题题库。

**这些一行都不该重写。** 重写意味着 356 条测试作废，
而且判定语义会漂——那是这个项目唯一不可让渡的东西。

所以：新 backend 只做"接入层"（HTTP、鉴权、存储、检索），
规则层通过 `sys.path` 直接 import 旧的模块。

## 为什么不用 pip 安装旧模块

旧模块不是包（没有 `setup.py`/`pyproject.toml`），是一堆平铺的 `.py`。
改成包要动所有 `import`，风险大于收益。
`sys.path.insert` 一行搞定，且不影响旧代码继续独立运行。

## 依赖方向（重要）

```
backend/app/  ──imports──▶  interview_agent/*.py   （单向）
interview_agent/*.py  ──✗──▶  backend/           （永不反向）
```

单向依赖保证：删掉 backend，旧功能照常跑；删掉旧代码，backend 立刻报错（编译期就发现）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from app.settings import settings

log = logging.getLogger("interview.bridge")

_LEGACY_READY = False


def _find_legacy_root() -> Path:
    """定位旧核心目录。

    优先级：
    1. IA_LEGACY_ROOT 环境变量（部署时可指向别处）
    2. backend 的上一级（仓库结构：interview_agent/backend/ → interview_agent/）
    """
    if settings.legacy_root:
        return Path(settings.legacy_root).resolve()
    # backend/app/core/bridge.py → parents[3] = interview_agent/
    return Path(__file__).resolve().parents[3]


def ensure_legacy_importable() -> Path:
    """把旧核心目录加进 sys.path，并验证关键模块可导入。

    只做一次（幂等）。失败时抛异常而不是静默——规则层缺失时
    整个面试会问出空问题，比启动失败更糟。
    """
    global _LEGACY_READY
    root = _find_legacy_root()
    if _LEGACY_READY:
        return root

    if not (root / "state_machine.py").exists():
        raise RuntimeError(
            f"找不到旧核心模块：{root}\n"
            "期望目录里存在 state_machine.py / probe_tree.py / invariants.py。\n"
            "可用 IA_LEGACY_ROOT 环境变量指定位置。"
        )

    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    # 验证关键模块都能导入（缺一个就早点失败）
    missing = []
    for mod in ("state_machine", "probe_tree", "invariants", "ledger",
                "evaluator", "interviewer", "config", "bank"):
        try:
            __import__(mod)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{mod}: {type(e).__name__} {e}")
    if missing:
        raise RuntimeError("旧核心模块导入失败：\n  " + "\n  ".join(missing))

    _LEGACY_READY = True
    log.info("旧核心已接入：%s", root)
    return root


# ---------------------------------------------------------------- 便捷访问
def legacy(name: str) -> Any:
    """按名字取旧模块（自动确保 sys.path 就绪）。"""
    ensure_legacy_importable()
    return __import__(name)


def legacy_attr(module: str, attr: str) -> Any:
    return getattr(legacy(module), attr)


def phase_config() -> Any:
    """阶段配置（旧 config.PHASE_CONFIG）。"""
    return legacy_attr("config", "PHASE_CONFIG")


def roles() -> list[str]:
    """可用岗位方向。旧 config 里是 ROLE_KNOWLEDGE / ROLE_KB 之类，做兼容处理。"""
    cfg = legacy("config")
    for name in ("ROLE_KB", "ROLE_KNOWLEDGE", "ROLES"):
        obj = getattr(cfg, name, None)
        if isinstance(obj, dict):
            return sorted(obj.keys())
        if isinstance(obj, (list, tuple)):
            return sorted(str(x) for x in obj)
    return ["ai_algo", "backend", "frontend", "agent_app", "general"]


def personas() -> list[str]:
    """面试官风格。"""
    cfg = legacy("config")
    for name in ("PERSONAS", "PERSONA_CONFIG"):
        obj = getattr(cfg, name, None)
        if isinstance(obj, dict):
            return sorted(obj.keys())
    return ["balanced", "strict", "warm", "academic", "engineering", "efficient"]


def bank_stats() -> dict[str, Any]:
    """题库统计（复用旧 bank 模块，避免两处读 JSONL）。"""
    bank_mod = legacy("bank")
    path = _find_legacy_root() / "data" / "question_bank.jsonl"
    items = []
    if path.exists():
        import json
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                items.append(json.loads(line))
    if hasattr(bank_mod, "bank_stats"):
        return bank_mod.bank_stats(items)
    return {"total": len(items)}


def thresholds() -> dict[str, float]:
    """评分阈值（旧 config.VERDICT_THRESHOLDS），只读引用不复制。"""
    cfg = legacy("config")
    return dict(getattr(cfg, "VERDICT_THRESHOLDS", {}))
