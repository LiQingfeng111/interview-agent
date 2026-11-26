"""数据契约：所有跨模块传递的结构都在这里定义。

为什么要单独一个文件？
Agent 项目最容易烂的地方就是"到处是裸 dict"，字段名改一次全线崩。
把契约集中，等于给自己一份可检查的接口文档——这也是面试官常问的"
你怎么管理多 Agent 之间的数据流"的答案。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from config import ELEMENT_NAMES, RUBRIC_DIMS, VERDICT_THRESHOLDS

# 候选人对某个追问要素的三种状态：
#   covered → 讲到了，算会
#   evaded  → 说了很多但绕开了该点（顾左右而言他）
#   unknown → 直接承认不知道 / 没答
TARGET_STATUSES = ("covered", "evaded", "unknown")

# 评分官必须输出的 JSON schema（用于校验，也可直接喂给模型的 response_format）
EVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{d: {"type": "integer", "minimum": 1, "maximum": 5} for d in RUBRIC_DIMS},
        "verdict": {"type": "string", "enum": ["pass", "partial", "fail"]},
        "weakest": {"type": "string", "enum": RUBRIC_DIMS},
        "evidence": {"type": "string", "maxLength": 60},
        "followup_hint": {"type": "string", "maxLength": 60},
        # 这段回答实际讲到了哪些深挖要素（用于跳过已答过的问题）
        "covered": {"type": "array", "items": {"type": "string", "enum": ELEMENT_NAMES}},
        # 事实核对：候选人这句技术表述与参考资料是否冲突
        "fact_check": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ok", "error", "unverifiable"]},
                "detail": {"type": "string", "maxLength": 80},
            },
            "required": ["status"],
        },
    },
    "required": [*RUBRIC_DIMS, "verdict", "weakest", "evidence"],
    "additionalProperties": False,
}

RESUME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "projects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "stack": {"type": "array", "items": {"type": "string"}},
                    "highlights": {"type": "array", "items": {"type": "string"}},
                    "metrics": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["projects"],
    "additionalProperties": False,
}


@dataclass
class Project:
    name: str
    role: str = ""
    stack: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)

    def one_liner(self) -> str:
        stack = "/".join(self.stack[:4]) if self.stack else "技术栈未说明"
        return f"{self.name}（{self.role or '角色未说明'}｜{stack}）"


@dataclass
class Evaluation:
    accuracy: int
    depth: int
    tradeoff: int
    boundary: int
    clarity: int
    verdict: str
    weakest: str
    evidence: str = ""
    followup_hint: str = ""
    # 这段回答覆盖了哪些深挖要素（"目标"/"baseline"/"指标"/"失败方案"/"个人贡献"）
    covered: list[str] = field(default_factory=list)
    # 本次追问针对的要素，以及回答对它的状态：covered / evaded / unknown
    target_element: str = ""
    target_status: str = ""
    # 模型自己给的判定（仅作参考）：阈值说了算之后，这里的值只用来发现"尺子与模型不一致"
    model_verdict: str = ""
    # 模型判定与阈值判定是否打架（打架说明提示词或阈值需要复核）
    verdict_conflict: bool = False
    # 事实核对：ok / error / unverifiable（没有联网资料时一律 unverifiable）
    fact_status: str = ""
    fact_detail: str = ""

    @property
    def avg(self) -> float:
        vals = [getattr(self, d) for d in RUBRIC_DIMS]
        return sum(vals) / len(vals)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict, mode: str = "real") -> "Evaluation":
        d = normalize_eval(d, mode)
        return cls(**d)


@dataclass
class Turn:
    idx: int
    phase: str
    question: str
    answer: str = ""
    evaluation: Evaluation | None = None
    ladder_level: int = 0
    project: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "idx": self.idx,
            "phase": self.phase,
            "question": self.question,
            "answer": self.answer,
            "evaluation": self.evaluation.to_dict() if self.evaluation else None,
            "ladder_level": self.ladder_level,
            "project": self.project,
            "note": self.note,
        }


@dataclass
class SessionResult:
    role: str
    projects: list[Project]
    turns: list[Turn]
    feedback: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "projects": [asdict(p) for p in self.projects],
            "turns": [t.to_dict() for t in self.turns],
            "feedback": self.feedback,
            "meta": self.meta,
        }


# ---------------------------------------------------------------- 校验
def normalize_eval(d: dict, mode: str = "real") -> dict:
    """宽容地修正模型输出：裁剪分数区间、补默认值、重算 verdict。

    生产的真实情况：模型十次里总有一两次给 0 分、给 4.5 分、或者把
    verdict 拼错。规整逻辑比 prompt 约束更可靠。
    """
    out: dict[str, Any] = {}
    for dim in RUBRIC_DIMS:
        raw = d.get(dim, 3)
        try:
            val = int(round(float(raw)))
        except (TypeError, ValueError):
            val = 3
        out[dim] = max(1, min(5, val))

    weak = d.get("weakest", "")
    out["weakest"] = weak if weak in RUBRIC_DIMS else min(
        RUBRIC_DIMS, key=lambda x: out[x]
    )

    # 判定权在阈值，不在模型。
    # 之前的写法是"模型给了合法 verdict 就用模型的"，后果是：
    # 校准出来的阈值只在校模型不给 verdict 时才生效（实测 12 条标注里 10 条被模型覆盖），
    # 于是"尺子可校准"变成了一句空话。现在模型的判定只作为对照记录。
    avg = sum(out[x] for x in RUBRIC_DIMS) / len(RUBRIC_DIMS)
    pass_min, partial_min = VERDICT_THRESHOLDS.get(mode, VERDICT_THRESHOLDS["real"])
    by_score = ("pass" if avg >= pass_min
                else "partial" if avg >= partial_min else "fail")
    model_verdict = str(d.get("verdict", "")).lower().strip()
    if model_verdict not in ("pass", "partial", "fail"):
        model_verdict = ""
    out["verdict"] = by_score
    out["model_verdict"] = model_verdict
    out["verdict_conflict"] = bool(model_verdict and model_verdict != by_score)
    out["evidence"] = str(d.get("evidence", ""))[:60]
    out["followup_hint"] = str(d.get("followup_hint", ""))[:60]

    # covered 只保留合法要素名；模型给中文近义词时做一次归一
    raw_covered = d.get("covered") or []
    if isinstance(raw_covered, str):
        raw_covered = [raw_covered]
    alias = {
        "基线": "baseline", "对比基线": "baseline", "baseline": "baseline",
        "衡量指标": "指标", "量化": "指标",
        "失败方案": "失败方案", "失败的方案": "失败方案", "踩坑": "失败方案",
        "贡献": "个人贡献", "个人贡献": "个人贡献",
        "目标": "目标", "背景": "目标",
    }
    covered: list[str] = []
    for item in raw_covered:
        name = str(item).strip()
        name = alias.get(name, name)
        if name in ELEMENT_NAMES and name not in covered:
            covered.append(name)
    out["covered"] = covered
    out["target_element"] = str(d.get("target_element", "")).strip()
    status = str(d.get("target_status", "")).strip().lower()
    out["target_status"] = status if status in TARGET_STATUSES else ""

    # 事实核对：模型可能给字符串也可能给对象，两种都收；非法值一律降级为 unverifiable
    fc = d.get("fact_check")
    fstatus, fdetail = "", ""
    if isinstance(fc, dict):
        fstatus = str(fc.get("status", "")).strip().lower()
        fdetail = str(fc.get("detail", ""))[:80]
    elif isinstance(fc, str):
        fstatus = fc.strip().lower()
    if fstatus not in ("ok", "error", "unverifiable"):
        fstatus = "unverifiable"
    out["fact_status"] = fstatus
    out["fact_detail"] = fdetail
    return out


def is_valid_json_shape(obj: dict) -> bool:
    """极简 schema 校验（不引入 jsonschema 依赖）。"""
    for dim in RUBRIC_DIMS:
        if dim not in obj or not isinstance(obj[dim], (int, float)):
            return False
    return isinstance(obj.get("verdict"), str)
