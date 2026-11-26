"""错误码常量与异常类型。

## 设计依据（见 docs/ERROR-DESIGN.md）

**统一的是结构，不是措辞。** 每个错误都有：
- `status`：HTTP 状态码（给机器）
- `code`：业务码（按段分类，前端按段分流）
- `message`：一句人话（给用户，说清发生了什么 + 该怎么办）

## 为什么错误消息写在这里而不是路由里

路由里拼字符串会导致：
1. 同一个错误在两个接口说法不同；
2. 改措辞要翻遍所有路由；
3. 无法被测试枚举（"有没有哪种错误没写提示"）。

集中成常量后，测试可以遍历 `ALL_ERRORS` 检查
"每条 message 是否够具体"，而不用逐个接口去试。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- 分段
# 40xxx 会话 | 41xxx 输入校验 | 42xxx 鉴权 | 43xxx 模型检索 | 44xxx 预算 | 5xxxx 系统


class AppError(Exception):
    """业务异常：带 HTTP 状态码、业务码、用户可见消息。"""

    def __init__(self, status: int, code: int, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "code": self.code,
                "message": self.message, "detail": self.detail}


def error_body(code: int, message: str, request_id: str = "",
               detail: dict | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "data": None, "message": message}
    if detail:
        body["detail"] = detail
    if request_id:
        body["request_id"] = request_id
    return body


def ok_body(data: Any = None) -> dict[str, Any]:
    return {"code": 0, "data": data, "message": ""}


# ---------------------------------------------------------------- 会话类 40xxx
def session_not_found() -> AppError:
    return AppError(404, 40003,
                    "这场面试已过期（服务重启后常见），可以重新开始一场")


def session_missing_id() -> AppError:
    return AppError(404, 40001, "找不到这场面试，可能链接不完整")


def session_bad_id(sid: str) -> AppError:
    return AppError(400, 40002, "面试编号格式不正确", {"received": sid})


def session_finished() -> AppError:
    return AppError(409, 40004, "这场面试已经结束了，去查看报告吧")


def session_forbidden() -> AppError:
    return AppError(403, 40005, "这不是你的面试，无权查看")


def session_busy() -> AppError:
    return AppError(409, 40006, "上一题还在评分中，请稍等")


def session_turns_exhausted(max_turns: int) -> AppError:
    return AppError(409, 40007, f"已达到本轮最大轮次（{max_turns} 轮），面试结束",
                    {"max_turns": max_turns})


# ---------------------------------------------------------------- 校验类 41xxx
def answer_empty() -> AppError:
    return AppError(422, 41001, "请先输入回答再提交")


def answer_too_short(n: int, minimum: int = 10) -> AppError:
    return AppError(422, 41002, f"回答太短了，再多说几句（至少 {minimum} 个字）",
                    {"length": n, "min": minimum})


def answer_too_long(n: int, maximum: int = 5000) -> AppError:
    return AppError(422, 41003, f"回答太长了（上限 {maximum} 字），可以精简一下",
                    {"length": n, "max": maximum})


def resume_no_project() -> AppError:
    return AppError(422, 41004,
                    "简历里没识别到项目经历，请检查是否包含「项目」段落")


def file_unsupported(suffix: str, allowed: list[str]) -> AppError:
    return AppError(415, 41005,
                    f"只支持 {' / '.join(allowed)}，当前文件是 {suffix}",
                    {"allowed": allowed, "received": suffix})


def file_too_large(size_mb: float, limit_mb: float = 10) -> AppError:
    return AppError(413, 41006,
                    f"文件 {size_mb:.1f}MB 超过上限 {limit_mb:.0f}MB，请压缩后再传",
                    {"size_mb": size_mb, "limit_mb": limit_mb})


def unknown_role(role: str, allowed: list[str]) -> AppError:
    """把可选值列出来——用户看到就知道怎么改，这比"参数错误"有用。"""
    return AppError(400, 41007,
                    f"岗位「{role}」不存在，可选：{' / '.join(allowed)}",
                    {"allowed": allowed, "received": role})


def unknown_persona(persona: str, allowed: list[str]) -> AppError:
    return AppError(400, 41009,
                    f"风格「{persona}」不存在，可选：{' / '.join(allowed)}",
                    {"allowed": allowed, "received": persona})


def turns_out_of_range(n: int, low: int = 6, high: int = 30) -> AppError:
    return AppError(400, 41008,
                    f"轮次需在 {low}–{high} 之间，当前是 {n}",
                    {"received": n, "min": low, "max": high})


# ---------------------------------------------------------------- 鉴权类 42xxx
def not_logged_in() -> AppError:
    return AppError(401, 42001, "请先登录")


def bad_credentials() -> AppError:
    return AppError(401, 42002, "用户名或密码不对")


def account_locked(minutes: int = 15) -> AppError:
    return AppError(423, 42003, f"密码错误次数太多，{minutes} 分钟后再试")


def token_expired() -> AppError:
    return AppError(401, 42004, "登录已过期，正在自动续期…")


def refresh_expired() -> AppError:
    return AppError(401, 42005, "登录状态失效，请重新登录")


def token_invalid() -> AppError:
    return AppError(401, 42006, "登录凭证无效，请重新登录")


def admin_required() -> AppError:
    return AppError(403, 42007, "这个页面需要管理员权限")


def not_owner() -> AppError:
    return AppError(403, 42008, "你只能查看自己的面试记录")


def user_exists(name: str) -> AppError:
    return AppError(409, 42009, f"用户名「{name}」已被占用，换一个试试",
                    {"user": name})


def password_too_short(n: int, minimum: int = 6) -> AppError:
    return AppError(400, 42010, f"密码至少 {minimum} 位",
                    {"length": n, "min": minimum})


def bad_username(reason: str) -> AppError:
    return AppError(400, 42011,
                    f"用户名只能包含字母、数字、下划线（3–20 位）——{reason}")


def registration_closed() -> AppError:
    return AppError(403, 42012, "当前部署已关闭自助注册，请联系管理员开通")


# ---------------------------------------------------------------- 模型检索 43xxx
def model_unreachable(detail: str = "") -> AppError:
    return AppError(503, 43001, "模型服务暂时连不上，正在用规则兜底评分",
                    {"detail": detail} if detail else {})


def model_empty() -> AppError:
    return AppError(502, 43002, "模型这次没给出内容，已自动重试")


def model_bad_format(detail: str = "") -> AppError:
    return AppError(502, 43003, "模型输出格式异常，已用兜底逻辑处理",
                    {"detail": detail} if detail else {})


def model_timeout(seconds: int) -> AppError:
    return AppError(504, 43004, f"模型响应太慢（超过 {seconds} 秒），本题用规则评分",
                    {"seconds": seconds})


def model_quota_exhausted() -> AppError:
    return AppError(429, 43005, "模型额度已用完，请联系管理员充值")


def retrieval_down() -> AppError:
    return AppError(503, 43006, "语义检索暂不可用，已退回关键词匹配")


def embedding_dim_mismatch(expected: int, actual: int) -> AppError:
    return AppError(500, 43007, "向量索引需要重建（模型维度变了）",
                    {"expected": expected, "actual": actual})


# ---------------------------------------------------------------- 预算 44xxx
def cost_limit_reached(limit_cny: float) -> AppError:
    return AppError(402, 44001,
                    f"本次面试已达成本上限（{limit_cny} 元），报告仍会生成",
                    {"limit_cny": limit_cny})


# ---------------------------------------------------------------- 系统 5xxxx
def db_unavailable(detail: str = "") -> AppError:
    return AppError(503, 50001, "数据服务暂时不可用，请稍后重试",
                    {"detail": detail} if detail else {})


def db_conflict() -> AppError:
    return AppError(409, 50002, "数据正在被修改，请重试")


def bad_json(detail: str = "") -> AppError:
    return AppError(400, 40000, "请求格式错误，需要 JSON",
                    {"detail": detail} if detail else {})


def missing_field(fields: list[str]) -> AppError:
    return AppError(400, 40001, f"缺少必填字段：{', '.join(fields)}",
                    {"missing": fields})


def not_found(path: str) -> AppError:
    return AppError(404, 40400, f"接口 {path} 不存在", {"path": path})


# ---------------------------------------------------------------- 可遍历清单
#: 供测试遍历：检查每条消息都"够具体"（有可选值/有下一步/有具体数字）
ALL_ERRORS: dict[int, str] = {
    40001: "找不到这场面试，可能链接不完整",
    40002: "面试编号格式不正确",
    40003: "这场面试已过期（服务重启后常见），可以重新开始一场",
    40004: "这场面试已经结束了，去查看报告吧",
    40005: "这不是你的面试，无权查看",
    40006: "上一题还在评分中，请稍等",
    40007: "已达到本轮最大轮次（{n} 轮），面试结束",
    41001: "请先输入回答再提交",
    41002: "回答太短了，再多说几句（至少 {n} 个字）",
    41003: "回答太长了（上限 {n} 字），可以精简一下",
    41004: "简历里没识别到项目经历，请检查是否包含「项目」段落",
    41005: "只支持 {list}，当前文件是 {suffix}",
    41006: "文件超过上限，请压缩后再传",
    41007: "岗位「{x}」不存在，可选：{list}",
    41008: "轮次需在 {low}–{high} 之间，当前是 {n}",
    41009: "风格「{x}」不存在，可选：{list}",
    42001: "请先登录",
    42002: "用户名或密码不对",
    42003: "密码错误次数太多，{n} 分钟后再试",
    42004: "登录已过期，正在自动续期…",
    42005: "登录状态失效，请重新登录",
    42006: "登录凭证无效，请重新登录",
    42007: "这个页面需要管理员权限",
    42008: "你只能查看自己的面试记录",
    42009: "用户名「{x}」已被占用，换一个试试",
    42010: "密码至少 {n} 位",
    42011: "用户名只能包含字母、数字、下划线（3–20 位）",
    42012: "当前部署已关闭自助注册，请联系管理员开通",
    43001: "模型服务暂时连不上，正在用规则兜底评分",
    43002: "模型这次没给出内容，已自动重试",
    43003: "模型输出格式异常，已用兜底逻辑处理",
    43004: "模型响应太慢（超过 {n} 秒），本题用规则评分",
    43005: "模型额度已用完，请联系管理员充值",
    43006: "语义检索暂不可用，已退回关键词匹配",
    43007: "向量索引需要重建（模型维度变了）",
    43008: "服务端语音识别未启用（浏览器原生支持时不需要）",
    43009: "服务端语音合成未启用（浏览器原生支持时不需要）",
    44001: "本次面试已达成本上限（{n} 元），报告仍会生成",
    50001: "数据服务暂时不可用，请稍后重试",
    50002: "数据正在被修改，请重试",
    59999: "服务出了点问题，已记录（追踪号：{rid}）",
}
