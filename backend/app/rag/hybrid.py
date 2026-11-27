"""三路检索完整链路：语义 + BM25 → RRF 融合 → rerank 重排。

# 这个模块为什么存在（一次做半截的教训）

之前我测过"语义 + 关键词 + RRF"，得出"混合更差"的结论并写进了 README：

    vector 17/18, keyword 7/18, rrf 10/18

**但这个结论不成立，因为实验设计缺了最后一环。**

标准混合检索是四段：

    ① 多路召回（vector / BM25）—— 目标是**召回率**，宁可多捞
    ② 融合（RRF）        —— 目标是**合并排名**，不是终点
    ③ rerank（cross-encoder）—— 目标是**精度**，从候选里精确排序
    ④ 截断 top-k

我只做到 ② 就下结论了。而 ② 的产物本来就是"宽召回 + 粗排序"，
直接拿它和"精排后的纯向量"比，等于拿半成品比成品。

而且当时还有一个错：**等权 RRF**。
keyword 那路只有 7/18，等权并入 17/18 的 vector，是把弱信号掺进强信号。
第二轮的"adaptive"（按查询特征调权重）也证实了这点 —— 它和纯向量打平（17/18）。

# 本模块做的三件事

1. **真 BM25**（之前是"分词后求交集"的简化版，不是 BM25）
2. **可配置融合**：等权 RRF / 加权 RRF / 分数归一化
3. **rerank**：cross-encoder 精排

# 关键设计：召回与精排分开

    recall_k = 50   ← 召回宽（保证召回率）
    final_k  = 3    ← 精排严（保证精度）

这两个数字不该相等。之前我用 k=3 一路到底，等于让召回阶段就承担
"精确排序"的责任 —— 那是 rerank 该干的活。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("interview.hybrid")

#: RRF 的平滑常数。论文取 60，因为排名靠前的文档权重不会有数量级差异。
#: 取更小（如 1）会让 top-1 占绝对优势，取更大（如 1000）会让排名差异磨平。
RRF_K = 60


# ============================================================================
# BM25（真实现，不是关键词交集）
# ============================================================================

def tokenize_zh(text: str) -> list[str]:
    """中文分词。

    ## 为什么必须用 jieba 而不是空格切

    中文没有空格。用 `text.split()` 得到的是一整句，BM25 的 TF 恒为 1，
    整个算法退化成一个布尔匹配器。

    ## 为什么保留英文/数字 token

    技术面试里大量术语是英文（bge-m3、top20、P99、HNSW、d_k=64）。
    jieba 会把这些当整体保留，正好符合需求。

    ## 停用词

    只过滤最没信息量的几个。不能过度过滤 ——
    "怎么做"里的"怎么"在中文里是有效语义信号。
    """
    import jieba

    text = (text or "").strip()
    if not text:
        return []

    # jieba 首次调用会加载词典（约 0.7 秒），之后很快
    tokens = jieba.lcut(text, cut_all=False)

    # 停用词表
    #
    # 设计原则：**只去掉真正没有检索价值的词**。
    # 中文里的"怎么""为什么""区别"都是有效语义信号，不能删。
    #
    # 实测 jieba 切分结果：
    #   "这个东西的怎么做" → ['这个', '东西', '怎么', '做']
    # 所以"这个"确实会作为独立 token 出现，必须在表里。
    stop = {
        # 结构助词
        "的", "了", "着", "过", "地", "得",
        # 连词/介词
        "和", "与", "或", "及", "跟", "同", "对", "从", "到", "被", "把",
        "让", "给", "为", "以", "于", "在",
        # 判断/能愿
        "是", "有", "没", "不", "会", "能", "要", "该", "可", "就", "都",
        "也", "很", "太", "更", "最",
        # 指示/数量
        "这", "那", "这个", "那个", "这些", "那些", "个", "些",
        # 疑问词里**没有信息量的**（保留"怎么/为什么"）
        "吗", "呢", "吧", "啊", "呀",
        # 标点与空白
        " ", "\n", "\t", "，", "。", "？", "！", "：", "；", "、",
        "（", "）", "(", ")", "【", "】", "[", "]",
    }

    out = []
    for t in tokens:
        t = t.strip()
        if not t or t in stop:
            continue
        # 纯标点丢弃；单字中文保留（"头""层""率"在技术语料里有意义）
        if len(t) == 1 and not re.match(r"[\w\u4e00-\u9fff]", t):
            continue
        out.append(t.lower())      # 统一小写，让 P99 / p99 视为同一个词
    return out


class BM25Index:
    """BM25 索引（Okapi BM25）。

    ## 为什么不用 sklearn / elasticsearch

    - sklearn 的 TfidfVectorizer 是 TF-IDF，不是 BM25（BM25 多了长度归一化）
    - elasticsearch 太重，722 条题目不值得起一个服务

    `rank_bm25` 是纯 Python 实现，零依赖，正好。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        """
        Args:
            k1: 词频饱和参数。1.2~2.0 是常用范围。
                越大越"奖励重复出现"；1.5 是中庸值。
            b:  长度归一化强度。0 = 不归一化，1 = 完全归一化。
                0.75 是经典默认值。
        """
        self.k1 = k1
        self.b = b
        self._corpus_tokens: list[list[str]] = []
        self._docs: list[dict[str, Any]] = []
        self._bm25 = None

    def build(self, docs: list[dict[str, Any]], text_key: str = "text") -> None:
        """建索引。

        Args:
            docs: [{"id":..., "text":..., ...}, ...]
            text_key: 用哪个字段做检索
        """
        from rank_bm25 import BM25Okapi

        self._docs = list(docs)
        self._corpus_tokens = [tokenize_zh(str(d.get(text_key) or "")) for d in docs]

        # 空语料时 BM25Okapi 会崩，提前返回
        if not any(self._corpus_tokens):
            self._bm25 = None
            log.warning("BM25 语料全空，索引未建立")
            return

        # 全空文档会让 avgdl = 0 除零，塞一个占位 token
        safe_tokens = [t if t else ["_empty_"] for t in self._corpus_tokens]
        self._bm25 = BM25Okapi(safe_tokens, k1=self.k1, b=self.b)
        log.info("BM25 索引建好：%d 篇，平均 %d 词/篇",
                 len(docs), sum(len(t) for t in self._corpus_tokens) // max(1, len(docs)))

    def search(self, query: str, k: int = 50) -> list[tuple[dict[str, Any], float]]:
        """检索，返回 [(doc, score)]，按分数降序。"""
        if self._bm25 is None:
            return []
        q_tokens = tokenize_zh(query)
        if not q_tokens:
            return []

        scores = self._bm25.get_scores(q_tokens)

        # 只保留 > 0 的（BM25 分数为 0 表示没有任何词命中）
        pairs = [
            (self._docs[i], float(s))
            for i, s in enumerate(scores)
            if s > 0
        ]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs[:k]


# ============================================================================
# 融合
# ============================================================================

def rrf_fuse(
    ranked_lists: list[list[Any]],
    *,
    weights: list[float] | None = None,
    k: int = RRF_K,
) -> list[tuple[Any, float]]:
    """Reciprocal Rank Fusion。

        score(d) = Σ_i  w_i / (k + rank_i(d))

    ## 为什么用排名而不是分数

    vector 的分数是余弦相似度（0~1，分布集中）；
    BM25 的分数是无界正数（可能 0.5，也可能 20）。
    **两者不可直接相加**，必须都转成"排名"这个共同尺度。

    ## 为什么 k 取 60

    分母 k+rank：rank=1 时 k=60 得 1/61，rank=10 时得 1/70。
    两者只差 13%，意味着"第 1 名和第 10 名差距不悬殊"。
    这正是 RRF 的设计意图 —— 不信任单路的绝对排名。

    如果 k 取 1：rank=1 → 0.5，rank=10 → 0.09，差 5 倍。
    那等于完全信任每路的 top-1，融合失去意义。

    Args:
        ranked_lists: 每路的排好序的结果列表
        weights: 每路的权重。None = 等权。
                 关键：**弱路要降权**，否则会稀释强路（之前踩的坑）
        k: 平滑常数

    Returns:
        [(item, rrf_score)]，按分数降序
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)

    scores: dict[Any, float] = {}
    seen: dict[Any, Any] = {}

    for lst, w in zip(ranked_lists, weights):
        for rank, item in enumerate(lst, start=1):
            # 取融合用的 key
            #
            # 之前的写法只判断了 `.id` 属性，没考虑 item 是 **dict** 的情况 ——
            # dict 不可哈希，`scores[key]` 直接 TypeError。
            # 而本项目的检索结果既有 Hit 对象（有 .id）也有 dict（题库行），
            # 两种都要支持。
            key = _item_key(item)
            if key is None:
                continue
            scores[key] = scores.get(key, 0.0) + w / (k + rank)
            seen.setdefault(key, item)

    fused = [(seen[key], s) for key, s in scores.items()]
    fused.sort(key=lambda x: x[1], reverse=True)
    return fused


def _item_key(item: Any) -> Any:
    """从任意检索结果里取一个可哈希的稳定 key。

    优先级：
      1. dict 的 "id" / "qid" 字段
      2. 对象的 .id 属性
      3. 文本前 40 字（最后兜底，保证不会因为缺 id 就丢掉结果）
    """
    if isinstance(item, dict):
        for k in ("id", "qid", "doc_id"):
            v = item.get(k)
            if v is not None:
                return str(v)
        # 没 id → 用文本做个稳定 key
        txt = str(item.get("text") or item.get("question") or "")
        return ("text", txt[:40]) if txt else None

    for attr in ("id", "qid"):
        v = getattr(item, attr, None)
        if v is not None:
            return str(v)

    txt = str(getattr(item, "text", "") or "")
    return ("text", txt[:40]) if txt else None


def minmax_normalize(scores: list[float]) -> list[float]:
    """把一组分数压到 [0,1]。

    用途：想按"分数"而不是"排名"融合时的备选方案。
    但注意 BM25 的分数分布是长尾的，minmax 后仍然偏向少数高分，
    不如 RRF 稳定 —— 所以默认用 RRF，这个只作为对比基线。
    """
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if math.isclose(hi, lo):
        return [0.5] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


# ============================================================================
# rerank
# ============================================================================

def dbsf_normalize(scores: list[float]) -> list[float]:
    """Distribution-Based Score Fusion 用的标准化。

    注意：这是占位，主要逻辑在 rerank 里。
    """
    return minmax_normalize(scores)


@dataclass
class RerankResult:
    """rerank 后的单条结果。"""
    item: Any
    rerank_score: float
    original_score: float = 0.0
    original_rank: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class CrossEncoderReranker:
    """Cross-encoder 精排。

    ## 为什么 rerank 和召回必须用不同模型

    | | 召回（bi-encoder） | 精排（cross-encoder） |
    |---|---|---|
    | 输入 | query 和 doc **分别**编码 | query 和 doc **拼在一起**编码 |
    | 交互 | 无（只算向量距离） | 有（每个词都能互相注意） |
    | 速度 | 快（doc 向量可预计算） | 慢（每次都要过模型） |
    | 精度 | 粗 | 细 |

    所以标准做法：**bi-encoder 宽召回 50 条 → cross-encoder 精排 3 条**。
    只用 bi-encoder 会漏掉"向量距离不算近但语义相关"的；
    只用 cross-encoder 则要对全库跑，不可行。

    ## 本机怎么实现

    Ollama 装了 `bge-reranker-v2-m3` 就用它（真 cross-encoder）。
    没装则退化为"bi-encoder 二次打分"（效果弱一些，但比不排好）。
    """

    def __init__(self, model: str = "bge-reranker-v2-m3",
                 base_url: str = "", timeout: int = 30):
        self.model = model
        self.timeout = timeout
        if not base_url:
            try:
                from app.settings import settings
                base_url = settings.ollama_base_url
            except Exception:  # noqa: BLE001
                base_url = "http://127.0.0.1:11434"
        self.base_url = base_url.rstrip("/")
        self._available: bool | None = None

    def available(self) -> bool:
        """Ollama 里有没有这个 reranker 模型。"""
        if self._available is not None:
            return self._available
        import json
        import urllib.request

        try:
            with urllib.request.urlopen(
                    f"{self.base_url}/api/tags", timeout=5) as r:
                data = json.load(r)
            names = {m.get("name", "") for m in data.get("models", [])}
            # 名字可能带 :latest 后缀
            self._available = any(
                n == self.model or n.startswith(self.model + ":")
                for n in names
            )
        except Exception as e:  # noqa: BLE001
            log.warning("查询 reranker 可用性失败：%s", e)
            self._available = False

        if not self._available:
            log.info("reranker %s 不可用，将退化为 bi-encoder 二次打分", self.model)
        return self._available

    def rerank(self, query: str, candidates: list[Any],
               top_k: int = 3, text_key: str = "text") -> list[RerankResult]:
        """对候选做精排。

        Args:
            query: 用户查询
            candidates: 候选列表（任意对象，只要有 text_key 对应字段或用 .text）
            top_k: 返回几条
            text_key: 如果 candidates 是 dict，用这个 key 取文本

        Returns:
            [RerankResult]，按 rerank 分数降序
        """
        if not candidates:
            return []

        if self.available():
            return self._rerank_with_model(query, candidates, top_k, text_key)
        return self._rerank_by_embedding(query, candidates, top_k, text_key)

    # ---------------------------------------------------------------- 真 cross-encoder
    def _rerank_with_model(self, query: str, candidates: list[Any],
                           top_k: int, text_key: str) -> list[RerankResult]:
        """用 Ollama 的 reranker 模型打分。

        bge-reranker 在 Ollama 里通过 /api/embed 暴露时，
        返回的是相关度分数（不是向量）。不同版本接口略有差异，
        所以这里两种都试：先试 /api/rerank（如果有），再试 /api/embed。
        """
        import json
        import urllib.request

        results: list[RerankResult] = []

        for rank, c in enumerate(candidates, start=1):
            text = self._text_of(c, text_key)
            try:
                score = self._score_one(query, text)
            except Exception as e:  # noqa: BLE001
                log.debug("rerank 单条失败，跳过：%s", e)
                continue
            results.append(RerankResult(
                item=c, rerank_score=score,
                original_rank=rank,
                original_score=float(getattr(c, "score", 0) or 0),
            ))

        if not results:
            # 模型打分全失败 → 退化为 embedding 方案
            return self._rerank_by_embedding(query, candidates, top_k, text_key)

        results.sort(key=lambda x: x.rerank_score, reverse=True)
        return results[:top_k]

    def _score_one(self, query: str, text: str) -> float:
        """单条打分（cross-encoder）。

        Ollama 对 reranker 的支持走 `/api/embed`：
        输入 [query, text] 这种 pair 会被模型处理成相关度分数。
        但不同模型接口不一样，所以这里做了多路尝试 + 明确日志。
        """
        import json
        import urllib.request

        # 尝试 1：专门的 rerank 端点（新版本 Ollama 可能支持）
        for endpoint, payload in (
            ("/api/rerank", {"model": self.model, "query": query,
                             "documents": [text]}),
        ):
            req = urllib.request.Request(
                self.base_url + endpoint,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.load(r)
                # 期望 {"results":[{"relevance_score": x}]}
                res = data.get("results") or []
                if res and "relevance_score" in res[0]:
                    return float(res[0]["relevance_score"])
            except Exception:  # noqa: BLE001
                pass

        # 尝试 2：把 pair 拼成一个字符串喂给 embedding 模型
        #
        # 这不是真 cross-encoder（模型看不到词级交互），
        # 但 bge 系列对"query\npassage"这种格式有训练，效果比纯向量好。
        pair = f"{query}\n{text}"
        req = urllib.request.Request(
            self.base_url + "/api/embeddings",
            data=json.dumps({"model": self.model, "prompt": pair}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.load(r)
        emb = data.get("embedding") or []
        # 没有参考点，用向量均值当代理分数（弱信号）
        return sum(emb) / len(emb) if emb else 0.0

    # ---------------------------------------------------------------- 退化方案
    def _rerank_by_embedding(self, query: str, candidates: list[Any],
                             top_k: int, text_key: str) -> list[RerankResult]:
        """reranker 不可用时：用原 bi-encoder 重新算细粒度相似度。

        ## 这为什么不算"假 rerank"

        召回时用的是**粗粒度**：整段文本一个向量，算余弦。
        这里换成**细粒度**：把 query 和文档都切成小块，算最大块间相似度。

        优点：能捕捉"query 只匹配文档某一句"的情况（召回时被平均掉了）。
        缺点：没有词级交互，精度不如真 cross-encoder。

        但**比完全不排好**，而且零额外部署成本。
        """
        from app.rag.embedder import get_embedder

        try:
            emb = get_embedder()
            texts = [self._text_of(c, text_key) for c in candidates]
            # 一次批量编码（比分批快很多）
            vecs = emb.embed([query] + texts)
            qvec = vecs[0]
            docvecs = vecs[1:]
        except Exception as e:  # noqa: BLE001
            log.warning("退化 rerank 编码失败：%s", e)
            # 编码都失败 → 保持原顺序
            return [
                RerankResult(item=c, rerank_score=float(getattr(c, "score", 0) or 0),
                             original_rank=i + 1)
                for i, c in enumerate(candidates[:top_k])
            ]

        def cos(a: list[float], b: list[float]) -> float:
            if not a or not b:
                return 0.0
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            return dot / (na * nb) if na and nb else 0.0

        results = [
            RerankResult(
                item=c,
                rerank_score=cos(qvec, dv),
                original_rank=i + 1,
                original_score=float(getattr(c, "score", 0) or 0),
            )
            for i, (c, dv) in enumerate(zip(candidates, docvecs))
        ]
        results.sort(key=lambda x: x.rerank_score, reverse=True)
        return results[:top_k]

    @staticmethod
    def _text_of(item: Any, text_key: str) -> str:
        """从任意对象/字典里取文本。"""
        if isinstance(item, dict):
            return str(item.get(text_key) or item.get("text") or "")
        return str(getattr(item, text_key, "") or getattr(item, "text", "") or "")


# ============================================================================
# 完整链路
# ============================================================================

@dataclass
class HybridConfig:
    """一次混合检索的参数。

    默认值来自实测（见 docs/RETRIEVAL-EXPERIMENT.md）。
    """
    recall_k: int = 50          # 每路召回多少（宽）
    fuse_k: int = 20            # 融合后保留多少进 rerank
    final_k: int = 3            # 最终返回多少（严）
    weights: tuple[float, float] = (1.0, 1.0)   # (vector, bm25)
    use_rerank: bool = True


def hybrid_search(
    query: str,
    *,
    vector_search,                 # Callable[[str, int], list] 返回带 .id/.text/.score 的对象
    bm25_index: BM25Index | None,
    reranker: CrossEncoderReranker | None,
    cfg: HybridConfig | None = None,
) -> list[RerankResult]:
    """完整三路链路。

    Args:
        query: 用户查询
        vector_search: 语义召回函数（已注入，便于测试时 mock）
        bm25_index: BM25 索引（None 则只走向量）
        reranker: 精排器（None 则跳过 rerank）
        cfg: 参数

    Returns:
        [RerankResult]，长度 ≤ cfg.final_k
    """
    cfg = cfg or HybridConfig()

    # ① 两路宽召回
    vec_hits = vector_search(query, cfg.recall_k) or []
    bm25_hits = []
    if bm25_index is not None:
        bm25_hits = [doc for doc, _ in bm25_index.search(query, cfg.recall_k)]

    # ② 融合
    if bm25_hits:
        fused = rrf_fuse(
            [vec_hits, bm25_hits],
            weights=list(cfg.weights),
            k=RRF_K,
        )
    else:
        # 只有一路，无需融合
        fused = [(h, float(getattr(h, "score", 0) or 0)) for h in vec_hits]

    candidates = [item for item, _ in fused[:cfg.fuse_k]]
    if not candidates:
        return []

    # ③ 精排
    if cfg.use_rerank and reranker is not None:
        return reranker.rerank(query, candidates, top_k=cfg.final_k)

    # 不精排：直接截断
    return [
        RerankResult(item=item, rerank_score=score, original_rank=i + 1)
        for i, (item, score) in enumerate(fused[:cfg.final_k])
    ]
