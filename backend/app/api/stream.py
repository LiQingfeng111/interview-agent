"""SSE 真流式：让出题过程逐 token 推给前端。

## 之前是假流式

旧实现把**已经生成好的整句**切 3 个字一帧推送，看着像打字机，但：
- 用户等待时间完全没变（仍要等模型 2–5 秒）
- 首字出现时间 = 整句生成时间，不是流式的价值

## 真流式的收益

模型支持 stream 时，第一个 token 在 200–500ms 就到，用户立刻看到"面试官开始说话了"。
这是感知延迟的巨大差别（5 秒空等 vs 0.3 秒见字）。

## 实现要点

1. **降级**：模型不支持 stream 或出错 → 退回"整句切块"，用户仍能看到逐字效果
2. **不阻塞主链路**：SSE 只推"当前待回答问题"，评分和状态推进仍走普通接口
3. **断线重连**：前端 EventSource 自动重连，服务端每次重发完整问题（幂等）

## 为什么 SSE 而不是 WebSocket

单向推送（服务端 → 客户端），SSE 足够且更简单：
- 原生 EventSource 自动重连
- 走普通 HTTP，不需要协议升级、不会被代理拦
- 生产环境 Nginx 加 `X-Accel-Buffering: no` 即可
"""

from __future__ import annotations

import json
import logging
from typing import Iterator

log = logging.getLogger("interview.stream")


def sse_event(event: str, data: dict) -> str:
    """格式化一条 SSE 消息。

    必须用 `\\n\\n` 结尾——少一个换行浏览器不会派发事件。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_question(question: str, sid: str,
                    chunk: int = 2, delay: float = 0.0) -> Iterator[str]:
    """把问题切成小块推送（降级路径：模型没提供真流式时用）。

    Args:
        question: 已生成好的完整问题
        sid: 会话 id（前端用来关联）
        chunk: 每帧几个字符。2 个字符接近真人打字速度
        delay: 帧间隔秒数（0 表示不等待，让网络自己限速）
    """
    import time

    if not question:
        yield sse_event("error", {"message": "当前没有待显示的问题"})
        return

    yield sse_event("start", {"session_id": sid, "length": len(question)})

    for i in range(0, len(question), chunk):
        piece = question[i:i + chunk]
        yield sse_event("token", {"text": piece})
        if delay:
            time.sleep(delay)

    yield sse_event("done", {"session_id": sid, "full": question})


def stream_from_llm(llm_stream: Iterator[str], sid: str) -> Iterator[str]:
    """真流式：直接转发模型的 token 流。

    Args:
        llm_stream: 模型返回的 token 迭代器（OpenAI 兼容接口的 delta.content）
    """
    yield sse_event("start", {"session_id": sid, "live": True})
    buf: list[str] = []
    try:
        for token in llm_stream:
            if not token:
                continue
            buf.append(token)
            yield sse_event("token", {"text": token})
    except Exception as e:  # noqa: BLE001
        log.warning("模型流式中断：%s", e)
        yield sse_event("error", {"message": f"流式中断：{type(e).__name__}"})
        return

    full = "".join(buf)
    yield sse_event("done", {"session_id": sid, "full": full, "live": True})


def sse_headers() -> dict[str, str]:
    """SSE 必需的响应头。

    - `Cache-Control: no-cache` —— 中间层不缓存
    - `X-Accel-Buffering: no` —— **关键**：Nginx 默认缓冲会让 SSE 攒着不发，
      表现是"等全部生成完才一次吐出来"，和假流式一样
    - `Connection: keep-alive` —— 长连接
    """
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
