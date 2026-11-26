"""深挖树：**主路径状态**。节点=一问，边=从哪一问追下来。

切换记录（提案 S4 → S5）：
  上一轮它是影子（只写不读，parity 与计数器逐项比对，全一致后才允许切）。
  现在它是唯一事实来源——要素的"问了几次、关没关、答到没、打到第几层"
  全部存在这里；`ProbeState` 退化成读视图（保留旧属性名，测试和报告不用改）。

为什么值得切：
  计数器只能回答"问了几次"，回答不了"这一问是从哪一问追下来的、为什么不再问"。
  切成树之后，报告里的每条剪枝都能指到具体节点和关闭原因。

保留的约束（没变）：
  - 同一要素最多追问 MAX_ELEMENT_ATTEMPTS 次（首问 + 追一次），不与候选人死磕；
  - 已关闭的要素不再累加计数（否则"最多两次"会变成假约束，审计层 INV-01 会抓到）。
"""
from __future__ import annotations

import dataclasses

ELEMENTS = ("目标", "baseline", "指标", "失败方案", "个人贡献")
MAX_ELEMENT_ATTEMPTS = 2


@dataclasses.dataclass
class ProbeNode:
    node_id: str
    parent_id: str = ""
    project: str = ""
    ladder_level: int = 0
    element: str = ""
    question: str = ""
    verdict: str = ""
    evidence: str = ""
    status: str = "open"          # open / closed
    close_reason: str = ""
    children: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class ProbeTree:
    """一次会话的追问树 + 要素账本。"""

    MAX_ELEMENT_ATTEMPTS = MAX_ELEMENT_ATTEMPTS

    def __init__(self):
        # ---- 节点（追问链）----
        self.nodes: dict[str, ProbeNode] = {}
        self.order: list[str] = []
        self._last_open: dict[tuple[str, str], str] = {}

        # ---- 要素账本（主路径状态）----
        self.covered: dict[str, set[str]] = {}                    # 项目 → 答到过的要素
        self.attempts: dict[tuple[str, str], int] = {}             # (项目,要素) → 问了几次
        self.status: dict[tuple[str, str], str] = {}               # (项目,要素) → covered/evaded/unknown
        self.levels: dict[str, int] = {}                           # 项目 → 打到过的最深木梯层

    # ---------------------------------------------------------- 要素账本
    def mark_covered(self, project: str, elements: list[str]) -> None:
        if not project or not elements:
            return
        self.covered.setdefault(project, set()).update(elements)

    def elements_of(self, project: str) -> set[str]:
        return set(self.covered.get(project, set()))

    def note_attempt(self, project: str, element: str, status: str = "") -> None:
        if not project or not element:
            return
        key = (project, element)
        if self.element_closed(project, element):
            if status:
                self.status[key] = status
            return
        self.attempts[key] = self.attempts.get(key, 0) + 1
        if status:
            self.status[key] = status
        if status == "covered":
            self.mark_covered(project, [element])

    def attempts_of(self, project: str, element: str) -> int:
        return self.attempts.get((project, element), 0)

    def element_closed(self, project: str, element: str) -> bool:
        key = (project, element)
        if self.status.get(key) == "covered":
            return True
        return self.attempts.get(key, 0) >= self.MAX_ELEMENT_ATTEMPTS

    def level_up(self, project: str, level: int) -> None:
        self.levels[project] = max(self.levels.get(project, 0), level)

    def depth(self, project: str) -> int:
        return self.levels.get(project, 0)

    def coverage(self, project: str) -> dict[str, str]:
        """要素状态报告。报告与不变量都读这一份，口径只有一处。"""
        out: dict[str, str] = {}
        covered = self.covered.get(project, set())
        for element in ELEMENTS:
            key = (project, element)
            status = self.status.get(key, "")
            attempts = self.attempts.get(key, 0)
            if element in covered or status == "covered":
                out[element] = "已答到" if attempts else "已答到（顺带提及）"
            elif attempts == 0:
                out[element] = "未问到"
            elif status == "evaded":
                out[element] = f"回避（问{attempts}次）"
            else:
                out[element] = f"未答出（问{attempts}次）"
        return out

    # ---------------------------------------------------------- 节点
    def add_question(self, project: str, level: int, element: str,
                     question: str) -> str:
        node_id = f"n{len(self.order) + 1}"
        parent = self._last_open.get((project, element), "")
        node = ProbeNode(node_id=node_id, parent_id=parent, project=project,
                         ladder_level=level, element=element, question=question)
        self.nodes[node_id] = node
        self.order.append(node_id)
        if parent and parent in self.nodes:
            self.nodes[parent].children.append(node_id)
        self._last_open[(project, element)] = node_id
        self.level_up(project, level)
        return node_id

    def note_verdict(self, node_id: str, verdict: str, evidence: str = "") -> None:
        node = self.nodes.get(node_id)
        if not node:
            return
        node.verdict = verdict
        node.evidence = evidence or ""

    def close(self, node_id: str, reason: str) -> None:
        node = self.nodes.get(node_id)
        if not node or node.status == "closed":
            return
        node.status = "closed"
        node.close_reason = reason
        key = (node.project, node.element)
        if self._last_open.get(key) == node_id:
            self._last_open.pop(key, None)

    def close_element(self, project: str, element: str, reason: str) -> None:
        for node in self.nodes.values():
            if node.project == project and node.element == element:
                self.close(node.node_id, reason)

    # ---------------------------------------------------------- 渲染
    def render_text(self, project: str = "") -> str:
        lines: list[str] = []
        roots = [nid for nid in self.order
                 if not self.nodes[nid].parent_id
                 and (not project or self.nodes[nid].project == project)]
        for rid in roots:
            self._render(rid, 0, lines)
        return "\n".join(lines) if lines else "（无深挖记录）"

    def _render(self, node_id: str, depth: int, lines: list[str]) -> None:
        n = self.nodes[node_id]
        pad = "  " * depth
        mark = {"pass": "✓", "partial": "~", "fail": "✗"}.get(n.verdict, "·")
        reason = f"（{n.close_reason}）" if n.close_reason else ""
        lines.append(f"{pad}{mark} L{n.ladder_level} [{n.element or '-'}] {n.question[:44]}{reason}")
        for cid in n.children:
            self._render(cid, depth + 1, lines)

    def stats(self) -> dict:
        return {
            "nodes": len(self.order),
            "closed": sum(1 for n in self.nodes.values() if n.status == "closed"),
            "source": "tree",          # 主路径标记：报告里能看出状态从哪来
            "elements": {p: self.coverage(p) for p in {n.project for n in self.nodes.values()}},
        }
