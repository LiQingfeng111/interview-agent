"""给旧 LLM 补上 usage 回传：monkey-patch LLMLogger。

## 为什么用 patch 而不是改 llm.py

`llm.py` 的 `chat()` 返回值是纯文本，usage 只写进 logger。
要把 usage 传到调用方，有两条路：

1. **改 llm.py**：让 `chat()` 返回 `(text, usage)` → 影响 30+ 调用点 +
   379 条测试 + 4 个 eval 脚本。风险远大于收益。
2. **patch logger**：给 `LLMLogger.write()` 打个补丁，把最后一次的 usage
   挂到 `logger.last_usage` 上。业务代码一行不动。

选 2。这是一个**受控的、单点的** monkey-patch：
- 只加一个属性，不改原有行为
- 幂等（重复 import 不会叠 patch）
- 失败不影响主流程（只影响成本统计）

## 代价

这是"侵入式"的，如果旧代码将来改 logger 实现，patch 会失效。
所以 `usage.RecordingLlm` 里做了退化估算（按字符数），
patch 失效时仍能有粗略数字，且报告里会标注 `estimated`。
"""

from __future__ import annotations

import logging

log = logging.getLogger("interview.usage_patch")

_PATCHED = False


def patch_llm_logger() -> bool:
    """给 LLMLogger 加上 last_usage 记录。返回是否成功。"""
    global _PATCHED
    if _PATCHED:
        return True

    try:
        from app.core import bridge
        bridge.ensure_legacy_importable()
        from llm import LLMLogger  # type: ignore
    except Exception as e:  # noqa: BLE001
        log.warning("无法导入 LLMLogger，跳过 patch：%s", e)
        return False

    original_write = LLMLogger.write

    def write(self, record: dict, response: str = "") -> None:
        # 先调原实现（不能破坏原有落盘行为）
        try:
            original_write(self, record, response)
        except Exception:  # noqa: BLE001
            pass

        # 再记 usage（不抛异常——统计失败不该影响主流程）
        try:
            u = record.get("usage") or {}
            if u:
                self.last_usage = {
                    "prompt_tokens": int(u.get("prompt_tokens") or 0),
                    "completion_tokens": int(u.get("completion_tokens") or 0),
                    "model": str(record.get("model") or ""),
                    "task": str(record.get("task") or ""),
                    "ok": bool(record.get("ok", True)),
                }
        except Exception:  # noqa: BLE001
            pass

    try:
        LLMLogger.write = write
        LLMLogger.last_usage = None       # type: ignore[attr-defined]
        _PATCHED = True
        log.info("LLMLogger 已 patch：usage 可通过 last_usage 读取")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("patch LLMLogger 失败：%s", e)
        return False


def wrap_llm(llm):
    """把任意 LLM 包成带记账的版本。patch 失败时也包（走估算）。"""
    try:
        patch_llm_logger()
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.core.usage import RecordingLlm
        return RecordingLlm(llm)
    except Exception as e:  # noqa: BLE001
        log.warning("包装 LLM 失败，返回原对象：%s", e)
        return llm
