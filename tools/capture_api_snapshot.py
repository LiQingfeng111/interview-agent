"""录制现有 23 个接口的真实响应，作为重构的黄金快照。

## 为什么必须先做这件事

接下来要把 `webui.py`（标准库 http.server）换成 FastAPI，再把前端换成 Vue。
这个过程中最容易出的问题不是"报错"，而是**响应结构悄悄变了**：
少一个字段、多一层嵌套、某个数字从 int 变成 str —— 前端不会崩，
但行为已经和旧版不同，而这种差异在单元测试里完全看不见。

所以顺序必须是：**先录快照 → 再写新实现 → 逐字段比对 → 才敢删旧代码**。
这和之前做提示词模板化时"先拍 9 条 golden"是同一个方法。

## 快照要处理的三类不确定性

1. **时间戳**：`created_at`、`ts`、`updated_at` 每次都变 → 归一化成 `<TIME>`
2. **会话 id**：`s_17955912080441113` 每次不同 → 归一化成 `<SID>`
3. **模型输出**：出题、评分是随机的 → 用 mock 模式跑，保证确定性

如果不用 mock，同一个接口录两次结果就不同，快照就没有比对价值。

## 用法

    python tools/capture_api_snapshot.py            # 起服务 → 录快照 → 停服务
    python tools/capture_api_snapshot.py --url http://127.0.0.1:8770
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "golden" / "api_snapshot.json"

# 归一化规则：把每次都变的东西替换成占位符
NORMALIZE = [
    (re.compile(r"s_\d{10,}"), "<SID>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?"), "<TIME>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "<DATE>"),
    (re.compile(r"\d{10,}\.?\d*"), "<NUM>"),
    (re.compile(r"[0-9a-f]{16,}"), "<HASH>"),
]


def normalize(obj):
    """递归归一化：字符串做正则替换，数字里的时间戳也处理。"""
    if isinstance(obj, dict):
        return {k: normalize(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    if isinstance(obj, str):
        s = obj
        for pat, rep in NORMALIZE:
            s = pat.sub(rep, s)
        return s
    if isinstance(obj, float):
        # 浮点只保留 4 位，避免末位抖动
        return round(obj, 4)
    return obj


def call(base: str, path: str, method: str = "GET", payload: dict | None = None,
         cookie: str = "") -> dict:
    url = base.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode("utf-8", "ignore")
            set_cookie = r.headers.get("Set-Cookie") or ""
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {"_raw": body[:400]}
            return {"status": r.status, "body": parsed, "_set_cookie": bool(set_cookie)}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"_raw": body[:400]}
        return {"status": e.code, "body": parsed, "_set_cookie": False}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "error": f"{type(e).__name__}: {e}"}


def wait_ready(base: str, timeout: float = 40) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = call(base, "/api/health")
        if r.get("status") == 200:
            return True
        time.sleep(0.8)
    return False


def capture(base: str) -> dict:
    out: dict[str, dict] = {}

    # ---- 无需登录的接口 ----
    out["health"] = call(base, "/api/health")
    out["meta"] = call(base, "/api/meta")
    out["bank"] = call(base, "/api/bank")
    out["registration"] = call(base, "/api/registration")

    # ---- 未登录访问受保护接口：记录 401/403 行为 ----
    for name, path in [("me_anon", "/api/me"), ("history_anon", "/api/history"),
                       ("report_anon", "/api/report"), ("admin_anon", "/api/admin")]:
        out[name] = call(base, path)

    # ---- 注册 + 登录（mock 模式，避免调模型）----
    # 密码必须 ≥6 位（旧实现有这个校验，第一次录快照时踩到）
    out["register_short_pwd"] = call(base, "/api/register", "POST",
                                     {"user": "snap_user", "password": "123"})
    out["register"] = call(base, "/api/register", "POST",
                           {"user": "snap_user", "password": "snap_pwd_123"})
    out["login_wrong_pwd"] = call(base, "/api/login", "POST",
                                  {"user": "snap_user", "password": "wrong_pwd"})
    login = call(base, "/api/login", "POST", {"user": "snap_user", "password": "snap_pwd_123"})
    out["login"] = login
    out["me"] = call(base, "/api/me")

    # ---- 开一场面试（mock 模式，保证确定性）----
    # 简历必须能被解析出"项目"，否则 /api/start 直接报错。
    # 第一次录快照用的简历太短（没写项目结构），这次补全。
    resume = (
        "姓名：张三  求职方向：AI 应用开发\n"
        "项目一：课程问答助手（2026.03 - 2026.09）\n"
        "  背景：同学找课程答案要翻 300 份 PDF，平均 8 分钟。\n"
        "  做法：RAG 检索，切块 500 字，top20 重排。\n"
        "  结果：召回从 71% 提到 82%，P99 延迟 1.8 秒。\n"
        "  代价：索引体积增加 30%。\n"
    )
    start = call(base, "/api/start", "POST", {
        "resume": resume,
        "role": "ai_algo", "persona": "balanced", "max_turns": 4,
        "web": False, "mock": True, "coach": True,
    })
    out["start"] = start
    sid = ""
    if isinstance(start.get("body"), dict):
        sid = str(start["body"].get("sid") or "")

    if sid:
        out["answer"] = call(base, "/api/answer", "POST",
                             {"sid": sid, "answer": "召回从 71% 提到 82%，300/500/800 三档实测过。"})
        out["end"] = call(base, "/api/end", "POST", {"sid": sid})
        out["report"] = call(base, f"/api/report?sid={sid}")
        out["history"] = call(base, "/api/history")
        out["trend"] = call(base, "/api/trend")
        out["sessions"] = call(base, "/api/sessions")

    # ---- 后台接口 ----
    out["users"] = call(base, "/api/users")
    out["admin"] = call(base, "/api/admin")

    # ---- 异常路径 ----
    # 岗位名不合法时旧实现会抛 500（内部异常）；这里如实记录，不掩盖
    out["start_bad_role"] = call(base, "/api/start", "POST", {"role": "不存在的岗位"})
    out["answer_no_sid"] = call(base, "/api/answer", "POST", {"answer": "x"})
    out["answer_empty"] = call(base, "/api/answer", "POST", {"sid": sid, "answer": ""})
    out["notfound"] = call(base, "/api/不存在")

    out["logout"] = call(base, "/api/logout", "POST", {})

    return {k: {"status": v.get("status"), "body": normalize(v.get("body", v.get("error")))}
            for k, v in out.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--no-start", action="store_true", help="不自己起服务，直接打 --url")
    args = ap.parse_args()

    proc = None
    base = args.url
    if not base:
        base = "http://127.0.0.1:8791"
        env = dict(os.environ)
        env["IA_LLM_BACKEND"] = "native"
        print(f"启动旧 webui（mock 模式）: {base}")
        proc = subprocess.Popen(
            [sys.executable, "webui.py", "--port", "8791", "--auth", "off"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not wait_ready(base):
            print("服务没起来，放弃")
            if proc:
                proc.terminate()
            return 1

    try:
        snap = capture(base)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n已录 {len(snap)} 个接口 → {dest}")
    for k, v in snap.items():
        body = v.get("body")
        shape = ""
        if isinstance(body, dict):
            shape = "{" + ", ".join(list(body.keys())[:6]) + ("…" if len(body) > 6 else "") + "}"
        elif isinstance(body, list):
            shape = f"[{len(body)} 项]"
        print(f"  {k:<18} status={v.get('status')}  {shape}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
