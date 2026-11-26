"""面试会话路由（图驱动版）。

## 与上一版的区别

上一版直接调 `legacy_session`（同步函数），带来两个问题：
1. 服务重启后运行时丢失 → "会话不存在"
2. RAG 检索没接进出题流程

这一版走 `core/service.py`，它编排：注册表 + 状态图 + PG 持久化。

## 接口保持不变

路径、请求体、响应结构都不变——27 条接口快照仍然有效。
这是"重构不改契约"的验收点。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.api import stream as stream_mod
from app.core import errors, service
from app.deps import current_user, current_user_optional
from app.schemas import ApiResponse, AnswerRequest, FinishRequest, StartRequest

log = logging.getLogger("interview.api.sessions")

router = APIRouter()


@router.post("", response_model=ApiResponse)
def start(req: StartRequest, user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """开一场面试。

    校验顺序：Pydantic（role/persona/max_turns）→ 建运行时 → 跑图。
    旧实现是先初始化模型再校验参数，传错岗位名会 500 + LLMError（见 API-CONTRACT 问题 1）。
    """
    data = service.start(req, user["user"])
    return ApiResponse(data=data)


@router.post("/{sid}/answer", response_model=ApiResponse)
def answer(sid: str, req: AnswerRequest,
           user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """交一轮答案：恢复图 → 评分 → 下一问。"""
    data = service.answer(sid, req.answer, user["user"])
    return ApiResponse(data=data)


@router.post("/{sid}/finish", response_model=ApiResponse)
def finish(sid: str, req: FinishRequest | None = None,
           user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """结束面试。只返回状态 + 报告，不重复返回全文（旧实现两接口返同一份）。"""
    reason = (req.reason if req else "user_quit") or "user_quit"
    data = service.finish(sid, reason, user["user"])
    return ApiResponse(data=data)


@router.get("/{sid}/report", response_model=ApiResponse)
def report(sid: str, user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    data = service.report(sid, user["user"])
    return ApiResponse(data=data)


@router.get("/{sid}", response_model=ApiResponse)
def state(sid: str, user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    data = service.state_of(sid, user["user"])
    return ApiResponse(data=data)


@router.get("", response_model=ApiResponse)
def list_sessions(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    user: dict[str, str] = Depends(current_user),
) -> ApiResponse:
    """历史会话列表（分页）。"""
    try:
        from app.repos.pg import get_conn
        offset = (page - 1) * size
        is_admin = user.get("role") == "admin"
        where = "" if is_admin else "WHERE owner = %s"
        params: tuple = () if is_admin else (user["user"],)

        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""SELECT id, role, phase, status, total_turns, created_at, finish_reason
                    FROM sessions {where}
                    ORDER BY created_at DESC LIMIT %s OFFSET %s""",
                params + (size, offset),
            )
            rows = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) FROM sessions {where}", params)
            total = cur.fetchone()[0]
            cur.close()

        items = [
            {"session_id": r[0], "role": r[1], "phase": r[2], "status": r[3],
             "total_turns": r[4],
             "created_at": r[5].isoformat() if r[5] else "",
             "finish_reason": r[6] or ""}
            for r in rows
        ]
        return ApiResponse(data={"items": items, "total": int(total),
                                 "page": page, "size": size})
    except Exception as e:  # noqa: BLE001
        log.warning("读会话列表失败：%s", e)
        return ApiResponse(data={"items": [], "total": 0, "page": page, "size": size})


@router.get("/{sid}/stream")
def stream(
    sid: str,
    token: str = "",
    request: Request = None,  # type: ignore[assignment]
    user: dict[str, str] | None = Depends(current_user_optional),
) -> StreamingResponse:
    """SSE：把当前待回答的问题流式推给前端。

    ## 鉴权为什么支持 query 参数

    浏览器的 `EventSource` **无法自定义请求头**，所以带不了
    `Authorization: Bearer ...`。这是 SSE 的固有限制。

    三种解法：
    1. 把 access token 放 cookie（我们没用，因为 token 存在内存里防 XSS）
    2. 用 `fetch` + ReadableStream 手动解析 SSE（能做，但失去 EventSource 的自动重连）
    3. **在 query 里传 token**（本方案）

    选 3 的权衡：token 会出现在 URL 里 → 可能进浏览器历史/服务器日志。
    缓解：SSE 端点只是**读**一个已生成的问题，不修改状态；
    且 access token 只有 15 分钟寿命。生产环境应该加
    `Referrer-Policy: no-referrer` 并避免把 URL 写进访问日志。

    ## 两级策略

    1. **真流式**（`POST /stream/ask`）：重新调模型拿 token 流，
       首字 200-500ms。代价是多一次模型调用。
    2. **回放流式**（本端点，默认）：把已生成好的问题切块推送。
       不花钱，但首字延迟 = 整句生成时间。
    """

    def gen() -> Iterator[str]:
        try:
            # 宽松鉴权：没登录也要走 SSE 发 error 事件，
            # 而不是直接 401（EventSource 拿不到 401 的响应体）
            if user is None:
                yield stream_mod.sse_event(
                    "error", {"code": 42001, "message": "请先登录（SSE 需要 ?token= 或 Authorization）"})
                return
            question = service.pending_question(sid)
            if not question:
                yield stream_mod.sse_event("error", {"message": "当前没有待显示的问题"})
                return
            # 回放模式：把已有问题切块推送
            yield from stream_mod.stream_question(question, sid, chunk=2)
        except errors.AppError as e:
            yield stream_mod.sse_event("error", {"code": e.code, "message": e.message})
        except Exception as e:  # noqa: BLE001
            log.exception("SSE 失败 sid=%s", sid)
            yield stream_mod.sse_event(
                "error", {"message": f"流式推送出错：{type(e).__name__}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers=stream_mod.sse_headers(),
    )


@router.post("/{sid}/stream/ask")
def stream_ask(
    sid: str,
    token: str = "",
    user: dict[str, str] | None = Depends(current_user_optional),
) -> StreamingResponse:
    """真流式出题：调模型时开 stream，逐 token 推。

    这是"感知延迟最低"的路径：第一个 token 200-500ms 就到。
    代价：多一次模型调用。所以前端只在**首次出题**时用，
    后续轮次用 `POST /answer` 拿 JSON（那里已经生成过问题了）。

    鉴权同 `GET /stream`：同时支持 header 和 `?token=`
    （EventSource 带不了 header）。
    """

    def gen() -> Iterator[str]:
        try:
            if user is None:
                yield stream_mod.sse_event(
                    "error", {"code": 42001, "message": "请先登录（SSE 需要 ?token= 或 Authorization）"})
                return
            rt = service._require_runtime(sid, user["user"])

            # 拿内层 LLM 的流式接口
            tokens = _token_stream(rt)

            if tokens is not None:
                # 先试着真流式。**但要在收到第一个 token 之后才认为成功**——
                # 如果模型/relay 返回空流（实测：推理上游 502 时 relay 会
                # 回一个空 delta 然后 [DONE]），要能退回回放，
                # 否则用户看到的是"空白问题"。
                yielded = False
                try:
                    for t in tokens:
                        yielded = True
                        yield stream_mod.sse_event("token", {"live": True, "text": t})
                except Exception as e:  # noqa: BLE001
                    log.warning("真流式中断（已有 %s token）：%s", yielded, e)

                if yielded:
                    yield stream_mod.sse_event(
                        "done", {"session_id": sid, "full": "", "live": True})
                    return
                log.warning("真流式零 token（模型侧异常），退回回放")

            # 降级：回放已生成的问题
            q = service.pending_question(sid)
            if q:
                yield stream_mod.sse_event(
                    "start", {"session_id": sid, "live": False, "degraded": True})
                yield from stream_mod.stream_question(q, sid, chunk=2)
                return

            # 连问题都没有（比如首问还没生成）→ 让模型现场生成一句
            fallback = "请先做个自我介绍：说清你做过什么、技术栈、以及最想被追问的那个项目。"
            yield stream_mod.sse_event(
                "start", {"session_id": sid, "live": False, "degraded": True})
            yield from stream_mod.stream_question(fallback, sid, chunk=2)
        except errors.AppError as e:
            yield stream_mod.sse_event("error", {"code": e.code, "message": e.message})
        except Exception as e:  # noqa: BLE001
            log.exception("真流式出题失败 sid=%s", sid)
            yield stream_mod.sse_event(
                "error", {"message": f"流式出题失败：{type(e).__name__}"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers=stream_mod.sse_headers(),
    )


def _token_stream(rt: Any) -> Iterator[str] | None:
    """从运行时里挖出模型的 token 流。

    为什么要这么绕：旧 `llm.py` 的 `chat()` 是阻塞的、不支持 stream。
    但底层 `OpenAICompatibleLLM` 用的是 `urllib`，可以手写一个
    `stream: true` 的请求拿到 SSE 行流。

    返回 None 表示"拿不到流"（比如 mock 模式、模型不支持），
    调用方据此退回回放模式。
    """
    try:
        session = getattr(rt, "_session", None)
        llm = getattr(session, "llm", None)
        inner = getattr(llm, "_inner", llm)          # 拆掉 RecordingLlm 包装
        cfg = getattr(inner, "cfg", None)
        if cfg is None:
            return None
        if getattr(inner, "is_mock", False):
            return None

        # 把"当前该问的问题"作为 prompt 传进去。
        #
        # ## 为什么不能传空 prompt（踩过的坑）
        #
        # 第一版优先取 `service.pending_question(sid)`，但那个函数在
        # "问题已展示给用户"之后会被清空 → 拿到空串 → 流式请求带着
        # 空 messages 发给模型 → 模型返回空内容 → 前端看到
        # `{"live": true, "full": ""}`（有 start 有 done，但零 token）。
        #
        # 所以现在三级兜底，保证 prompt 一定非空：
        #   ① 状态里的 pending_question（问题还没被消费时）
        #   ② 用户最近一次回答（据此续问最自然）
        #   ③ 固定引导句（最后兜底，至少能出字）
        prompt = ""
        try:
            sid = getattr(rt, "session_id", "")
            if sid:
                from app.core import service as svc
                prompt = svc.pending_question(sid) or ""
        except Exception:  # noqa: BLE001
            pass

        if not prompt:
            # ② 用最近一轮的问答
            try:
                sess = getattr(rt, "_session", None)
                turns = getattr(sess, "turns", None) or []
                if turns:
                    last = turns[-1]
                    q = str(getattr(last, "question", "") or "")
                    a = str(getattr(last, "answer", "") or "")
                    if q:
                        prompt = (f"你是技术面试官。上一轮你问：{q}\n"
                                  f"候选人回答：{a[:300]}\n"
                                  f"请基于这个回答，提出下一个追问（只说问题本身，一到两句）。")
            except Exception:  # noqa: BLE001
                pass

        if not prompt:
            # ③ 最后兜底：让模型主动开个头
            prompt = "你是技术面试官，请用一句话请候选人做个自我介绍。"

        return _urllib_token_stream(cfg, prompt)
    except Exception as e:  # noqa: BLE001
        log.warning("拿 token 流失败：%s", e)
        return None


def _urllib_token_stream(cfg: Any, prompt: str = "") -> Iterator[str]:
    """手写 OpenAI 兼容的流式请求，逐行解析 SSE。

    为什么不用 openai SDK：项目本来就用 urllib 手写请求（零依赖传统），
    这里保持一致，避免为一个功能引入新依赖。

    ## 为什么同时读 `content` 和 `reasoning_content`（踩过的坑）

    `lobsterai/deepseek-flash` 是**推理模型**：它先输出 `reasoning_content`
    （思考过程），再输出 `content`（最终回答）。如果只读 `content`，
    在思考阶段一个 token 都拿不到 → 整条流看起来"没有内容" → 前端退回回放。

    实测现象：`/stream/ask` 耗时 322ms（确实连上了），但 token 帧数 = 0。

    所以两个字段都要读。前端拿到的是"思考 + 回答"的连续文本，
    对"面试官正在说话"这个体验来说是合适的（用户看到字在往外冒）。
    """
    import json as _json
    import urllib.request

    url = cfg.base_url.rstrip("/") + "/chat/completions"

    # 没有传 prompt 时不让请求发空 messages（模型会返回空内容）。
    # 这里的兜底必须非空，否则前端只会看到 start + done 而零 token。
    messages = [{"role": "user", "content": prompt or "请说一句：准备好了"}]

    payload = {
        "model": cfg.model,
        "messages": messages,
        "stream": True,
        "max_tokens": max(120, int(getattr(cfg, "max_tokens", 200) or 200)),
        "temperature": getattr(cfg, "temperature", 0.7),
    }
    body = _json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = _json.loads(data)
                choices = obj.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                # 推理模型：先 reasoning_content，后 content，两者都要
                piece = delta.get("content") or delta.get("reasoning_content")
                if piece:
                    yield piece
            except Exception:  # noqa: BLE001
                continue
