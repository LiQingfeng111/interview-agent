"""双轨评分：逻辑分 + 模型分，按维度分工。

## 为什么需要两套分

单一评分源都有硬伤：

| 只靠模型 | 只靠规则 |
|---|---|
| 分数会漂移（同答案两次可能 2.5 / 3.0） | 判断不了"讲得深不深" |
| 容易被话术骗（说得漂亮就给分） | 判断不了"取舍讲清楚了吗" |
| 换个模型分数就变 | 判断不了"表达清不清楚" |
| 成本高（每轮一次调用） | 可以零成本、确定性 |

## 分工原则

**规则能算准的，不交给模型。** 具体：

| 维度 | 谁负责 | 为什么 |
|---|---|---|
| `accuracy`（准确度） | **规则为主** | 能检测：有没有数字、有没有指标名、有没有与简历矛盾 |
| `depth`（深度） | **模型为主** | 抽象判断——讲的是表面还是根因 |
| `tradeoff`（取舍） | **模型为主** | 是否说清"为什么选 A 不选 B"、代价是什么 |
| `boundary`（边界） | **规则 + 模型** | 规则查"什么情况下不行/失效"，模型判断是否讲透 |
| `clarity`（表达） | **规则为主** | 能算：句子长度、有没有结构词（因为/所以/首先） |

`avg` = 五维平均 → 于是 `avg` 本身就是两套分数融合的结果，
不需要再人为加权。

## 关键约束：规则分不能"盖过"模型分

规则分只在**能给出证据**时才生效。举个反例：
候选人说"我们做了优化，效果不错"——没有数字，规则会给 accuracy 低分，
但这可能是真话（项目保密）。所以规则分最低给 1.5，不能直接判 1。

这样规则负责"别被空话骗"，模型负责"别把有料的话判死"。
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("interview.rubric")

#: 规则分的最低值。低于这个值会让"真的没数据但讲得对"的答案被误杀。
RULE_FLOOR = 1.5

#: 数字/指标的正则。用于 accuracy 判断。
_METRIC_PATTERNS = [
    r"\d+(?:\.\d+)?%",                      # 71%、82.5%
    r"\d+(?:\.\d+)?\s*(?:ms|毫秒|秒|s)\b",   # 1.8 秒、200ms
    r"\d+(?:\.\d+)?\s*(?:倍|x)\b",           # 3 倍
    r"\d+(?:\.\d+)?\s*(?:万|千|k|K|M|GB|MB|TB)\b",
    r"(?:提升|下降|降低|提高|增加|减少)了?\s*\d+",
    r"\b(?:p99|P99|qps|QPS|latency|recall|precision|f1|F1|auc|AUC|kappa)\b",
]

#: 对照/基线词。有基线说明不是"自说自话"。
_BASELINE_WORDS = [
    "基线", "对照", "相比", "对比", "原来", "之前", "上一版",
    "baseline", "对照组", "改前", "改后",
]

#: 取舍词。说清"代价"是深度的重要信号。
_TRADEOFF_WORDS = [
    "代价", "成本", "权衡", "取舍", "牺牲", "换来", "但是", "不过",
    "缺点是", "副作用", "trade-off", "tradeoff",
]

#: 边界词。说清"什么情况下不行"。
_BOUNDARY_WORDS = [
    "只在", "仅当", "不适用", "失效", "边界", "限制", "做不到",
    "如果数据量", "当.*时不行", "无法", "不支持", "例外",
]

#: 结构词。表达清晰的信号。
_STRUCTURE_WORDS = [
    "首先", "其次", "然后", "最后", "第一", "第二", "第三",
    "因为", "所以", "因此", "于是", "结果", "总结",
]

#: 空话/套话。这些词密度高但信息量低，accuracy 要扣。
_EMPTY_PHRASES = [
    "效果不错", "挺好的", "优化了一下", "改进了很多", "提升明显",
    "差不多", "大概吧", "应该是", "可能是", "反正",
]


def count_metrics(text: str) -> int:
    """数回答里出现了几处具体数字/指标。"""
    if not text:
        return 0
    hits = 0
    for pat in _METRIC_PATTERNS:
        hits += len(re.findall(pat, text, flags=re.IGNORECASE))
    return hits


def _hits(text: str, words: list[str]) -> int:
    """数命中了几组词（每组算 1 次，避免同义词堆分）。"""
    n = 0
    for w in words:
        if re.search(w, text, flags=re.IGNORECASE):
            n += 1
    return n


def rule_scores(answer: str, *, resume_context: str = "") -> dict[str, Any]:
    """算规则能确定的那部分分。

    Args:
        answer: 候选人回答
        resume_context: 简历片段（用于一致性核对）

    Returns:
        {
          "accuracy": 分数,
          "boundary": 分数,
          "clarity": 分数,
          "signals": {...}      # 给日志/报告看的证据
        }
    """
    out: dict[str, Any] = {}
    text = (answer or "").strip()
    if not text:
        return {
            "accuracy": RULE_FLOOR, "boundary": RULE_FLOOR, "clarity": RULE_FLOOR,
            "signals": {"empty": True},
        }

    # ---------------------------------------------------------------- accuracy
    #
    # 三个信号：
    #   ① 有具体数字/指标  → 这是最硬的"不是空话"证据
    #   ② 有基线对照      → 说明不是自说自话
    #   ③ 空话密度        → 扣分
    n_metric = count_metrics(text)
    n_base = _hits(text, _BASELINE_WORDS)
    n_empty = _hits(text, _EMPTY_PHRASES)
    length = len(text)

    acc = 2.0
    if n_metric >= 3:
        acc = 4.5
    elif n_metric == 2:
        acc = 4.0
    elif n_metric == 1:
        acc = 3.2
    else:
        # 没有数字：可能是保密，也可能是空话。给 2.0 起，篇幅够长加一点
        acc = 2.5 if length > 120 else 2.0

    if n_base >= 1:
        acc += 0.4
    if n_empty >= 2:
        acc -= 0.8
    elif n_empty == 1:
        acc -= 0.3

    # 与简历矛盾检测：回答里的百分比在简历里找不到对应
    if resume_context and n_metric:
        pcts = re.findall(r"\d+(?:\.\d+)?%", text)
        resume_pcts = set(re.findall(r"\d+(?:\.\d+)?%", resume_context))
        if pcts and resume_pcts:
            novel = [p for p in pcts if p not in resume_pcts]
            # 全部数字都不在简历里 → 可能是编的（也可能简历没写全）
            if len(novel) == len(pcts):
                acc -= 0.5

    out["accuracy"] = round(max(RULE_FLOOR, min(5.0, acc)), 1)

    # ---------------------------------------------------------------- boundary
    #
    # 规则只能判断"有没有提边界"，提得透不透要模型。
    # 所以规则给一个温和的底分，模型分数会主导。
    n_bound = _hits(text, _BOUNDARY_WORDS)
    bnd = 2.0 + min(2.0, n_bound * 0.9)
    out["boundary"] = round(max(RULE_FLOOR, min(5.0, bnd)), 1)

    # ---------------------------------------------------------------- clarity
    #
    # 能算的：结构词、句长、有没有跑题式的超长堆砌
    n_struct = _hits(text, _STRUCTURE_WORDS)
    sentences = [s for s in re.split(r"[。！？.!?\n]", text) if s.strip()]
    avg_len = (sum(len(s) for s in sentences) / len(sentences)) if sentences else 0

    clr = 2.5
    clr += min(1.5, n_struct * 0.5)
    # 句子过长（>80 字）说明没断句，可读性差
    if avg_len > 80:
        clr -= 0.6
    # 太短（<40 字）信息量不足
    if length < 40:
        clr -= 0.5
    # 结构化列举（有"第一/首先"等且篇幅够）加分
    if n_struct >= 2 and length > 100:
        clr += 0.3

    out["clarity"] = round(max(RULE_FLOOR, min(5.0, clr)), 1)

    out["signals"] = {
        "metrics": n_metric,
        "baseline": n_base,
        "boundary": n_bound,
        "structure": n_struct,
        "empty_phrases": n_empty,
        "length": length,
        "avg_sentence_len": round(avg_len, 1),
    }
    return out


def merge(llm_eval: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    """把模型分和规则分合成最终五维分。

    ## 合并策略（每个维度不一样）

    | 维度 | 合并方式 |
    |---|---|
    | accuracy | 模型 : 规则 = 4 : 6（规则更可靠——数字在不在是客观的） |
    | depth | 纯模型（规则算不了） |
    | tradeoff | 纯模型（规则算不了） |
    | boundary | 模型 : 规则 = 6 : 4（"提没提"规则准，"透不透"模型准） |
    | clarity | 模型 : 规则 = 3 : 7（结构词、句长是可量化的） |

    ## 为什么不是简单平均

    简单平均会让弱信号拖累强信号。比如 accuracy：
    模型因为"讲得流畅"给 4 分，但回答里**一个数字都没有**——
    规则给 2 分。如果平均得到 3 分，等于认可了"没数据也算准确"。
    4:6 会得到 2.8，更接近"缺数据"的真实情况。

    Returns:
        合并后的 dict，含五维 + `score_source` 说明每维来自谁
    """
    WEIGHTS = {
        # 维度: (模型权重, 规则权重)
        "accuracy": (0.4, 0.6),
        "boundary": (0.6, 0.4),
        "clarity": (0.3, 0.7),
    }

    merged: dict[str, Any] = {}
    source: dict[str, str] = {}

    for dim in ("accuracy", "depth", "tradeoff", "boundary", "clarity"):
        m_val = llm_eval.get(dim)
        r_val = rule.get(dim)

        has_m = isinstance(m_val, (int, float))
        has_r = isinstance(r_val, (int, float))

        if dim in WEIGHTS and has_m and has_r:
            wm, wr = WEIGHTS[dim]
            val = float(m_val) * wm + float(r_val) * wr
            source[dim] = f"model×{wm}+rule×{wr}"
        elif has_m:
            val = float(m_val)
            source[dim] = "model"
        elif has_r:
            val = float(r_val)
            source[dim] = "rule"
        else:
            val = 2.5
            source[dim] = "default"

        merged[dim] = round(max(1.0, min(5.0, val)), 1)

    merged["score_source"] = source
    merged["rule_signals"] = rule.get("signals") or {}
    return merged


def apply(llm_eval: dict[str, Any], answer: str,
          *, resume_context: str = "") -> dict[str, Any]:
    """一步到位：算规则分 + 合并，返回可直接写库的 dict。

    调用方拿到后只需覆盖原 eval 里的五维字段，
    `verdict` 仍由阈值决定（阈值是第三方裁决，不受这次合并影响）。
    """
    rule = rule_scores(answer, resume_context=resume_context)
    merged = merge(llm_eval, rule)

    out = dict(llm_eval)
    for dim in ("accuracy", "depth", "tradeoff", "boundary", "clarity"):
        out[dim] = merged[dim]
    out["score_source"] = merged["score_source"]
    out["rule_signals"] = merged["rule_signals"]

    # avg 重算（原来是模型五维的平均，现在维度值变了）
    dims = [out[d] for d in ("accuracy", "depth", "tradeoff", "boundary", "clarity")]
    out["avg"] = round(sum(dims) / len(dims), 2)

    log.info(
        "双轨评分 accuracy=%.1f depth=%.1f tradeoff=%.1f boundary=%.1f clarity=%.1f avg=%.2f | 规则信号 %s",
        out["accuracy"], out["depth"], out["tradeoff"], out["boundary"],
        out["clarity"], out["avg"], merged["rule_signals"],
    )
    return out
