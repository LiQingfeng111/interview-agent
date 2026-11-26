"""联网扩题：从真实面经页面抽取面试题，补进题库。

调查报告（这是"没有联网"这条批评的实际结论）：
  - 免 Key 的 Bing 抓取，对**普通中文查询几乎不可用**：
    "多 Agent 编排 面试题" 返回的是《汉典》"多"字释义，完全不相关。
  - DuckDuckGo HTML / 公开 SearXNG 实例在当前网络下直接 URLError / SSL EOF，走不通。
  - **但 `site:` 限定查询可用**：`site:zhihu.com RAG 面试题` 立刻返回
    「2026年RAG大厂面试题汇总」「30 道 RAG 面试题与答案」这类真面经。
  - 面经页面本身可以直接 urllib 抓（实测卡码笔记 49KB、149 个问号可抽取），
    知乎/DataCamp 有反爬会失败——所以要多站点轮询、失败就跳过。

所以扩题链路是：site: 搜索 → 抓页面正文 → 抽问句 → 与现有题库去重 → 落库候选题。
候选题标 source=web / reviewed=false：**必须有人的那一道工序**，
机器抓来的句子当题库用等于把噪声当能力。

用法：
  python tools/enrich_bank.py --role all --per-topic 6
  python tools/enrich_bank.py --role agent_app --dry     # 只看抓到什么
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bank import (  # noqa: E402
    dedupe,
    load_bank,
    render_bank_report,
    save_bank,
    validate_bank,
)
from config import ROLE_KB  # noqa: E402
from knowledge import role_topics  # noqa: E402
from search import provider_name, search_bing  # noqa: E402
from llm import build_llm  # noqa: E402

# 实测有效的站点限定：面经集中、且在部分情况下可直接抓取
SITE_QUERIES = [
    "site:zhihu.com {topic} 面试题",
    "site:kamacoder.com {topic} 面试题",
    "site:csdn.net {topic} 面试题",
    "site:juejin.cn {topic} 面试题",
    "site:cnblogs.com {topic} 面试",
    "site:javaguide.cn {topic}",
    "site:segmentfault.com {topic} 面试",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
SKIP_MARKERS = ("版权", "转载", "关注", "点赞", "登录", "注册", "下载", "扫码", "公众号")
QUESTION_STARTS = ("如何", "为什么", "怎么", "什么", "哪些", "是否", "怎样", "何时", "哪个", "能否", "简述")


def fetch_text(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    text = raw.decode("utf-8", "ignore")
    # 去脚本/样式再剥标签，否则会抽出一堆 JS 变量
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    return text


def extract_questions(text: str, topic: str, limit: int = 8) -> list[str]:
    """从页面正文里抽"像面试题"的句子。

    判据（宁可漏，不要乱收）：
      - 长度 10~120：短了没信息，长了是段落；
      - 含问号，或以疑问词开头（很多面经把问号写成「？」或「?」）；
      - 命中话题词，或含通用技术词（否则会抽到"你叫什么名字"这类无关句）。
    去重按文本规范化后比对。
    """
    out: list[str] = []
    seen: set[str] = set()
    key = re.sub(r"\s+", "", topic)
    for line in re.split(r"[\n。；;]", text or ""):
        s = re.sub(r"\s+", " ", line).strip()
        s = re.sub(r"^[\d\.\)、\-\*\s]+", "", s)          # 去掉编号
        # 下限 6 而不是 10：「RAG 是什么？」这类短问题本身就很典型，
        # 用 10 会把真问题筛掉（实测就是这样丢了 3 条正常题）。
        if not (6 <= len(s) <= 140):
            continue
        if any(b in s for b in SKIP_MARKERS):
            continue
        has_q = ("？" in s) or ("?" in s)
        starts_q = any(s.startswith(w) for w in QUESTION_STARTS)
        if not (has_q or starts_q):
            continue
        if key and key not in s.replace(" ", "") and not any(
            w in s for w in ("模型", "检索", "评测", "索引", "缓存", "Agent", "RAG", "并发")
        ):
            continue
        norm = re.sub(r"[^\w\u4e00-\u9fff]", "", s)[:60]
        if norm in seen:
            continue
        seen.add(norm)
        out.append(s.rstrip("？?"))
        if len(out) >= limit:
            break
    return out


FILTER_SYSTEM = """你是面试题库编辑。给你一批从网页里抓来的句子，只保留**真正能在技术面试里问出来**的题目。
判据：
- 必须是关于给定话题的技术问题（不是产品广告、新闻标题、价格、续航、游戏评测）；
- 必须落在给定岗位的语境里：面的是「{role}」岗，就不要收操作系统/CPU 硬件底层题；
- 必须是一个完整可回答的问题，不是半句、不是标题拼接；
- 题目要能暴露真实能力缺口（概念、实现、参数、失效、权衡）。
输出 JSON：{"keep":[{"i":序号,"q":"改写后的干净问题（去掉口语铺垫和网页残留）"}],"drop_reason":"一句话"}
没有合格的就返回 {"keep":[]}。不要解释，不要 markdown。"""


def llm_filter(llm, topic: str, candidates: list[str], role: str = "") -> list[str]:
    """用模型当编辑，把网页噪声挡在题库外面。

    为什么必须有这一步：实测直接抽取会捞到「石头扫地机器人 P10 续航多久」
    「从144帧手游到3A 红魔评测」这种句子——机械规则分不清"技术问题"和"广告"。
    模型分得清，代价是每个话题一次调用。
    """
    if not candidates:
        return []
    listing = "\n".join(f"{i}. {c[:120]}" for i, c in enumerate(candidates))
    role_name = ROLE_KB.get(role, {}).get("name", role or "技术")
    user = (
        f"岗位：{role_name}\n话题：{topic}\n候选句子：\n{listing}\n\n"
        "只保留真正的面试题，输出 JSON。"
    )
    try:
        data = llm.chat_json(
            # 注意：模板里有字面量 {"keep":[...]}，绝对不能用 str.format（会把花括号当占位符，
            # 抛 KeyError 然后被 except 吞掉 —— 实测整个扩题跑出 0 条就是这么来的）
            [{"role": "system", "content": FILTER_SYSTEM.replace("{role}", role_name)},
             {"role": "user", "content": user}],
            task="bank_filter",
        )
    except Exception:
        return []
    out = []
    for it in (data.get("keep") or []):
        q = str(it.get("q", "")).strip()
        if q:
            out.append(q)
    return out


def enrich_topic(role: str, topic: str, per_topic: int = 6, pages: int = 2,
                 llm=None) -> list[dict]:
    got: list[dict] = []
    tried_urls: set[str] = set()
    for tpl in SITE_QUERIES:
        if len(got) >= per_topic:
            break
        query = tpl.format(topic=topic)
        results = search_bing(query, limit=4)
        for r in results:
            url = r.get("url") or ""
            if not url or url in tried_urls:
                continue
            tried_urls.add(url)
            try:
                text = fetch_text(url)
            except Exception:
                continue
            for q in extract_questions(text, topic, limit=per_topic - len(got)):
                got.append({
                    "id": f"{role}-web-{abs(hash(q)) % 1000000:06d}",
                    "role": role,
                    "topic": topic,
                    "level": "advanced",
                    "question": q,
                    "points": [],
                    "tags": ["web", "needs_points"],
                    "source": "web",
                    "reviewed": False,
                    "url": url,
                    "updated_at": time.strftime("%Y-%m-%d"),
                })
            if len(got) >= per_topic:
                break
            time.sleep(0.6)

    if llm is not None and got:
        kept = llm_filter(llm, topic, [g["question"] for g in got], role=role)
        by_q = {g["question"]: g for g in got}
        filtered = []
        for q in kept:
            base = by_q.get(q) or next(
                (g for g in got if abs(len(g["question"]) - len(q)) < 30), None
            )
            item = dict(base or got[0])
            item["question"] = q
            item["id"] = f"{role}-web-{abs(hash(q)) % 1000000:06d}"
            item["tags"] = ["web", "llm_filtered", "needs_points"]
            filtered.append(item)
        return filtered
    return got


POINTS_SYSTEM = """你是面试题库编辑。给每道题补 2-3 条「答到才算过」的要点。
要点必须具体（关键词、数字方向、常见错误），不要写"理解深刻"这种空话。
输出 JSON：{"items":[{"id":"题号","points":["...","..."]}]}，不要解释，不要 markdown。"""


def fill_points(llm, items: list[dict], batch: int = 3) -> int:
    """给没有 points 的题补要点。

    为什么必须补：没有要点时评分官只能凭"感觉"打分，
    同一份回答两次跑出两个分，题库就失去了可比性。
    """
    todo = [q for q in items if not q.get("points")]
    filled = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        listing = "\n".join(
            f'- id={q["id"]}｜{q["topic"]}｜{q["question"][:100]}' for q in chunk
        )
        try:
            data = llm.chat_json(
                [{"role": "system", "content": POINTS_SYSTEM},
                 {"role": "user", "content": listing + "\n\n给这些题补要点，输出 JSON。"}],
                task="bank_points",
            )
        except Exception as e:
            print(f"      [warn] 补要点失败：{e}")
            continue
        by_id = {q["id"]: q for q in chunk}
        for it in (data.get("items") or []):
            q = by_id.get(str(it.get("id", "")))
            pts = [str(p).strip() for p in (it.get("points") or []) if str(p).strip()][:3]
            if q and pts:
                q["points"] = pts
                q.setdefault("tags", []).append("points_filled")
                filled += 1
    return filled


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="agent_app", help="岗位名，或 all")
    ap.add_argument("--per-topic", type=int, default=6)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--no-filter", action="store_true", help="不做 LLM 过滤（噪声会进去）")
    ap.add_argument("--fill-points", action="store_true", help="只给缺要点的题补 points")
    args = ap.parse_args()

    if args.fill_points:
        llm = build_llm(mode="auto")
        if getattr(llm, "cfg", None) is not None:
            llm.cfg.max_tokens = 3000
        items = load_bank()
        n = fill_points(llm, items)
        save_bank(items)
        print(f"[enrich] 补了 {n} 道题的要点")
        print(render_bank_report(load_bank()))
        return 0

    roles = list(ROLE_KB) if args.role == "all" else [args.role]
    print(f"[enrich] 搜索源：{provider_name()}｜站点限定：{len(SITE_QUERIES)} 种")
    llm = None if args.no_filter else build_llm(mode="auto")
    if llm is not None:
        print("[enrich] 已启用 LLM 过滤（挡掉广告/新闻类噪声）")

    added: list[dict] = []
    for role in roles:
        print(f"[enrich] {role}")
        for topic in role_topics(role):
            got = enrich_topic(role, topic, args.per_topic, llm=llm)
            print(f"      {topic:<14} {len(got)} 条")
            added.extend(got)

    existing = load_bank()
    merged, dropped = dedupe(existing + added)
    print(f"\n[enrich] 抓取 {len(added)} 条，去重丢弃 {dropped} 条（含与已有题库重复）")

    if args.dry:
        for q in added[:10]:
            print(f"  - [{q['topic']}] {q['question'][:60]}")
        print("（--dry 未写库）")
        return 0

    errors, _ = validate_bank(merged)
    save_bank(merged)
    print(render_bank_report(merged))
    print("[enrich] 已写入题库。候选题（source=web, reviewed=false）需要人工改写并补 points。")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())




