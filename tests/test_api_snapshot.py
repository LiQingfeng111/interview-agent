"""接口响应一致性测试：新旧实现必须返回同样的结构。

## 这份测试的定位

`tests/golden/api_snapshot.json` 是从**旧 webui.py（标准库 http.server）**
录下来的 27 个接口真实响应。接下来换成 FastAPI 后，这份测试会逐字段比对。

它不是"测旧代码"，而是**重构的验收闸门**：
- 改之前：它验证快照本身完整（每条都有 status + body）
- 改之后：它验证新实现的响应能对齐快照

所以现在就跑，等新实现上线后同一个文件继续用。

## 为什么用"结构比对"而不是"逐字相等"

三类东西天然每次不同，快照里已经归一化了：
- 时间戳 → `<TIME>` / `<DATE>`
- 会话 id → `<SID>`
- 数值（金额、token）→ `<NUM>`

剩下能逐字比的就是**字段名与嵌套形状**，这正是重构最容易改坏的地方。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).parent / "golden" / "api_snapshot.json"


@pytest.fixture(scope="module")
def snap() -> dict:
    assert SNAPSHOT.exists(), (
        f"快照缺失：{SNAPSHOT}\n"
        "先跑：python tools/capture_api_snapshot.py"
    )
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def shape_of(obj, depth: int = 0) -> str:
    """把 JSON 压成"形状签名"：只保留键名与类型，不保留值。"""
    if depth > 3:
        return "…"
    if isinstance(obj, dict):
        keys = sorted(obj.keys())
        return "{" + ",".join(f"{k}:{shape_of(obj[k], depth + 1)}" for k in keys) + "}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        # 列表只看第一个元素的形状（其余应同构）
        return "[" + shape_of(obj[0], depth + 1) + "]"
    if obj is None:
        return "null"
    return type(obj).__name__


# 每个接口的期望字段（不写全部嵌套，只锁最外层 + 关键字段）
EXPECTED_KEYS = {
    "health": {"ok", "sessions"},
    "meta": {"roles", "personas", "default_resume"},
    "bank": {"total", "topics", "items"},
    "registration": {"open", "first", "existing"},
    "me": {"user", "role"},
    "register": {"ok", "user", "role"},
    "login": {"ok", "user", "role"},
    "start": {"question", "projects", "engine", "progress"},
    "answer": {"question", "eval", "progress", "done"},
    "end": {"ok", "done", "report", "dims"},
    "report": {"report", "dims"},
    "trend": {"user", "count", "points"},
    "sessions": {"sessions"},
    "users": {"users"},
    "admin": {"users", "sessions"},
}


def test_snapshot_is_complete(snap):
    """27 个接口一个不少——少一条说明录快照时漏了。"""
    assert len(snap) == 27, f"快照应有 27 条，实际 {len(snap)}：{sorted(snap)}"


@pytest.mark.parametrize("name", sorted(EXPECTED_KEYS))
def test_expected_keys_present(name, snap):
    """关键接口的最外层字段必须与快照一致。"""
    assert name in snap, f"快照缺少接口 {name}"
    body = snap[name]["body"]
    assert isinstance(body, dict), f"{name} 的响应不是对象：{type(body)}"
    missing = EXPECTED_KEYS[name] - set(body)
    assert not missing, f"{name} 缺少字段 {missing}（现有：{sorted(body)}）"


def test_anonymous_access_behavior(snap):
    """未登录时的行为要记录清楚：旧实现是"本机免登录"，返回 local 用户。

    换成 JWT 后这里会变（未登录应返回 401），
    所以这条断言的真正用途是——提醒"这是一处有意的行为变更"。
    """
    me_anon = snap["me_anon"]["body"]
    assert snap["me_anon"]["status"] == 200
    assert me_anon.get("user") == "local", (
        f"旧实现本机免登录返回 local，实际 {me_anon}；"
        "如果这里变了，说明鉴权策略变了，需要显式写进迁移说明"
    )


def test_start_response_has_interview_shape(snap):
    """/api/start 必须带第一问与项目列表，且 question 非空。"""
    body = snap["start"]["body"]
    assert isinstance(body.get("question"), str) and body["question"].strip()
    assert isinstance(body.get("projects"), list)
    assert "progress" in body, "前端进度条依赖 progress 字段"


def test_answer_response_has_eval_shape(snap):
    """/api/answer 的 eval 必须含 verdict 与 avg，这是前端评分卡的依据。"""
    ev = snap["answer"]["body"].get("eval")
    assert isinstance(ev, dict), f"eval 应为对象，实际 {type(ev)}"
    assert "verdict" in ev, f"eval 缺 verdict：{sorted(ev)}"
    assert "avg" in ev, f"eval 缺 avg：{sorted(ev)}"


def test_report_response_shape(snap):
    """报告必须能被前端直接渲染：report 是 Markdown 文本、dims 是分维度分数。"""
    body = snap["report"]["body"]
    assert isinstance(body.get("report"), str), (
        f"/api/report 的 report 是 Markdown 字符串，实际 {type(body.get('report'))}"
    )
    assert isinstance(body.get("dims"), dict), f"dims 应是字典，实际 {type(body.get('dims'))}"
    # 五个评分维度必须齐全，前端雷达图依赖它
    expected_dims = {"accuracy", "depth", "tradeoff", "boundary", "clarity"}
    assert expected_dims <= set(body["dims"]), (
        f"dims 缺维度 {expected_dims - set(body['dims'])}，实际 {sorted(body['dims'])}"
    )


def test_report_payload_is_identical_on_both_endpoints(snap):
    """/api/end 与 /api/report 返回的 report 完全一样。

    这条测试是三次试错后按事实改的，过程值得记下来：
      第 1 版：断言 report 是 dict          → 红（实际是 str）
      第 2 版：断言两接口类型不同            → 红（实际都是 str）
      第 3 版：断言长度不同                 → 红（实际都是 2108 字符，逐字相同）

    所以真实情况是：**同一个字符串被两个接口重复返回**。
    新实现里会合并成一处（`GET /api/sessions/{id}/report`），
    `/api/end` 只返回结束状态，不再重复带全文。这条测试就是变更前的证据。
    """
    end_report = snap["end"]["body"]["report"]
    get_report = snap["report"]["body"]["report"]
    assert isinstance(end_report, str) and isinstance(get_report, str)
    assert end_report == get_report, (
        "两接口的 report 内容不同了——若已按计划拆分 summary/full，请更新本测试与快照"
    )
    assert "审计" in end_report, f"报告应含审计段，实际开头：{end_report[:80]}"


def test_error_paths_are_recorded(snap):
    """异常路径也要有快照，否则重构后错误码变了也没人发现。"""
    for name in ("answer_no_sid", "answer_empty", "start_bad_role", "login_wrong_pwd"):
        assert name in snap, f"缺少异常路径快照 {name}"
        body = snap[name]["body"]
        assert isinstance(body, dict) and "error" in body, (
            f"{name} 应返回 {{error: ...}}，实际 {body}"
        )


def test_known_defect_bad_role_returns_500(snap):
    """已知缺陷：非法 role 时旧实现抛 500（应为 400）。

    这条测试**故意断言缺陷存在**，作用是：
    1. 记录现状，防止有人以为它是"正确行为"；
    2. 新实现修成 400 后，这条测试会红——提醒我们更新快照并写进变更说明。
    """
    assert snap["start_bad_role"]["status"] == 500, (
        "旧实现非法 role 返回 500；若已修成 400，请更新快照并修改本测试"
    )
    err = snap["start_bad_role"]["body"]["error"]
    assert "LLMError" in err or "role" in err.lower(), f"错误信息：{err}"
