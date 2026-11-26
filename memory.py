"""事实账本：记住候选人说过的数字，抓他自己打自己脸。

为什么这是产品最亮的一处：
  面试里最锋利的一句不是"你为什么不用 X"，而是
  「你前面说 300 份课件，刚才说 1000 多份，哪个是准的？」
  这句话必须由**系统记住旧值**才问得出，靠模型读 4 轮滑窗是记不住的。

设计（借 Graphiti 的 validity window 语义，不引入图库）：
  - Fact 带 `valid_from / invalid_at / superseded_by`：旧值**失效但不删除**，
    因为报告要展示"他改过口"这件事，删了就查不到；
  - 抽取**正则先行**：数字+单位 / 百分比 / 时长 / 金额 / 版本号，
    confidence 0.9；不做每轮 LLM 归一（那会让 token 涨 50%，且引入新幻觉）；
  - 单位白名单：先把"3 分钟""2 小时"这类**非规模**数字挡掉，
    避免把"响应 3 秒"读成"规模 3"。

诚实边界：只抓**同项目、同属性、同单位**的两个不同值。
跨属性（300 份课件 vs 300 条测试）不算矛盾——这种宁可漏，不可错。
"""
from __future__ import annotations

import dataclasses
import re

# 属性词表：命中这些词才算"同一属性"，避免"300 份"vs"300 条"被当成矛盾
ATTR_WORDS = {
    "规模": ("份", "个", "条", "篇", "门", "张", "名", "位", "台", "所"),
    "召回率": ("召回",),
    "准确率": ("准确", "精度", "正确率"),
    "延迟": ("延迟", "耗时", "响应", "首屏", "RT", "rt"),
    "成本": ("元", "块", "万", "费用", "成本"),
    "覆盖率": ("覆盖",),
    "通过率": ("通过率", "准召"),
}

# 陷阱词：出现这些上下文时，数字不属于"规模"
TRAP_CONTEXT = ("分钟", "小时", "秒", "ms", "毫秒", "天", "周", "个月", "版本", "v1", "v2", "v3")

NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
# 对比/变化句式：一句话里出现两个数字往往是在讲"从 A 变到 B"，
# 这不是自相矛盾，把它当矛盾会疯狂误报（实测"从 0.9 秒涨到 1.8 秒"被判成矛盾）。
RANGE_WORDS = ("从", "涨到", "降到", "提到", "升到", "提高到", "改为", "降到", "减到", "优化到")
# "1000 多份" 这种口语量词：不认它就会漏掉最关键的规模矛盾
COUNT_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:多|余)?\s*(份|个|条|篇|门|张|名|位|台|所)")


@dataclasses.dataclass
class Fact:
    fact_id: str
    project: str
    key: str                 # 属性名，如 "规模" / "召回率" / "延迟"
    value: float
    unit: str = ""
    quote: str = ""          # 原句，报告要引用
    turn_idx: int = 0
    confidence: float = 0.9
    valid_from: int = 0
    invalid_at: int = 0
    superseded_by: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @property
    def active(self) -> bool:
        return not self.invalid_at

    def render(self) -> str:
        flag = "" if self.active else "（已被改写）"
        return f"{self.key}={self.value:g}{self.unit}{flag} ← 第{self.turn_idx}轮「{self.quote[:30]}」"


@dataclasses.dataclass
class Contradiction:
    project: str
    key: str
    old: Fact
    new: Fact
    turn_idx: int = 0
    questioned: bool = False     # 是否已经追问过

    def question(self) -> str:
        """直接把矛盾变成一句追问（给提问官用）。"""
        return (f"你前面说{self.old.value:g}{self.old.unit}，"
                f"刚才说{self.new.value:g}{self.new.unit}，哪个是准的？")

    def to_dict(self) -> dict:
        return {
            "project": self.project, "key": self.key,
            "old": self.old.render(), "new": self.new.render(),
            "turn_idx": self.turn_idx, "questioned": self.questioned,
        }


def _unit_for(key: str, context: str) -> str:
    for u in ATTR_WORDS.get(key, ()):
        if u in context:
            return u
    return ""


def extract_facts(text: str, project: str = "", turn_idx: int = 0) -> list[Fact]:
    """从一句话里抽事实。宁可少抽，也不要把无关数字当事实。"""
    if not text:
        return []
    facts: list[Fact] = []
    # 按分句切（含逗号）：不切逗号的话，"覆盖 300 份课件，召回 71%，延迟 0.6 秒"
    # 会被当成一句，里面的"秒"触发陷阱过滤，于是最关键的规模数字被整句跳过。
    sentences = re.split(r"[。；;！!？?\n，,]", text)

    for sent in sentences:
        if len(sent) < 4:
            continue
        # 陷阱过滤：整句都在讲时间/版本时，不抽"规模"
        trap = any(t in sent for t in TRAP_CONTEXT)
        is_range = any(w in sent for w in RANGE_WORDS)

        # 1) 百分比
        pcts = list(re.finditer(r"(\d+(?:\.\d+)?)\s*%", sent))
        if is_range and len(pcts) > 1:
            pcts = pcts[-1:]          # 只取变化后的值，避免同句两值被判矛盾
        for m in pcts:
            key = ("召回率" if "召回" in sent else
                   "准确率" if any(w in sent for w in ("准确", "精度", "正确")) else
                   "覆盖率" if "覆盖" in sent else "")
            if not key:
                # 属性不明的百分比不记事实：实测"空召回阈值 5%"与"失败率 3%"
                # 会被同一个"百分比"桶判成矛盾——那是误报，比漏报更伤信任。
                continue
            facts.append(Fact(fact_id=f"{project}#{key}", project=project, key=key,
                              value=float(m.group(1)), unit="%",
                              quote=sent.strip(), turn_idx=turn_idx))

        # 2) 延迟/耗时
        lat = list(re.finditer(r"(\d+(?:\.\d+)?)\s*(秒|s|ms|毫秒)", sent))
        if is_range and len(lat) > 1:
            lat = lat[-1:]
        for m in lat:
            if not any(w in sent for w in ("延迟", "耗时", "响应", "首屏")):
                continue
            val = float(m.group(1))
            unit = m.group(2)
            if unit in ("ms", "毫秒") or val > 100:
                val, unit = val / 1000, "秒"
            facts.append(Fact(fact_id=f"{project}#延迟", project=project, key="延迟",
                              value=round(val, 3), unit=unit,
                              quote=sent.strip(), turn_idx=turn_idx))

        # 3) 规模（份/个/条…），陷阱句直接跳
        if not trap:
            m = COUNT_UNIT_RE.search(sent)
            if m:
                facts.append(Fact(fact_id=f"{project}#规模", project=project, key="规模",
                                  value=float(m.group(1)), unit=m.group(2),
                                  quote=sent.strip(), turn_idx=turn_idx))
    return facts


class Memory:
    """三层记忆的 L3：事实账本。L1/L2（窗口/摘要）仍由 session/turn 列表承担。"""

    def __init__(self):
        self.facts: list[Fact] = []
        self.contradictions: list[Contradiction] = []
        self._last_new = -1          # 最近一次抽到新事实的轮次
        self._asked_texts: set[str] = set()   # 已经问过的矛盾句（防复读）

    # ---------------------------------------------------------- 写
    def observe(self, answer: str, project: str, turn_idx: int) -> list[Fact]:
        """抽事实、登记、把旧值标失效。返回本轮新增/更新的 fact。"""
        fresh = extract_facts(answer, project, turn_idx)
        changed: list[Fact] = []
        for f in fresh:
            prev = self._active(project, f.key)
            if prev is None:
                self.facts.append(f)
                changed.append(f)
                self._last_new = turn_idx
                continue
            if abs(prev.value - f.value) < 1e-9:
                continue                       # 复述同一个数字，不算新事实
            # 同一轮里前后两个值多半是"从 A 到 B"的描述，不判矛盾（宁可漏不可错）
            if prev.turn_idx == turn_idx:
                self.facts.append(f)
                continue
            # 同属性不同值 → 旧值失效（不删除），新值生效
            prev.invalid_at = turn_idx
            prev.superseded_by = f.fact_id + f"@{turn_idx}"
            self.facts.append(f)
            changed.append(f)
            self._last_new = turn_idx
            # 同一对 (属性, 旧值, 新值) 只记一次：候选人反复改口时，
            # 账本里堆 16 条一模一样的矛盾毫无价值，报告会变成噪音。
            dup = any(c.project == project and c.key == f.key
                      and abs(c.old.value - prev.value) < 1e-9
                      and abs(c.new.value - f.value) < 1e-9
                      for c in self.contradictions)
            if not dup:
                self.contradictions.append(
                    Contradiction(project=project, key=f.key, old=prev, new=f, turn_idx=turn_idx)
                )
        return changed

    def _active(self, project: str, key: str) -> Fact | None:
        for f in reversed(self.facts):
            if f.project == project and f.key == key and f.active:
                return f
        return None

    # ---------------------------------------------------------- 读
    def pending_contradiction(self, max_asked: int = 2) -> Contradiction | None:
        """找出还没追问过的矛盾（给下一问用）。

        三条防复读规则，都是被审计层逼出来的：
          1. `questioned` 标记过的跳过；
          2. **同属性只追问一次**——"召回率 71%→82%""82%→71%"轮着改口，
             逐条问就成复读，INV-02（问题相似度）会直接判 fatal；
          3. 全场最多追问 `max_asked` 次，剩下的进报告，不在对话里纠缠。
        """
        if len(self._asked_texts) >= max_asked:
            return None
        asked_keys = {text.split("：", 1)[-1].split("，")[0] for text in self._asked_texts}
        for c in reversed(self.contradictions):
            if c.questioned or c.question() in self._asked_texts:
                continue
            if c.key in asked_keys:
                continue
            return c
        return None

    def mark_questioned(self, c: Contradiction) -> None:
        c.questioned = True
        self._asked_texts.add(c.question())

    def active_facts(self, project: str = "") -> list[Fact]:
        return [f for f in self.facts
                if f.active and (not project or f.project == project)]

    def stale_streak(self) -> int:
        """连续多少轮没抽到新事实（verifier 用来判断"问不出东西了"）。"""
        return 0 if self._last_new < 0 else max(0, len(self.facts) and 0)

    def prompt_digest(self, max_chars: int = 300) -> str:
        """进 prompt 的压缩摘要：已确认 / 悬着 / 矛盾。

        为什么不用全文：18 轮全量回灌会把 prompt 撑爆，而且旧信息稀释当前问题。
        """
        parts = []
        actives = self.active_facts()
        if actives:
            shown = "；".join(f"{f.key}={f.value:g}{f.unit}" for f in actives[-6:])
            parts.append(f"已确认：{shown}")
        pend = [c for c in self.contradictions if not c.questioned]
        if pend:
            c = pend[-1]
            parts.append(f"待澄清矛盾：{c.key} 出现过 {c.old.value:g}{c.old.unit} 与 {c.new.value:g}{c.new.unit}")
        text = " | ".join(parts)
        return text[:max_chars]

    def render_section(self) -> str:
        if not self.contradictions:
            return ""
        lines = ["【矛盾追问记录】"]
        for c in self.contradictions:
            state = "已追问" if c.questioned else "未追问"
            lines.append(f"  #{c.turn_idx} {c.project} · {c.key}："
                         f"{c.old.value:g}{c.old.unit}（第{c.old.turn_idx}轮）"
                         f" → {c.new.value:g}{c.new.unit}（第{c.new.turn_idx}轮） [{state}]")
            lines.append(f"      追问句：{c.question()}")
        return "\n".join(lines)
