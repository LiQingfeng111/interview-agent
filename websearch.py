"""运行时联网检索：追问时给面试官参考资料，验答时给评分官事实核对。

为什么不能直接用 search.py：
  实测免 Key 的 Bing 对普通中文查询几乎不可用——
  "多 Agent 编排 面试题" 返回的是《汉典》"多"字释义。
  但加 `site:` 限定后就正常。所以这里做的是"检索策略层"：
    1. 先按意图拼查询（技术词 + 站点限定 + 年份）
    2. 识别垃圾结果（字典页、百科词条、下载站）
    3. 垃圾率高就换站点重试，最多两轮
    4. 结果落盘缓存，避免同一问题反复打搜索源

两个使用场景（这也是它存在的理由）：
  - 追问：面试官要问"RAG 融合成本"这种点，先查一下当前主流做法，
    避免问到两年前的话术；
  - 验答：候选人说"RAG 一定需要重排"，评分官需要依据判断这句对不对，
    而不是凭模型印象附和。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from search import digest, search_bing, search_serper, search_tavily

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "search_cache")
CACHE_TTL = 3 * 24 * 3600       # 3 天：技术话题变化没那么快，但也不能用去年的

# 垃圾站点特征：命中就认为这次检索不可信
JUNK_HOSTS = (
    "baike.baidu.com", "zdic.net", "hanyu.baidu.com", "guoxuedashi", "cidian",
    "xh.5156edu", "dict.", "down.", "soft.", "zol.com.cn", "pcauto", "xcar",
)
JUNK_TITLE_WORDS = ("汉语文字", "字的意思", "组词", "部首", "拼音", "翻译", "下载", "报价")

# 站点限定的候选顺序（实测：知乎/卡码命中率最高）
SITE_HINTS = ("zhihu.com", "kamacoder.com", "juejin.cn", "csdn.net", "cnblogs.com")


def _cache_path(query: str) -> str:
    h = hashlib.md5(query.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{h}.json")


def _load_cache(query: str) -> list[dict] | None:
    path = _cache_path(query)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) < CACHE_TTL:
            return data.get("results") or []
    except (OSError, ValueError):
        pass
    return None


def _save_cache(query: str, results: list[dict]) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(_cache_path(query), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "query": query, "results": results},
                      f, ensure_ascii=False)
    except OSError:
        pass


# 技术领域词：检索结果里一个都没有，基本就是跑题了
TECH_WORDS = (
    "模型", "训练", "推理", "检索", "向量", "索引", "接口", "系统", "算法", "数据",
    "并发", "缓存", "队列", "服务", "评测", "指标", "框架", "Agent", "RAG", "LLM",
    "token", "embedding", "prompt", "API", "数据库", "延迟", "微调",
)


def is_off_domain(item: dict) -> bool:
    """结果与本领域无关（例：「评估体系」搜到「资产评估协会」）。

    为什么要单独判：这类结果不含垃圾特征词，但完全答非所问。
    把它当资料喂给评分官，比不给资料更危险——会生成看似有依据的假结论。
    """
    blob = f"{item.get('title', '')} {item.get('snippet', '')}"
    return not any(w in blob for w in TECH_WORDS)
def is_junk(item: dict) -> bool:
    url = (item.get("url") or "").lower()
    title = item.get("title") or ""
    if any(h in url for h in JUNK_HOSTS):
        return True
    return any(w in title for w in JUNK_TITLE_WORDS)


def quality(results: list[dict]) -> float:
    """可用结果占比。低于 0.5 说明这次检索不可信。"""
    if not results:
        return 0.0
    good = sum(1 for r in results if not is_junk(r) and not is_off_domain(r))
    return good / len(results)


def build_queries(term: str, kind: str = "tech") -> list[str]:
    """按意图拼查询。kind: tech（技术做法）/ fact（事实核对）/ interview（面经）。"""
    term = (term or "").strip()
    if not term:
        return []
    qs = []
    if kind == "fact":
        qs.append(f"{term} 原理 最佳实践")
        qs += [f"site:{s} {term}" for s in SITE_HINTS[:2]]
    elif kind == "interview":
        qs.append(f"{term} 面试题")
        qs += [f"site:{s} {term} 面试" for s in SITE_HINTS[:3]]
    else:
        qs.append(term)
        qs += [f"site:{s} {term}" for s in SITE_HINTS[:3]]
    return qs


def _raw_search(query: str, limit: int) -> list[dict]:
    for fn in (search_serper, search_tavily, search_bing):
        try:
            got = fn(query, limit) if fn is not search_bing else search_bing(query, limit)
        except Exception:
            continue
        if got:
            return got
    return []


def search(term: str, kind: str = "tech", limit: int = 5, min_quality: float = 0.5) -> dict:
    """带策略的多轮检索。返回 {ok, results, queries, quality}。

    ok=False 不是异常，是"这次没拿到可信资料"——调用方应当据此降级
    （面试官少给参考资料、评分官标 unverifiable），而不是硬编一段假依据。
    """
    queries = build_queries(term, kind)
    tried: list[str] = []
    best: dict = {"ok": False, "results": [], "queries": [], "quality": 0.0}
    for q in queries[:3]:
        cached = _load_cache(q)
        if cached is None:
            cached = _raw_search(q, limit)
            if cached:
                _save_cache(q, cached)
        tried.append(q)
        good = [r for r in cached if not is_junk(r) and not is_off_domain(r)]
        qscore = quality(cached)
        if qscore > best["quality"]:
            best = {"ok": qscore >= min_quality, "results": good or cached,
                    "queries": list(tried), "quality": round(qscore, 2)}
        if best["ok"]:
            break
        time.sleep(0.4)
    return best


def gather(term: str, kind: str = "tech", max_chars: int = 900) -> dict:
    """给 prompt 用的紧凑资料包。"""
    res = search(term, kind=kind, limit=5)
    text = digest(res["results"], max_chars=max_chars) if res["results"] else ""
    return {
        "ok": res["ok"],
        "text": text,
        "quality": res["quality"],
        "queries": res["queries"],
        "sources": [r.get("url", "") for r in res["results"][:3]],
    }
