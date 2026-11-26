"""录制 / 重放：让"这一场到底发生了什么"可以被第三方独立复现。

核心思路（cassette 模式）：把每次模型调用的输入指纹和输出一起落盘，
重放时按 request_id 直接取用，**不再真调模型**。这样：
  - 复盘不花钱、不受网络影响；
  - 改代码后能验证"同样的输入是否还是同样的输出"（回归）；
  - 报告可以写"重放一致 ✓"，而不是"我本地跑通了"。

与 LangGraph durable execution 的区别：不引入图/框架，只做"事件流 + 折叠"，
因为我们的流程是显式状态机，可测性不需要藏进框架里。
"""
from __future__ import annotations

import hashlib
import json
import os

from ledger import Event, Ledger, load_ledger

CASSETTE_DIR = "runs"


def fingerprint(prompt: str, task: str = "") -> str:
    """调用指纹：同输入同指纹，用于 cassette 命中判断。"""
    h = hashlib.sha1()
    h.update((task or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((prompt or "").encode("utf-8"))
    return h.hexdigest()[:16]


class Cassette:
    """一次会话的模型调用录像。"""

    def __init__(self, path: str = ""):
        self.path = path
        self.entries: dict[str, str] = {}
        self.hits = 0
        self.misses = 0
        if path and os.path.exists(path):
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    self.entries[d.get("key", "")] = d.get("response", "")
        except Exception:
            self.entries = {}

    def record(self, key: str, response: str) -> None:
        if not key:
            return
        self.entries[key] = response
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"key": key, "response": response}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def get(self, key: str) -> str | None:
        if key in self.entries:
            self.hits += 1
            return self.entries[key]
        self.misses += 1
        return None

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits, "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
            "entries": len(self.entries),
        }


class ReplayLLM:
    """重放用的假 LLM：只从 cassette 取，取不到就走 fallback 文本。

    注意：它**继承同一个 chat/chat_json 接口**，所以能直接塞进 session，
    不需要为了重放改业务代码。
    """

    is_mock = True

    def __init__(self, cassette: Cassette, fallback: str = ""):
        self.cassette = cassette
        self.fallback = fallback
        self.cfg = None

    def chat(self, messages, **kw) -> str:
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        key = fingerprint(prompt, kw.get("task", ""))
        out = self.cassette.get(key)
        return out if out is not None else self.fallback

    def chat_json(self, messages, **kw) -> dict:
        text = self.chat(messages, **kw)
        try:
            import re
            m = re.search(r"\{.*\}", text or "", re.S)
            return json.loads(m.group(0)) if m else {}
        except Exception:
            return {}


# ---------------------------------------------------------------- 快照比对
IGNORED_KEYS = {"started_at", "ts", "duration", "latency_ms", "session_id"}


def diff_snapshots(a: dict, b: dict, path: str = "") -> list[str]:
    """逐字段比对两个快照，返回差异描述（空列表 = 一致）。"""
    diffs: list[str] = []

    def walk(x, y, p):
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                if k in IGNORED_KEYS:
                    continue
                walk(x.get(k), y.get(k), f"{p}.{k}" if p else k)
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                diffs.append(f"{p}: 长度 {len(x)} → {len(y)}")
            for i, (xi, yi) in enumerate(zip(x, y)):
                walk(xi, yi, f"{p}[{i}]")
        elif x != y:
            diffs.append(f"{p}: {str(x)[:40]!r} ≠ {str(y)[:40]!r}")

    walk(a, b, path)
    return diffs


def assert_same(a: dict, b: dict) -> None:
    diffs = diff_snapshots(a, b)
    if diffs:
        raise AssertionError("快照不一致：\n" + "\n".join(diffs[:10]))


def replay(path: str, up_to_seq: int = 0) -> dict:
    """把事件流折叠成快照（up_to_seq 支持"回到第 N 轮"）。"""
    ledger = load_ledger(path)
    if up_to_seq:
        events = [e for e in ledger.events if e.seq <= up_to_seq]
        ledger.events = events
    return ledger.fold()


def cassette_path(session_id: str) -> str:
    return os.path.join(CASSETTE_DIR, f"cassette_{session_id}.jsonl")


def ledger_path(session_id: str) -> str:
    return os.path.join(CASSETTE_DIR, f"events_{session_id}.jsonl")
