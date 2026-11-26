"""SSE 流式测试：验证协议格式与降级路径。

不依赖真实模型（真流式需要模型支持 stream，测试里不好构造），
只验证：
1. SSE 帧格式正确（浏览器能派发）
2. 降级路径能把整句正确切块、拼回原文
3. 空问题返回 error 事件而不是 hang
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import stream


def parse_frames(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本解析成 [(event, data)]。"""
    out = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = "message"
        data = ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data:
            out.append((event, json.loads(data)))
    return out


def test_sse_event_format():
    """每条消息必须是 `event: X\\ndata: {...}\\n\\n`。"""
    msg = stream.sse_event("token", {"text": "你"})
    assert msg.startswith("event: token\n")
    assert msg.endswith("\n\n"), "缺少结尾换行，浏览器不会派发"
    assert '"text": "你"' in msg or '"text":"你"' in msg


def test_stream_question_reassembles():
    """切块推送后能拼回原文，且帧数合理。"""
    q = "你提到这个项目用了向量库做检索，先说说为什么要用检索？"
    text = "".join(stream.stream_question(q, "s_test", chunk=2))
    frames = parse_frames(text)

    assert frames[0][0] == "start"
    assert frames[-1][0] == "done"
    assert frames[-1][1]["full"] == q

    tokens = [d["text"] for e, d in frames if e == "token"]
    assert "".join(tokens) == q, "拼回的文本必须与原文一致"
    # 2 字一帧，帧数应接近 len/2
    assert len(tokens) == (len(q) + 1) // 2


def test_stream_question_empty():
    """空问题返回 error 事件（不能 hang）。"""
    frames = parse_frames("".join(stream.stream_question("", "s_x")))
    assert len(frames) == 1
    assert frames[0][0] == "error"
    assert "没有待显示" in frames[0][1]["message"]


def test_stream_from_llm_forwarding():
    """真流式：转发 token 并汇总 full。"""
    def fake_tokens():
        for t in ["你", "好", "，", "请", "介绍"]:
            yield t

    frames = parse_frames("".join(stream.stream_from_llm(fake_tokens(), "s_y")))
    assert frames[0] == ("start", {"session_id": "s_y", "live": True})
    tokens = [d["text"] for e, d in frames if e == "token"]
    assert "".join(tokens) == "你好，请介绍"
    assert frames[-1][0] == "done"
    assert frames[-1][1]["full"] == "你好，请介绍"


def test_stream_from_llm_error_graceful():
    """模型流中断 → 发 error 事件而不是抛异常。"""
    def broken():
        yield "开头"
        raise RuntimeError("连接断了")

    frames = parse_frames("".join(stream.stream_from_llm(broken(), "s_z")))
    events = [e for e, _ in frames]
    assert "token" in events
    assert events[-1] == "error", f"应以 error 结尾，实际 {events}"


def test_sse_headers_have_no_buffering():
    """X-Accel-Buffering: no 必须存在——否则 Nginx 会把 SSE 攒着不发。"""
    h = stream.sse_headers()
    assert h["X-Accel-Buffering"] == "no"
    assert h["Cache-Control"] == "no-cache"
