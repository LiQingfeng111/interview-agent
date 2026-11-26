"""OpenAI 兼容的假服务：用于验证客户端链路（不发真实模型请求）。

为什么需要它：
  - 测试和 CI 不该依赖真模型（慢、要网、要 Key）
  - "跑起来没反应、没日志"这类问题，必须能在不接真模型时复现和验证
  - 它能同时提供 /v1/models 和 /v1/chat/completions，正好覆盖
    discover_models() 与 OpenAICompatibleLLM.chat() 两条路径

用法：
    python tools/fake_openai_server.py --port 8088
    # 另开一个窗口
    python run.py --demo --llm local --base-url http://127.0.0.1:8088/v1 --show-llm
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_NAME = "fake-qwen-test"

_ANSWERS = {
    "interviewer": "这个项目最初要解决什么问题？为什么当时必须做？",
    "evaluator": json.dumps(
        {
            "accuracy": 3, "depth": 3, "tradeoff": 2, "boundary": 2, "clarity": 3,
            "verdict": "partial", "weakest": "boundary",
            "evidence": "回答了但没给对照基线",
            "followup_hint": "追问 baseline 是什么",
            "covered": ["目标"],
            "target_status": "evaded",
        },
        ensure_ascii=False,
    ),
    "coach": "【总评】能讲清做了什么，但缺对照与失败复盘。\n【最深卡点】取舍层",
    "planner": json.dumps(
        {"projects": [{"name": "项目A", "focus": [
            {"element": "目标", "seed": "这个项目要解决什么问题？"},
            {"element": "baseline", "seed": "对比基线是什么？"},
            {"element": "指标", "seed": "指标怎么测的？"},
        ]}]},
        ensure_ascii=False,
    ),
    "resume": json.dumps({"projects": []}, ensure_ascii=False),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默，避免刷屏
        pass

    def _send(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self._send({"object": "list", "data": [{"id": MODEL_NAME, "object": "model"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(length) or b"{}")
        messages = req.get("messages") or []
        system = ""
        for m in messages:
            if m.get("role") == "system":
                system = str(m.get("content", ""))
                break
        # 按 system prompt 猜任务，让不同角色拿到对应格式的回复。
        # 顺序很重要：提问官的 system prompt 里也含"简历"二字，
        # 若先匹配"简历"会把提问官误判成简历解析（实测踩过）。
        task = "interviewer"
        for key, marker in (
            ("interviewer", "技术面试官"),
            ("coach", "面试教练"),
            ("evaluator", "评分"),
            ("planner", "面试计划生成器"),
            ("resume", "简历解析"),
        ):
            if marker in system:
                task = key
                break
        self._send(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "model": req.get("model", MODEL_NAME),
                "choices": [
                    {"index": 0, "message": {"role": "assistant",
                                             "content": _ANSWERS.get(task, "请继续。")},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            }
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8088)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"fake OpenAI server on http://127.0.0.1:{args.port}/v1  (model={MODEL_NAME})")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
