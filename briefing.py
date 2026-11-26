"""阶段性简报（briefing）：把"已经问到哪"压成结构化几行，供报告与后续回合使用。

抄的是 real-mock 的 step compaction 思路：对话不能无限往上下文里堆，
但也不能直接丢掉——压成 `verified / suspicious / weak_points / pending` 四类，
既省 token，又保住"我知道什么、我还差什么"。

和它们的区别（也是我们的优势）：
  它们是**文本压缩**（把原文交给模型再总结一遍，会引入新的编造风险）；
  我们是**结构化聚合**——数据全部来自已有的探针树与事实账本，
  不新增一次模型调用，因此不可能凭空造出"已验证事实"。

四类字段的来源：
  covered/evaded   探针树（谁被答到、谁被回避）
  verified         事实账本里带数值+单位、未被覆盖的事实
  suspicious       事实账本里检测到的前后矛盾
  weak_points      评分里最弱维度与失败回合
  pending          还没关掉的要素 + 还没问的技术话题
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Briefing:
    covered: list[str] = field(default_factory=list)
    evaded: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    suspicious: list[str] = field(default_factory=list)
    weak_points: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    phase: str = ""
    turns: int = 0

    def to_dict(self) -> dict:
        return {
            "phase": self.phase, "turns": self.turns,
            "covered": self.covered, "evaded": self.evaded,
            "verified": self.verified, "suspicious": self.suspicious,
            "weak_points": self.weak_points, "pending": self.pending,
        }

    def render(self) -> str:
        """给报告/提示词用的紧凑文本（空段不占位）。"""
        rows = [
            ("已答到", self.covered),
            ("被回避", self.evaded),
            ("已确认事实", self.verified),
            ("可疑/矛盾", self.suspicious),
            ("薄弱点", self.weak_points),
            ("待补", self.pending),
        ]
        lines = [f"【阶段简报】{self.phase} · 已进行 {self.turns} 轮"]
        for name, items in rows:
            if items:
                lines.append(f"  {name}：" + "；".join(items[:6]))
        return "\n".join(lines)


def build(session) -> Briefing:
    """从会话现有状态聚合一份简报。不做模型调用。"""
    sm = session.sm
    tree = getattr(sm.probe, "tree", None)
    b = Briefing(phase=getattr(sm, "phase", ""), turns=len(getattr(session, "turns", [])))

    # 1) 探针树：已答到 / 被回避
    if tree is not None:
        # 注意：PROJECT_PROBE 之外的阶段 probe.current 是空的，
        # 这时用第一个项目去取覆盖情况，否则简报会显示"全部未问到"（假空）
        target = sm.probe.current or (sm.probe.projects[0] if sm.probe.projects else "")
        try:
            report = tree.coverage(target) if target else {}
        except Exception:
            report = {}
        for element, text in (report or {}).items():
            if text.startswith("已答到"):
                b.covered.append(f"{element}({text})")
            elif text.startswith("回避") or text.startswith("未答出"):
                b.evaded.append(f"{element}({text})")
        b.pending = [e for e in tree.elements_of(target)
                     if not tree.element_closed(target, e)] if target else []

    # 2) 事实账本：已确认 / 矛盾
    mem = getattr(session, "memory", None)
    if mem is not None:
        for f in (mem.active_facts() or [])[:8]:
            unit = getattr(f, "unit", "") or ""
            if getattr(f, "value", None) is not None:
                b.verified.append(f"{f.project or '当前项目'}·{f.key}={f.value:g}{unit}")
        try:
            c = mem.pending_contradiction()
        except Exception:
            c = None
        if c is not None:
            b.suspicious.append(
                f"{getattr(c, 'key', '')}：{getattr(c, 'old_value', '')} vs {getattr(c, 'new_value', '')}")

    # 3) 薄弱点：最弱维度 + 失败回合
    fails = [t for t in getattr(session, "turns", [])
             if getattr(getattr(t, "evaluation", None), "verdict", "") == "fail"]
    for t in fails[-3:]:
        ev = t.evaluation
        b.weak_points.append(f"第{t.idx + 1}轮 {getattr(ev, 'weakest', '')} 最弱")
    dims = getattr(session, "_last_dims", None) or {}
    if dims:
        worst = min(dims, key=dims.get)
        b.weak_points.append(f"整体最弱维度：{worst}={dims[worst]}")

    # 4) 待补：还没问的技术话题
    tech = getattr(sm, "tech", None)
    if tech is not None:
        left = [t for t in (getattr(tech, "topics", []) or [])
                if t not in (getattr(tech, "asked", set()) or set())]
        b.pending.extend([f"话题:{x}" for x in left[:4]])
    return b
