"""岗位知识库：离线可用的"考点树"，外加可选的联网补全。

为什么知识库要分层？
  岗位知识（稳定） / 话题（稳定） / 追问角度（稳定） / 实时资料（易变）
前 3 层手写，第 4 层靠 search.py 备课式抓取并缓存。
这样离线能跑，联网能变强——面试时这正是"降级设计"的加分点。
"""
from __future__ import annotations

import json
import os
import random
import re

from config import CACHE_DIR, ROLE_KB, topic_entry

ROLE_ALIASES = {
    "算法": "ai_algo", "ai": "ai_algo", "algo": "ai_algo", "ml": "ai_algo",
    "后端": "backend", "backend": "backend", "服务端": "backend",
    "agent": "agent_app", "应用": "agent_app", "llm": "agent_app",
    "通用": "general", "general": "general", "开发": "general",
}


def resolve_role(text: str) -> str:
    t = (text or "").strip().lower()
    if t in ROLE_KB:
        return t
    for k, v in ROLE_ALIASES.items():
        if k in t:
            return v
    return "agent_app"  # 默认：AI 学生投 Agent 方向最常见


def role_topics(role_key: str) -> list[str]:
    kb = ROLE_KB.get(role_key, ROLE_KB["general"])
    return [t[0] for t in kb["topics"]]


def topic_angles(role_key: str, topic: str) -> list[str]:
    return list(topic_entry(role_key, topic)[1])


def topic_queries(role_key: str, topic: str) -> list[str]:
    entry = topic_entry(role_key, topic)
    queries = entry[2] if len(entry) > 2 else []
    return list(queries) or [f"{topic} 技术 教程"]


def topic_to_question_seed(topic: str) -> str:
    """把考点转成"提问角度"，喂给提问官做参考（不是让模型照抄）。"""
    if not topic:
        return ""
    for role_key, kb in ROLE_KB.items():
        for item in kb["topics"]:
            if item[0] == topic or topic in item[0]:
                return "；".join(item[1])
    return ""


# JD 用词和知识库话题名经常不是同一个词：
#   JD 写「评测」→ 话题叫「评估体系」
#   JD 写「Agent」/「智能体」→ 话题叫「多 Agent 编排」
#   JD 写「RAG」→ 话题叫「RAG 检索增强生成」（含"检索"）
# 不做同义词映射，JD 关注点就会被静默忽略——这是测试实际抓出来的问题。
JD_SYNONYMS = {
    "评测": ["评估", "测试", "eval"],
    "评估": ["评测", "测试"],
    "测试": ["评测", "评估"],
    "rag": ["检索", "向量"],
    "检索": ["rag", "向量"],
    "agent": ["智能体", "编排", "多智能体"],
    "智能体": ["agent", "编排"],
    "编排": ["agent", "智能体", "多智能体"],
    "结构化": ["schema", "json"],
    "上下文": ["memory", "记忆"],
    "记忆": ["memory", "上下文"],
    "工具调用": ["工具", "function"],
    "微调": ["lora", "peft", "sft"],
    "lora": ["微调", "peft"],
    "索引": ["b+", "innodb", "回表"],
    "缓存": ["redis", "一致性"],
    "限流": ["令牌桶", "熔断"],
    "消息队列": ["kafka", "rocketmq"],
}


def pick_tech_topics(
    role_key: str,
    n: int = 3,
    seed: int | None = None,
    prefer: list[str] | None = None,
) -> list[str]:
    """挑技术考点。

    prefer: 来自 JD 的关键词。JD 提到的话题优先排前面——
    对标 PolyInterview 的做法：问题应由「简历 × JD」共同决定，
    只按岗位随机抽会让面试和实际岗位要求脱节。
    """
    topics = role_topics(role_key)
    prefer = [p.lower() for p in (prefer or []) if p]

    def jd_score(topic: str) -> int:
        t = topic.lower()
        score = 0
        for p in prefer:
            candidates = [p] + JD_SYNONYMS.get(p, [])
            if any(c in t for c in candidates):
                score += 1
                continue
            # 反向匹配：话题里的词出现在 JD 关键词里也算命中
            for tok in re.split(r"[\s/、，,（）()]+", t):
                if len(tok) >= 2 and (tok in p or any(tok in s for s in JD_SYNONYMS.get(p, []))):
                    score += 1
                    break
        return score

    if prefer:
        ranked = sorted(topics, key=jd_score, reverse=True)
        hit = [t for t in ranked if jd_score(t) > 0]
        if len(hit) >= n:
            return hit[:n]
        rest_rng = random.Random(seed)
        remainder = [t for t in topics if t not in hit]
        rest_rng.shuffle(remainder)
        return (hit + remainder)[:n]

    rng = random.Random(seed)
    if len(topics) <= n:
        return topics
    return rng.sample(topics, n)


# ---------------------------------------------------------------- 联网备课
def cache_path(role_key: str) -> str:
    return os.path.join(CACHE_DIR, f"kb_{role_key}.json")


def load_cached(role_key: str) -> dict:
    path = cache_path(role_key)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cached(role_key: str, data: dict) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = cache_path(role_key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


# 判定"这条结果是否与话题相关"的通用锚点。
# 不直接用话题的追问角度做锚点——实测角度（如"角色边界怎么划"）几乎
# 不会出现在搜索结果摘要里，用它们过滤会把所有结果都干掉。
TECH_HINTS = (
    "llm", "大模型", "大语言模型", "agent", "智能体", "rag", "检索",
    "向量", "embedding", "prompt", "提示词", "模型", "推理", "训练",
    "微调", "fine-tun", "lora", "token", "api", "框架", "架构", "并发",
    "缓存", "索引", "数据库", "队列", "分布式", "接口", "代码", "编程",
    "python", "java", "langchain", "langgraph", "json", "schema", "性能",
    "gpu", "神经网络", "transformer", "上下文", "评估", "测试",
)

# 明确是"查字典 / 在线小工具 / 翻译"的垃圾结果特征
JUNK_HINTS = (
    "汉语词语", "汉语文字", "拼音", "部首", "笔顺", "组词",
    "词典", "翻译：", "剑桥词典", "音标", "爱词霸", "汉字",
    "是什么意思_", "的翻译_", "在线工具", "在线解析", "格式化工具",
    "英文单词", "英语单词", "读音", "例句",
)

_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{2,}")


def _latin_tokens(text: str) -> set[str]:
    return {t.lower() for t in _LATIN_TOKEN.findall(text or "")}


def relevance_score(result: dict, query: str) -> int:
    """给一条搜索结果打相关分，用于过滤垃圾。

    为什么要打分而不是简单的布尔判断？
    因为 Bing 抓取会稳定返回三类垃圾：字典释义、在线小工具、无关科普。
    布尔判断（"含技术词即通过"）会把 JSON 在线格式化工具也放进来。
    打分后设阈值，并通过率还能当指标汇报——"备课结果相关率 80%"，
    这比"我做了联网搜索"有说服力得多。
    """
    text = f"{result.get('title', '')} {result.get('snippet', '')}".lower()
    if len((result.get("snippet") or "").strip()) < 20:
        return 0
    if any(j in text for j in JUNK_HINTS):
        return 0
    score = 0
    # 查询里的技术 token 是否出现在结果里（最强信号）
    if _latin_tokens(query) & _latin_tokens(text):
        score += 2
    score += sum(1 for h in TECH_HINTS if h in text)
    return score


def is_useful(result: dict, query: str = "", min_score: int = 3) -> bool:
    return relevance_score(result, query) >= min_score


def enrich_role(
    role_key: str,
    use_search: bool = True,
    max_topics: int | None = None,
    force: bool = False,
) -> dict:
    """为岗位补充"最新资料摘要"。面试中不调用，只在开始前备课一次。"""
    base = {
        "role": role_key,
        "name": ROLE_KB.get(role_key, {}).get("name", ""),
        "topics": {},
        "meta": {},
    }
    if not use_search:
        return base

    from search import provider_name, web_search  # 延迟导入，避免无网环境启动失败

    cached = {} if force else load_cached(role_key)
    topics = role_topics(role_key)
    if max_topics:
        topics = topics[:max_topics]

    for topic in topics:
        if topic in cached.get("topics", {}) and cached["topics"][topic]:
            base["topics"][topic] = cached["topics"][topic]
            continue

        collected: list[dict] = []
        seen_urls: set[str] = set()
        raw_count = 0
        for query in topic_queries(role_key, topic):
            for r in web_search(query, limit=5):
                raw_count += 1
                if r.get("url") in seen_urls:
                    continue
                if not is_useful(r, query):
                    continue
                seen_urls.add(r.get("url", ""))
                collected.append({
                    "title": r.get("title", "")[:100],
                    "snippet": r.get("snippet", "")[:300],
                    "url": r.get("url", ""),
                })
                if len(collected) >= 3:
                    break
            if len(collected) >= 3:
                break
        base["topics"][topic] = collected

    total = sum(len(v) for v in base["topics"].values())
    base["meta"] = {
        "provider": provider_name(),
        "topics_with_data": sum(1 for v in base["topics"].values() if v),
        "total_kept": total,
    }
    if base["topics"]:
        save_cached(role_key, base)
    return base


def kb_report(role_key: str) -> str:
    kb = ROLE_KB.get(role_key, ROLE_KB["general"])
    lines = [f"岗位：{kb['name']}（{role_key}）", ""]
    for item in kb["topics"]:
        name, angles = item[0], item[1]
        lines.append(f"- {name}")
        for a in angles:
            lines.append(f"    · {a}")

    cached = load_cached(role_key)
    if cached.get("topics"):
        lines.append("")
        lines.append("已缓存的联网资料：")
        for topic, items in cached["topics"].items():
            n = len(items) if isinstance(items, list) else 0
            lines.append(f"- {topic}（{n} 条）")
            for it in (items or [])[:1]:
                if isinstance(it, dict):
                    lines.append(f"    · {it.get('title', '')[:60]}")
    return "\n".join(lines)
