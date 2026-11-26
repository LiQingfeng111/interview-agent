"""Pydantic 模型：请求/响应的唯一契约。

## 为什么必须有

旧实现 27 个接口全靠 `payload.get("xxx", "")` 手工取值，后果是：

1. 前端传错字段名**不会报错**，只会静默拿到默认值。
   实测过一次：注册接口字段是 `password`，我传了 `pwd`，
   结果报"密码至少 6 位"——其实是读到了空字符串，不是密码真的短。

2. 无法生成前端类型。Vue + TS 要手写一遍 interface，两边容易漂。

3. 无法做参数校验。`max_turns: 50` 要等到业务逻辑里才发现越界。

Pydantic 一次解决三个问题：字段名错就 422、自动生成 OpenAPI、校验规则写成声明。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.core import errors
from app.settings import settings


# ---------------------------------------------------------------- 通用
class ApiResponse(BaseModel):
    """统一响应包：code=0 成功，其余见 core/errors.py 的分段。"""
    code: int = 0
    data: Any = None
    message: str = ""
    request_id: str = ""
    detail: dict | None = None


# ---------------------------------------------------------------- 鉴权
class LoginRequest(BaseModel):
    user: str = Field(min_length=1, max_length=64, description="用户名")
    password: str = Field(min_length=1, max_length=128, description="密码")


class RegisterRequest(BaseModel):
    user: str = Field(min_length=3, max_length=20, description="3–20 位字母/数字/下划线")
    password: str = Field(min_length=6, max_length=128, description="至少 6 位")

    @field_validator("user")
    @classmethod
    def check_user(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", v):
            raise ValueError("只能包含字母、数字、下划线（3–20 位）")
        return v


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: str
    role: str


class MeResponse(BaseModel):
    user: str
    role: str


# ---------------------------------------------------------------- 会话
class StartRequest(BaseModel):
    """开一场面试。

    注意 `role` 和 `persona` 的校验发生在**模型初始化之前**——
    旧实现是先初始化模型再校验参数，所以传错岗位名会返回 500 + LLMError
    （见 docs/API-CONTRACT.md 问题 1）。
    """
    resume: str = Field(default="", max_length=50_000, description="简历正文")
    jd: str = Field(default="", max_length=20_000, description="岗位 JD（可选）")
    role: str = Field(default="", description="岗位方向")
    persona: str = Field(default="balanced", description="面试官风格")
    max_turns: int = Field(default=16, ge=6, le=30, description="最多轮次 6–30")
    web: bool = Field(default=False, description="是否开启联网检索")
    mock: bool = Field(default=False, description="mock 模式（不调模型，只验流程）")
    coach: bool = Field(default=False, description="评估模式（每轮给评分理由）")
    document_id: str = Field(default="", description="已上传简历的 id（优先于 resume）")

    @field_validator("role")
    @classmethod
    def check_role(cls, v: str) -> str:
        from app.core import bridge
        allowed = bridge.roles()
        if v and v not in allowed:
            raise ValueError(f"岗位「{v}」不存在，可选：{' / '.join(allowed)}")
        return v or settings.default_role

    @field_validator("persona")
    @classmethod
    def check_persona(cls, v: str) -> str:
        from app.core import bridge
        allowed = bridge.personas()
        if v not in allowed:
            raise ValueError(f"风格「{v}」不存在，可选：{' / '.join(allowed)}")
        return v


class AnswerRequest(BaseModel):
    answer: str = Field(description="候选人回答")

    @field_validator("answer")
    @classmethod
    def check_answer(cls, v: str) -> str:
        # 注意：这里不做 strip 后判空，交给业务层做——因为空答案要返回
        # "请先输入回答再提交"（422/41001），而不是 Pydantic 的默认校验消息。
        if len(v) > 5000:
            raise ValueError(f"回答太长了（上限 5000 字），当前 {len(v)} 字")
        return v


class FinishRequest(BaseModel):
    reason: str = Field(default="user_quit", description="结束原因")


class EvalView(BaseModel):
    """一轮的评分结果。

    字段名与旧实现保持一致（前端已按这个结构写），
    只是补上了 model_verdict / verdict_conflict 这类审计字段。
    """
    verdict: str = ""
    avg: float = 0.0
    weakest: str = ""
    evidence: str = ""
    dims: dict[str, float] = Field(default_factory=dict)
    model_verdict: str = ""
    verdict_conflict: bool = False
    degraded: bool = False
    covered: list[str] = Field(default_factory=list)


class ProgressView(BaseModel):
    turn: int = 0
    max_turns: int = 16
    phase: str = ""
    phase_desc: str = ""
    ladder_level: int = 0
    ladder_name: str = ""
    element_coverage: dict[str, str] = Field(default_factory=dict)


class TurnResponse(BaseModel):
    session_id: str
    turn: int
    question: str = ""
    element: str = ""
    eval: EvalView | None = None
    progress: ProgressView | None = None
    done: bool = False
    card: dict | None = None


# ---------------------------------------------------------------- 文档
class DocumentView(BaseModel):
    id: str
    kind: str
    filename: str
    size_bytes: int
    chunks: int = 0
    status: str = "pending"        # pending / parsing / embedding / ready / failed
    error: str = ""


# ---------------------------------------------------------------- 题库
class BankItem(BaseModel):
    id: str
    role: str = ""
    topic: str = ""
    level: str = ""
    question: str
    points: list[str] = Field(default_factory=list)


class BankSearchRequest(BaseModel):
    q: str = Field(min_length=1, description="查询文本")
    role: str = ""
    k: int = Field(default=10, ge=1, le=50)
    threshold: float | None = Field(default=None, ge=0, le=1)


class BankSearchHit(BaseModel):
    item: BankItem
    score: float
    rank: int


# ---------------------------------------------------------------- 诊断
class RagSearchRequest(BaseModel):
    index: str = Field(description="resume / jd / bank / history")
    q: str = Field(min_length=1)
    k: int = Field(default=5, ge=1, le=50)
    threshold: float | None = None


class HealthResponse(BaseModel):
    ok: bool
    version: str = "2.0.0"
    components: dict[str, Any] = Field(default_factory=dict)
