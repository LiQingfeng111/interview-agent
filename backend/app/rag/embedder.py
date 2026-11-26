"""Embedding 端口与 Ollama 实现。

## 为什么抽端口

三个理由，都不是"为了好看"：

1. **测试需要确定性**：单测不该依赖 Ollama 常驻。用假实现能测"维度不对怎么办"
   "服务挂了怎么办"，这些在真实环境里很难构造。

2. **三视角地址**（实测过）：Windows/WSL/容器看到的 Ollama 地址不同。
   端口把这层隔离掉，业务代码只认 `embed(texts) -> vectors`。

3. **将来可能换模型**：bge-m3 → 别的模型，维度会变（1024 → 768/1536）。
   维度写进 `dimension()`，索引建表时校验，不匹配就明确报错而不是静默出垃圾结果。

## 实测参数（2026-11-26，本机）

    模型      bge-m3:latest（F16，566.70M 参数）
    大小      1,157,672,605 字节 ≈ 1.16 GB
    维度      1024
    归一化    L2 = 1.0000（已归一化，pgvector 内积 = 余弦）
    速度      单条 0.277s / 批量 10 条 1.608s（161ms/条）
    显存      size_vram = 689MB（完全在 GPU 上，未退化 CPU）
    722 题    约 116 秒全量向量化
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Protocol

from app.settings import settings

log = logging.getLogger("interview.embedder")


class Embedder(Protocol):
    """向量化端口。"""

    def dimension(self) -> int: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def health(self) -> dict: ...


class OllamaEmbedder:
    """调 Ollama 的 /api/embed。

    为什么不用 langchain_ollama 的 Embeddings 类：
    多一层封装就多一处行为不明（批量策略、重试、超时都是黑盒）。
    这一层只需要"字符串列表 → 向量列表"，直接 HTTP 反而更好控制。
    LangChain 的价值在链编排，不在这种单点调用。
    """

    def __init__(self, base_url: str = "", model: str = "", dim: int = 0):
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.embedding_model
        self._dim = dim or settings.embedding_dim
        self._batch = 16               # 实测 16 条一批比单条快 6 倍
        self._last_ok = 0.0

    def dimension(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化。空输入返回空列表（不报错）。"""
        if not texts:
            return []

        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            batch = texts[i:i + self._batch]
            out.extend(self._call(batch))
        return out

    def _call(self, batch: list[str], retries: int = 2) -> list[list[float]]:
        payload = {"model": self.model, "input": batch}
        body = json.dumps(payload, ensure_ascii=False).encode()
        last: Exception | None = None

        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(
                    self.base_url + "/api/embed",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.loads(r.read())
                vecs = data.get("embeddings") or []
                if len(vecs) != len(batch):
                    raise RuntimeError(
                        f"返回向量数 {len(vecs)} 与输入 {len(batch)} 不匹配"
                    )
                # 维度校验：模型换了维度会静默出垃圾结果，这里显式拦
                for v in vecs:
                    if len(v) != self._dim:
                        raise RuntimeError(
                            f"向量维度 {len(v)} 与配置 {self._dim} 不符，"
                            "请改 IA_EMBED_DIM 并重建索引"
                        )
                self._last_ok = time.time()
                return vecs
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                log.warning("embedding 失败（已重试 %d 次）：%s", retries, e)
        raise RuntimeError(f"向量化失败：{last}") from last

    def health(self) -> dict:
        try:
            t0 = time.time()
            v = self.embed(["健康检查"])
            return {
                "ok": True,
                "backend": "ollama",
                "model": self.model,
                "dim": len(v[0]) if v else 0,
                "latency_ms": int((time.time() - t0) * 1000),
                "base_url": self.base_url,
            }
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "backend": "ollama", "base_url": self.base_url,
                    "error": f"{type(e).__name__}: {e}"}


class HashEmbedder:
    """降级实现：确定性哈希向量（char 2/3-gram → dim 维）。

    只在 Ollama 完全不可用时启用。能力远弱于 bge-m3
    （抓不到同义改写），但保证面试流程不断。

    ## 关键：dim 必须和数据库列定义一致

    数据库里是 `vector(1024)`（对应 bge-m3）。
    第一版这里默认 256 维，结果降级后：
    - 写入时报 `expected 1024 dimensions, not 256`
    - 题库 722 题的 embedding 全是 NULL
    - 检索永远返回空

    所以默认维度改成**从 settings 读**（跟 bge-m3 一致），
    这样降级时写库也不会挂，只是向量质量差。
    """

    def __init__(self, dim: int = 0):
        # 默认跟随配置（1024），保证与数据库列定义一致
        self._dim = dim or settings.embedding_dim

    def dimension(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        import re
        norm = re.sub(r"[\s，。！？、,.!?：:；;（）()\-—…\"'“”]", "", (text or "").lower())
        v = [0.0] * self._dim
        grams = set()
        for n in (2, 3):
            for i in range(len(norm) - n + 1):
                grams.add(norm[i:i + n])
        for g in grams:
            h = 0x811c9dc5
            for b in g.encode():
                h ^= b
                h = (h * 0x01000193) & 0xFFFFFFFF
            v[h % self._dim] += 1.0
        norm2 = sum(x * x for x in v) ** 0.5
        if norm2:
            v = [x / norm2 for x in v]
        return v

    def health(self) -> dict:
        return {"ok": True, "backend": "hash-fallback", "dim": self._dim,
                "note": "Ollama 不可用时的降级实现，语义能力弱"}


# ---------------------------------------------------------------- 单例
_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """取向量化实现。

    ## 降级策略：宁可"不可用"也不要"用错维度写库"

    实测踩过的坑：Ollama 不可用时降级成 `HashEmbedder`（256 维），
    但数据库列是 `vector(1024)`。结果：
    - 写入时报 `expected 1024 dimensions, not 256`
    - 题库 722 题的 embedding 全是 NULL
    - 检索永远返回空（而且没有任何报错，只是"召回 0 条"）

    更糟的是：如果 HashEmbedder 也用 1024 维（我改过一版），
    它会**真的写进去**——但语义质量和 bge-m3 完全不是一个东西，
    后续换成真模型时索引里混着两种向量，检索结果不可信。

    所以现在的策略：
    - Ollama 可用 → 用 bge-m3（1024 维）
    - Ollama 不可用 → 返回一个 `UnavailableEmbedder`，
      它的 `embed()` 抛异常，调用方据此**跳过写入**并明确报错

    这样问题在第一次检索时就能看到"向量服务不可用"，
    而不是静默地得到"召回 0 条"这种误导性结果。
    """
    global _embedder
    if _embedder is not None:
        return _embedder

    candidate = OllamaEmbedder()
    h = candidate.health()
    if h.get("ok"):
        _embedder = candidate
        log.info("Embedding 就绪：bge-m3 %d 维 @ %s", h["dim"], candidate.base_url)
    else:
        _embedder = UnavailableEmbedder(h.get("error", "unknown"))
        log.error(
            "向量服务不可用：%s\n"
            "  → 语义检索会完全失效（题库/简历都查不了）\n"
            "  → 请确认 Ollama 在运行且网络可达（见 tools/setup_host_services.sh）",
            h.get("error"),
        )
    return _embedder


class UnavailableEmbedder:
    """向量服务不可用时的占位实现：调用即抛错。

    为什么不"返回零向量"或"降级到哈希"：
    那会让调用方以为成功了，把错误数据写进库。
    抛异常能保证问题在第一现场暴露。
    """

    def __init__(self, reason: str = ""):
        self._reason = reason

    def dimension(self) -> int:
        return settings.embedding_dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError(
            f"向量服务不可用（{self._reason}）。"
            f"语义检索依赖它，请先启动 Ollama："
            f"tools/setup_host_services.sh"
        )

    def health(self) -> dict:
        return {"ok": False, "backend": "unavailable", "error": self._reason}


def embedder_health() -> dict:
    try:
        return get_embedder().health()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
