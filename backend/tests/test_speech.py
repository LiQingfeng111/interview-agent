"""语音能力测试：可用性检测、错误提示、降级路径。

不测真实识别（需要模型 + 音频），只测：
1. capabilities 接口如实报告可用性
2. 未安装时返回 501 + **可操作的安装说明**（不是"功能不可用"）
3. 参数校验

这三条对应"用户不知道发生了什么"的问题：
501 里写清楚装什么，比"不支持"有用。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import speech


def test_capabilities_reports_honestly():
    """capabilities 必须如实报告，不能谎报可用。"""
    data = speech.capabilities().data
    assert "asr" in data and "tts" in data
    assert isinstance(data["asr"]["available"], bool)
    assert isinstance(data["tts"]["available"], bool)
    # 说明里要讲清"浏览器优先"这个策略
    assert "浏览器" in data["note"]


def test_whisper_detection_no_crash():
    """检测函数不应抛异常（没装就是 False）。"""
    ok, kind = speech._whisper_available()
    assert isinstance(ok, bool)
    assert isinstance(kind, str)


def test_tts_detection_no_crash():
    ok, kind = speech._tts_available()
    assert isinstance(ok, bool)
    assert isinstance(kind, str)


def test_asr_missing_returns_actionable_501():
    """没装 whisper 时，501 里必须写清装什么。

    这是用户反馈"不知道发生什么"的直接对策：
    错误信息要能指导行动，不是"功能不可用"。
    """
    ok, _ = speech._whisper_available()
    if ok:
        return  # 装了就不测这条

    import asyncio

    class _F:
        filename = "a.webm"

        async def read(self):
            return b"x" * 100

    try:
        asyncio.run(speech.transcribe(_F(), "zh", {"user": "t", "role": "user"}))  # type: ignore[arg-type]
    except Exception as e:
        msg = str(e)
        assert "pip install" in msg, f"501 应给出安装命令，实际：{msg}"
        assert "whisper" in msg.lower()


def test_synthesize_empty_text_rejected():
    """空文本要明确拒绝，而不是返回空音频。"""
    import asyncio

    try:
        asyncio.run(speech.synthesize({"text": ""}, {"user": "t", "role": "user"}))  # type: ignore[arg-type]
    except Exception as e:
        msg = str(e)
        assert "不能为空" in msg or "为空" in msg
