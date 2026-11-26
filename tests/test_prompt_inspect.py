"""槽位诊断工具的测试。

它本身不改逻辑，但会出现在文档和演示里，所以输出结构必须稳定：
一旦某个槽位悄悄变空（比如 JD 关键词没传进去），要能立刻被这条用例发现。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tools.prompt_inspect import describe  # noqa: E402
from test_prompt_templates import CASES  # noqa: E402


def test_probe_retry_slots():
    sm, hint, jd, web = CASES["probe_retry"]
    row = describe("probe_retry", sm, hint, jd, web)
    filled = {s["slot"] for s in row["slots"] if s["filled"]}
    assert {"ladder", "project", "followup", "retry", "checklist", "recent", "asked"} <= filled
    assert "seed" in row["empty_slots"], "本用例没有 seed，应报为空槽位"
    assert row["total_chars"] == len(row["text"])


def test_tech_jd_slots():
    sm, hint, jd, web = CASES["tech_jd"]
    row = describe("tech_jd", sm, hint, jd, web)
    filled = {s["slot"] for s in row["slots"] if s["filled"]}
    assert {"topic", "seed", "jd_keywords", "jd_hint", "ask"} <= filled
    assert {"bank", "points", "web"} <= set(row["empty_slots"])


def test_all_cases_have_no_unknown_slot():
    """每条用例的槽位表必须覆盖模板里出现的所有名字，否则运行时才会 KeyError。"""
    for name, (sm, hint, jd, web) in CASES.items():
        row = describe(name, sm, hint, jd, web)
        assert row["slots"], f"{name} 没有槽位"
        assert row["total_chars"] > 0, f"{name} 渲染出空文本"


def test_empty_slots_are_truly_absent():
    """空槽位意味着文本里不该出现对应内容，而不是出现一行空的。"""
    sm, hint, jd, web = CASES["tech_web"]
    row = describe("tech_web", sm, hint, jd, web)
    assert "JD 关注点" not in row["text"]
    assert "题库里这道题" not in row["text"]
    assert "【联网参考资料】" in row["text"]
