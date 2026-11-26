"""FastAPI 应用入口。

## 为什么换掉 http.server

旧的 `webui.py` 用标准库 `ThreadingHTTPServer`，能跑但缺三样东西：

1. **没有异步** —— 一次模型调用要 3-5 秒，线程池很容易被打满
2. **没有 SSE** —— 无法做"逐字显示面试官提问"
3. **没有 schema** —— 27 个接口的参数全靠手写 dict 取值，前端无法生成类型

FastAPI 三样都有，代价是引入依赖（这也是原项目"零依赖"卖点的终结，
换成"一条 docker compose 起完整栈"）。

## 这个文件只做三件事

1. 创建 app
2. 挂中间件（CORS、请求 ID、统一异常）
3. 注册路由

**业务逻辑一行都不写在这里**，全部在 `api/` 下按领域拆开。
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import auth, bank, diagnostics, documents, sessions, speech
from app.core.errors import AppError, error_body
from app.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("interview.api")


def create_app() -> FastAPI:
    # 启动自检：缺模块/缺表在启动阶段就炸，别等到运行时静默降级
    # （踩过的坑：documents 模块缺失被 except 吞掉，表现为"上传成功但开场失败"）
    from app.core import selfcheck
    selfcheck.run(strict=True)

    app = FastAPI(
        title="模拟面试 Agent",
        version="2.0.0",
        description="项目深挖型模拟面试：出题交给模型、流程由代码判定",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # 前端 Vue 开发服务器在 5173，生产由 Nginx 同源代理
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        """给每个请求一个追踪号。

        用户报障时说"追踪号 abc123"，我们能在日志里精确找到那一次请求，
        不用让对方复述时间、操作、参数。这也是 59999 错误里给出追踪号的原因。
        """
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        t0 = time.perf_counter()
        response = await call_next(request)
        cost_ms = (time.perf_counter() - t0) * 1000
        response.headers["X-Request-Id"] = rid
        # 慢请求单独标出来：模型调用本来就慢，但超过 10 秒值得看一眼
        level = logging.WARNING if cost_ms > 10_000 else logging.INFO
        log.log(level, "%s %s → %s %.0fms rid=%s",
                request.method, request.url.path, response.status_code, cost_ms, rid)
        return response

    # ------------------------------------------------------------ 统一异常
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError):
        """业务异常：按 AppError 里的 code/message 原样返回。

        这里是"每种错误一句人话"的落地点——
        具体措辞在 core/errors.py 的常量表里，不在路由里拼字符串。
        """
        rid = getattr(request.state, "request_id", "")
        log.info("业务错误 %s %s → %s %s", request.method, request.url.path,
                 exc.code, exc.message)
        return JSONResponse(
            status_code=exc.status,
            content=error_body(exc.code, exc.message, rid),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        """未捕获异常：只给追踪号，不泄露堆栈。

        堆栈进日志，用户拿到 rid 报障即可对上。
        """
        rid = getattr(request.state, "request_id", "")
        log.exception("未捕获异常 %s %s rid=%s", request.method, request.url.path, rid)
        return JSONResponse(
            status_code=500,
            content=error_body(59999, f"服务出了点问题，已记录（追踪号：{rid}）", rid),
        )

    # ------------------------------------------------------------ 路由
    app.include_router(auth.router, prefix="/api/auth", tags=["鉴权"])
    app.include_router(sessions.router, prefix="/api/sessions", tags=["面试会话"])
    app.include_router(documents.router, prefix="/api/documents", tags=["简历/JD"])
    app.include_router(bank.router, prefix="/api/bank", tags=["题库"])
    app.include_router(diagnostics.router, prefix="/api/diagnostics", tags=["诊断"])
    app.include_router(speech.router, prefix="/api/speech", tags=["语音"])

    @app.get("/api/health", tags=["运维"])
    async def health():
        """健康检查：连带报告依赖状态，一眼看出哪个组件挂了。"""
        from app.deps import health_snapshot

        return health_snapshot()

    return app


app = create_app()
