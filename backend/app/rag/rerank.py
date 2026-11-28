"""真 cross-encoder reranker（sentence-transformers 实现）。

> ## 状态：已实现，**当前未接入生产链路**
>
> 依赖已装（torch / transformers / sentence-transformers），代码可用，
> 但 `bank_select` / `interview_graph` 都不 import 它。
>
> 原因：本次的检索问题不在排序 —— 见 `docs/RETRIEVAL-EXPERIMENT.md`。
> 简版：题库有 `topic` 字段但没用来过滤，导致"用话题名匹配题干文本"
> 命中率只有 20-25%。加 topic 过滤后直接到 100%，不需要精排。

---

# 为什么没启用（实测）

我完整实现并测了这条路线：

    词面 rerank（IDF 重叠 / 位置加权 / 短语匹配，6 种组合）
        → 全部比基线差 4 个点，B/C 类全灭
        → 原因：同义改写/中英对照的查询和目标字面完全不同，
           词面打分给正确目标 0 分

    细粒度向量精排（把候选切句，算 query 与最相似句子的分数）
        → 18 条逐条相同，0 点变化
        → 原因：同一个模型换粒度得到相同排序，
           说明 bge-m3 认为这些就是不相似

这时候才回头去看"生产查询到底长什么样"，然后发现：
**我一直在用不存在的输入做实验。**

生产查询是状态机给的话题名（"RAG"），而 RAG 题的题干里没有"RAG"。
候选池里没有正确的题，精排救不回来。

# 什么时候该启用

- 候选池里**确实有**正确的题，但排名靠后（top-50 有、top-3 没有）
- 需要区分"表述不同但都正确"的多个候选
- 题库规模大，topic 过滤后仍有几十上百个候选需要细排

判断方法：先量 `recall_k=50 的覆盖率` 和 `top-3 命中率`。
如果覆盖率低 → 问题在召回/查询方式，加 rerank 没用。
如果覆盖率高但 top-3 低 → rerank 才是对的工具。

---

# 原始设计说明（写于实现时）

# 为什么需要这个文件

`app/rag/hybrid.py` 里的 `CrossEncoderReranker` 走 Ollama，
但 **Ollama 官方模型库没有 bge-reranker**（实测 pull 全部 500 / 超时）。

而 sentence-transformers 可以直接加载 HuggingFace 上的
`BAAI/bge-reranker-v2-m3`，这才是标准做法。本文件是它的封装。

# 和 bi-encoder 的本质区别

    bi-encoder（bge-m3，召回用）
        query ──→ [encoder] ──→ vec_q ┐
                                       ├─→ 余弦相似度
        doc   ──→ [encoder] ──→ vec_d ┘
        ↑ 两者**分别**编码，编码时看不到对方

    cross-encoder（bge-reranker，精排用）
        [query, doc] ──→ [encoder] ──→ 相关度分数
        ↑ **拼在一起**编码，每个 token 能看到对方所有 token

这个差异决定了两者能力边界：
  · bi-encoder 适合宽召回（向量可预计算，快）
  · cross-encoder 适合精排（能捕捉"这两句话到底在说什么"）

# 首次运行会下载模型（约 2.2GB）

模型缓存在 `~/.cache/huggingface/`。之后加载约 3 秒。

本机实测下载速度 183 KB/s，2.2GB 需要约 3.4 小时。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("interview.rerank")

#: 默认模型。bge-reranker-v2-m3 是多语言版（支持中文），
#: 参数量 568M，CPU 上单条约 20-40ms。
#:
#: 备选：
#:   BAAI/bge-reranker-base   更小（278M），快一倍，精度略低
#:   BAAI/bge-reranker-large  更大（560M），精度更高，慢
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"


@dataclass
class Reranked:
    """rerank 结果。"""
    item: Any
    score: float
    original_rank: int = 0
    original_score: float = 0.0


class SentenceTransformerReranker:
    """用 sentence-transformers 加载 cross-encoder 做精排。

    ## 为什么懒加载

    加载模型要约 3 秒 + 2.2GB 内存。如果每次检索都建一个，
    或者启动时就加载（哪怕不用 rerank），都是浪费。

    所以：**第一次 rerank 调用时才加载**，之后复用。
    """

    def __init__(self, model_name: str = DEFAULT_MODEL,
                 max_length: int = 512, device: str = "cpu"):
        self.model_name = model_name
        self.max_length = max_length
        self.device = device
        self._model = None
        self._load_failed = False
        self._load_ms = 0.0

    # ---------------------------------------------------------------- 加载
    def _load(self) -> bool:
        """加载模型。失败返回 False（调用方据此降级）。"""
        if self._model is not None:
            return True
        if self._load_failed:
            return False

        try:
            from sentence_transformers import CrossEncoder

            log.info("加载 reranker %s（首次会下载约 2.2GB）…", self.model_name)
            t0 = time.time()
            self._model = CrossEncoder(
                self.model_name,
                max_length=self.max_length,
                device=self.device,
            )
            self._load_ms = (time.time() - t0) * 1000
            log.info("reranker 就绪（%.1f 秒）", self._load_ms / 1000)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("reranker 加载失败（将退回原始排序）：%s", e)
            self._load_failed = True
            return False

    def available(self) -> bool:
        """能否用（会触发首次加载）。"""
        return self._load()

    # ---------------------------------------------------------------- 精排
    def rerank(
        self,
        query: str,
        candidates: list[Any],
        top_k: int = 3,
        text_key: str = "text",
        batch_size: int = 16,
    ) -> list[Reranked]:
        """对候选精排。

        Args:
            query: 用户查询
            candidates: 候选（dict 或对象，取 text_key / .text）
            top_k: 返回几条
            text_key: dict 的文本字段名
            batch_size: 批大小。CPU 上 16 比较合适（再大会吃内存）

        Returns:
            [Reranked]，按 cross-encoder 分数降序。
            模型加载失败时返回**原顺序**（不假装排过）。
        """
        if not candidates:
            return []

        if not self._load():
            # 加载失败：保持原顺序，明确标注分数为原始分
            return [
                Reranked(
                    item=c,
                    score=float(getattr(c, "score", 0) or
                                (c.get("score", 0) if isinstance(c, dict) else 0)),
                    original_rank=i + 1,
                )
                for i, c in enumerate(candidates[:top_k])
            ]

        pairs = [(query, self._text_of(c, text_key)) for c in candidates]

        try:
            scores = self._model.predict(
                pairs,
                batch_size=batch_size,
                show_progress_bar=False,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("rerank 推理失败（保持原序）：%s", e)
            return [
                Reranked(item=c, score=0.0, original_rank=i + 1)
                for i, c in enumerate(candidates[:top_k])
            ]

        results = [
            Reranked(
                item=c,
                score=float(s),
                original_rank=i + 1,
                original_score=float(
                    getattr(c, "score", 0) or
                    (c.get("score", 0) if isinstance(c, dict) else 0)),
            )
            for i, (c, s) in enumerate(zip(candidates, scores))
        ]
        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]

    @staticmethod
    def _text_of(item: Any, text_key: str) -> str:
        if isinstance(item, dict):
            return str(item.get(text_key) or item.get("text") or "")
        return str(getattr(item, text_key, "") or getattr(item, "text", "") or "")

    # ---------------------------------------------------------------- 诊断
    def stats(self) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "loaded": self._model is not None,
            "load_failed": self._load_failed,
            "load_ms": round(self._load_ms, 1),
            "max_length": self.max_length,
            "device": self.device,
        }


# ---------------------------------------------------------------- 单例

_INSTANCE: SentenceTransformerReranker | None = None


def get_reranker(model_name: str = DEFAULT_MODEL) -> SentenceTransformerReranker:
    """取全局 reranker（避免重复加载 2.2GB 模型）。"""
    global _INSTANCE
    if _INSTANCE is None or _INSTANCE.model_name != model_name:
        _INSTANCE = SentenceTransformerReranker(model_name=model_name)
    return _INSTANCE
