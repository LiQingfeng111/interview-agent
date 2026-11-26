"""token 与成本统计：从旧的 LLM 调用日志里读。

## 为什么是"读日志"而不是"改接口"

旧 `llm.py` 的 `chat()` 返回值只有内容字符串，usage 写进了 `logs/*.jsonl`：

```json
{"task": "eval", "model": "...", "ms": 1200,
 "usage": {"prompt_tokens": 800, "completion_tokens": 120}}
```

改成"返回 usage"要动 `chat()` 签名 → 影响 30+ 调用点和 379 条测试。
**收益（拿到 token 数）和风险（改坏主链路）不成比例。**

所以选择：读它已经写好的日志。代价是有一点点延迟（日志文件可能还在缓冲），
但对"成本报告"这种非实时统计完全够用。

## 为什么按 session 时间窗过滤

日志是全局追加的，混着所有会话。要算"这一场花了多少"，
必须按时间窗切：`[session_created_at, now]`。

同一时间窗里如果有两个人并发面试，统计会串——
这是这个方案的已知局限。所以报告里会标注 `estimated: true`。
要精确的话得改 llm.py 传 request_id（将来做）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("interview.cost")

#: 单价（元 / 1K token）。按 deepseek-flash 量级估，报告里标注为估算。
PRICE_IN = 0.0005
PRICE_OUT = 0.002


def _logs_dir() -> Path:
    """旧核心的日志目录（`interview_agent/logs/`）。"""
    try:
        from app.core.bridge import _find_legacy_root
        return _find_legacy_root() / "logs"
    except Exception:  # noqa: BLE001
        return Path("logs")


def read_usage(since_ts: float = 0, until_ts: float = 0) -> dict[str, Any]:
    """扫日志，汇总时间窗内的 token 用量。

    Args:
        since_ts: 起始时间戳（unix 秒）。0 表示不限。
        until_ts: 结束时间戳。0 表示到现在。

    Returns:
        {calls, tokens_in, tokens_out, cost_cny, by_task, estimated}
    """
    d = _logs_dir()
    if not d.exists():
        return {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                "cost_cny": 0.0, "by_task": {}, "estimated": True,
                "note": "日志目录不存在"}

    tokens_in = tokens_out = calls = 0
    by_task: dict[str, int] = {}
    seen_files = 0

    # 按修改时间倒序，只扫最近的几个文件（日志会滚动）
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[:5]:
        seen_files += 1
        try:
            with f.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue

                    # 时间过滤：日志里字段名是 ts，格式是 ISO 字符串或 unix
                    ts = _to_epoch(rec.get("ts") or rec.get("time") or "")
                    if since_ts and ts and ts < since_ts:
                        continue
                    if until_ts and ts and ts > until_ts:
                        continue

                    usage = rec.get("usage") or {}
                    if not usage:
                        continue
                    ti = int(usage.get("prompt_tokens") or 0)
                    to = int(usage.get("completion_tokens") or 0)
                    if ti == 0 and to == 0:
                        continue

                    calls += 1
                    tokens_in += ti
                    tokens_out += to
                    task = str(rec.get("task") or "unknown")
                    by_task[task] = by_task.get(task, 0) + ti + to
        except OSError as e:
            log.debug("读日志失败 %s：%s", f, e)
            continue

    cost = round(tokens_in / 1000 * PRICE_IN + tokens_out / 1000 * PRICE_OUT, 6)

    return {
        "calls": calls,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens": tokens_in + tokens_out,
        "cost_cny": cost,
        "by_task": by_task,
        "files_scanned": seen_files,
        # 诚实标注：这是从日志估算的，并发场景可能串
        "estimated": True,
        "note": "从 llm 调用日志估算；并发会话可能串账",
    }


def _to_epoch(v: Any) -> float:
    """把各种时间表示转成 unix 秒。"""
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str) or not v:
        return 0.0
    try:
        from datetime import datetime
        # 处理 "2026-11-26T16:41:26" / "2026-11-26 16:41:26" / 带 Z
        s = v.replace("Z", "+00:00").replace(" ", "T", 1)
        return datetime.fromisoformat(s).timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def session_cost(session_created_at: str) -> dict[str, Any]:
    """算某场面试的用量（按创建时间起算）。"""
    since = _to_epoch(session_created_at)
    return read_usage(since_ts=since)


def attach_to_report(report: dict[str, Any], session_created_at: str) -> dict[str, Any]:
    """把成本信息挂到报告上。

    为什么挂在报告里而不是单独接口：
    用户看报告时最关心"这场花了多少"，单独再调一次接口反而麻烦。
    """
    if not report:
        return report
    try:
        cost = session_cost(session_created_at)
        report["cost"] = cost
        md = str(report.get("markdown") or "")
        if md and cost.get("tokens"):
            report["markdown"] = (
                md
                + f"\n\n——\n本场用量（估算）：{cost['tokens']} token，"
                + f"约 {cost['cost_cny']:.4f} 元（{cost['calls']} 次模型调用）"
            )
    except Exception as e:  # noqa: BLE001
        log.warning("附加成本信息失败：%s", e)
    return report
