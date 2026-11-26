"""联网搜索：仅用于"备课期"补全岗位知识库，面试过程中不调用。

为什么这样设计（面试必答题）：
  1. 延迟：实时搜索 1-3 秒，会打断面试节奏；
  2. 不确定性：搜索结果可能带回无关甚至错误内容，污染评分；
  3. 可复现：离线评估集要求同样的输入得到同样的输出。

后端分层（按可用性自动降级，永远不抛异常）：
  1. Serper / Tavily / Bocha 等真实搜索 API —— 需要环境变量里的 Key，质量最好
  2. Bing HTML 抓取 —— 免 Key，但**中文技术词召回质量差**（实测见下）
  3. 维基百科 API —— 免 Key，质量稳定但覆盖面窄
  4. 空列表 —— 上层退回纯离线知识库

⚠ 实测记录（这条最有价值，写进简历的叫"踩坑经验"）：
    搜「多 Agent 编排」→ 返回「多（汉语文字）_百度百科」，Bing 把中文复合词
    当成了查字典请求。加了英文锚点（LangGraph / agent orchestration）后
    才能拿到技术文档。所以 config.ROLE_KB 里每个话题都配了专门的
    英文+中文查询串，而不是简单拼 "话题名 + 面试"。
"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_H2_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
_SNIPPET = re.compile(r'<p class="b_lineclamp[^"]*"[^>]*>(?P<snippet>.*?)</p>', re.S)


def _clean(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _get(url: str, timeout: int = 12, payload: dict | None = None) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


# ---------------------------------------------------------------- 后端 1：搜索 API
def search_serper(query: str, limit: int = 5) -> list[dict]:
    key = os.getenv("SERPER_API_KEY", "")
    if not key:
        return []
    try:
        body = _get(
            "https://google.serper.dev/search",
            payload={"q": query, "num": limit, "hl": "zh-cn"},
        )
        data = json.loads(body)
    except Exception:
        return []
    out = []
    for item in (data.get("organic") or [])[:limit]:
        out.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
            "source": "serper",
        })
    return out


def search_tavily(query: str, limit: int = 5) -> list[dict]:
    key = os.getenv("TAVILY_API_KEY", "")
    if not key:
        return []
    try:
        body = _get(
            "https://api.tavily.com/search",
            payload={
                "api_key": key,
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
            },
        )
        data = json.loads(body)
    except Exception:
        return []
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("content", "")[:300],
            "source": "tavily",
        }
        for r in (data.get("results") or [])[:limit]
    ]


# ---------------------------------------------------------------- 后端 2：Bing 抓取
def search_bing(query: str, limit: int = 5) -> list[dict]:
    """解析 Bing 结果页。

    第一版用一个超长正则想一次匹配「li + h2 + p」，永远匹配不到：
    Bing 在 <li class="b_algo"> 和 <h2> 之间塞了一大段站点图标 div。
    改成"先按 li 切块，再在块内分别抓标题和摘要"，稳定得多。
    """
    url = "https://www.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "setlang": "zh-hans", "count": str(limit + 3)}
    )
    try:
        page = _get(url)
    except Exception:
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for chunk in page.split('<li class="b_algo"')[1:]:
        m = _H2_LINK.search(chunk)
        if not m:
            continue
        link = html.unescape(m.group("url"))
        if not link.startswith("http") or link in seen:
            continue
        title = _clean(m.group("title"))
        if not title:
            continue
        sm = _SNIPPET.search(chunk)
        seen.add(link)
        out.append({
            "title": title,
            "url": link,
            "snippet": _clean(sm.group("snippet")) if sm else "",
            "source": "bing",
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 后端 3：维基
def search_wikipedia(query: str, limit: int = 3, lang: str = "zh") -> list[dict]:
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": str(limit),
    })
    try:
        data = json.loads(_get(url))
    except Exception:
        return []
    return [
        {
            "title": item.get("title", ""),
            "url": f"https://{lang}.wikipedia.org/wiki/"
                   + urllib.parse.quote(item.get("title", "")),
            "snippet": _clean(item.get("snippet", ""))[:300],
            "source": "wikipedia",
        }
        for item in data.get("query", {}).get("search", [])
    ]


# ---------------------------------------------------------------- 主入口
def web_search(query: str, limit: int = 5, retries: int = 1) -> list[dict]:
    """搜索主入口：永不抛异常，返回 list（可能为空）。"""
    query = (query or "").strip()
    if not query:
        return []

    for fn in (search_serper, search_tavily):
        results = fn(query, limit)
        if results:
            return results

    for attempt in range(retries + 1):
        results = search_bing(query, limit)
        if results:
            return results
        if attempt < retries:
            time.sleep(0.6)

    return search_wikipedia(query, limit)


def provider_name() -> str:
    if os.getenv("SERPER_API_KEY"):
        return "serper(google)"
    if os.getenv("TAVILY_API_KEY"):
        return "tavily"
    return "bing-scrape(免Key，中文技术词质量有限)"


def digest(results: list[dict], max_chars: int = 900) -> str:
    """把搜索结果压成可塞进 prompt 的片段。"""
    lines, used = [], 0
    for r in results:
        piece = f"- {r['title']}：{r['snippet']}（{r['url']}）"
        if used + len(piece) > max_chars:
            break
        lines.append(piece)
        used += len(piece)
    return "\n".join(lines)
