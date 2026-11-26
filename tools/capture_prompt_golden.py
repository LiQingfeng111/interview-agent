"""把当前 _build_prompt 的输出固化成黄金样本（重构前先拍快照）。

为什么要先拍快照：提示词一字之差，模型行为就会漂，而这些差异在单元测试里
看不出来（没人会去断言 300 字的 prompt 全文）。先把现状存成 JSON，
重构后逐字比对，就能做到"结构换了、文本没变"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interviewer import Interviewer  # noqa: E402
from llm import MockLLM  # noqa: E402


def make_sm(phase: str, desc: str, project: str, history: list[dict]):
    probe = SimpleNamespace(current=project, ladder_level=2)
    return SimpleNamespace(
        phase=phase,
        phase_cfg={"desc": desc},
        probe=probe,
        history=history,
        ladder_name="怎么做",
    )


HISTORY = [
    {"q": "这个项目要解决什么问题？", "a": "解决课程资料检索慢的问题", "verdict": "pass"},
    {"q": "你的 baseline 是什么？", "a": "原来人工翻 PPT，平均 8 分钟", "verdict": "partial"},
]


def cases() -> list[tuple[str, object, dict]]:
    out = []
    # 1) OPENING
    out.append((
        "opening",
        make_sm("OPENING", "开场", "", []),
        {"phase": "OPENING", "persona": "balanced"},
    ))
    # 2) SELF_INTRO
    out.append((
        "self_intro",
        make_sm("SELF_INTRO", "自我介绍", "", []),
        {"phase": "SELF_INTRO", "persona": "balanced"},
    ))
    # 3) PROJECT_PROBE 首问
    out.append((
        "probe_first",
        make_sm("PROJECT_PROBE", "项目深挖", "课程问答助手", HISTORY),
        {"phase": "PROJECT_PROBE", "persona": "balanced", "project": "课程问答助手",
         "ladder_level": 2, "ladder_name": "怎么做", "attempt": 0},
    ))
    # 4) PROJECT_PROBE 带 verify + followup + 重追问
    out.append((
        "probe_retry",
        make_sm("PROJECT_PROBE", "项目深挖", "课程问答助手", HISTORY),
        {"phase": "PROJECT_PROBE", "persona": "strict", "project": "课程问答助手",
         "ladder_level": 2, "ladder_name": "怎么做", "attempt": 1,
         "verdict": "partial", "weakest": "tradeoff",
         "followup_hint": "追问 500 字这个切块是怎么定的"},
    ))
    # 5) PROJECT_PROBE 带 seed_question
    out.append((
        "probe_seed",
        make_sm("PROJECT_PROBE", "项目深挖", "课程问答助手", HISTORY),
        {"phase": "PROJECT_PROBE", "persona": "balanced", "project": "课程问答助手",
         "ladder_level": 3, "ladder_name": "取舍", "attempt": 0,
         "seed_question": "为什么选 500 字而不是 300 字？"},
    ))
    # 6) TECH_QA 带题库题与要点
    out.append((
        "tech_bank",
        make_sm("TECH_QA", "技术问答", "", HISTORY),
        {"phase": "TECH_QA", "persona": "balanced", "topic": "Transformer 注意力机制",
         "bank_question": "多头注意力中 d_model=512、h=8 时每个头维度多少？",
         "bank_points": ["d_k = d_v = 64", "各头学不同子空间", "concat 后 W_O 融合"]},
    ))
    # 7) TECH_QA 带 JD 关键词
    out.append((
        "tech_jd",
        make_sm("TECH_QA", "技术问答", "", HISTORY),
        {"phase": "TECH_QA", "persona": "engineering", "topic": "缓存一致性",
         "jd_keywords": ["Redis", "分布式锁", "多级缓存"]},
    ))
    # 8) TECH_QA 带联网参考
    out.append((
        "tech_web",
        make_sm("TECH_QA", "技术问答", "", HISTORY),
        {"phase": "TECH_QA", "persona": "balanced", "topic": "向量检索",
         "web_refs": "HNSW 在 1000 万规模下的 recall@10 通常在 0.95 以上"},
    ))
    # 9) CANDIDATE_ASK
    out.append((
        "candidate_ask",
        make_sm("CANDIDATE_ASK", "候选人反问", "", HISTORY),
        {"phase": "CANDIDATE_ASK", "persona": "balanced"},
    ))
    return out


def main() -> int:
    out: dict[str, dict] = {}
    for name, sm, hint in cases():
        iv = Interviewer(llm=MockLLM(seed=1))
        iv.jd_keywords = hint.pop("jd_keywords", []) if "jd_keywords" in hint else []
        if "jd_keywords" in hint:
            pass
        # 每条用例重新注入 jd_keywords（hint 里已被 pop）
        if name == "tech_jd":
            iv.jd_keywords = ["Redis", "分布式锁", "多级缓存"]
        iv.web_refs = ""
        out[name] = {
            "phase": sm.phase,
            "hint": hint,
            "text": iv._build_prompt(sm, hint),
        }
    dest = Path(__file__).resolve().parents[1] / "tests" / "golden" / "prompt_build.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {len(out)} 条黄金样本 → {dest}")
    for k, v in out.items():
        print(f"  {k}: {len(v['text'])} 字符")
    return 0


if __name__ == "__main__":
    sys.exit(main())
