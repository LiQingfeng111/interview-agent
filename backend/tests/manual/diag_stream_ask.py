"""诊断 /stream/ask 为什么拿不到 token。

在真实运行时上下文里调 _token_stream，打印每一步。
"""

import os
import sys

sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent/backend")
sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent")
os.environ.setdefault("DATABASE_URL", "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent")
os.environ.setdefault("OLLAMA_BASE_URL", "http://172.22.48.1:11800")
os.environ.setdefault("IA_LEGACY_ROOT", "/mnt/e/桌面/实习项目/interview_agent")
os.environ.setdefault("IA_REDIS", "0")

from dotenv import load_dotenv
load_dotenv("/mnt/e/桌面/实习项目/interview_agent/.env")

import logging
logging.basicConfig(level=logging.WARNING)

from app.core.bridge import ensure_legacy_importable
ensure_legacy_importable()

from app.api import sessions as sess_mod
from app.core.runtime import SessionRuntime
from app.core import service


class Req:
    resume = (
        "姓名：测试\n项目一：课程问答助手\n做法：RAG，切块500字\n结果：召回从71%到82%\n"
    )
    jd = ""
    role = "ai_algo"
    persona = "balanced"
    max_turns = 6
    web = False
    mock = False
    coach = False
    document_id = ""


SID = "s_stream_diag"
rt = SessionRuntime(SID, "stream_diag_user", Req())
from app.core import registry
registry.register(SID, rt)

print("=== 1) 先出第一问 ===")
q = rt.next_question()
print(f"  问题: {str(q)[:60]}")

print()
print("=== 2) pending_question 返回什么 ===")
pq = service.pending_question(SID)
print(f"  {pq!r}")

print()
print("=== 3) _token_stream 解包链路 ===")
session = getattr(rt, "_session", None)
print(f"  session: {type(session).__name__ if session else None}")
llm = getattr(session, "llm", None) if session else None
print(f"  llm: {type(llm).__name__ if llm else None}")
inner = getattr(llm, "_inner", llm) if llm else None
print(f"  inner: {type(inner).__name__ if inner else None}")
cfg = getattr(inner, "cfg", None) if inner else None
print(f"  cfg: {cfg}")
if cfg:
    print(f"    base_url={cfg.base_url}")
    print(f"    model={cfg.model}")
    print(f"    api_key={'有' if cfg.api_key else '无'}")
    print(f"    max_tokens={getattr(cfg, 'max_tokens', '?')}")
    print(f"    temperature={getattr(cfg, 'temperature', '?')}")

print()
print("=== 4) 直接调 _urllib_token_stream，用真实 prompt ===")
prompt = pq or "你是技术面试官，请用一句话请候选人做个自我介绍。"
print(f"  prompt: {prompt[:80]!r}")
try:
    tokens = list(sess_mod._urllib_token_stream(cfg, prompt))
    print(f"  token 数: {len(tokens)}")
    if tokens:
        print(f"  内容: {''.join(tokens)[:150]}")
    else:
        print("  ✗ 零 token")
except Exception as e:
    import traceback
    traceback.print_exc()

print()
print("=== 5) 调 _token_stream（走完整链路）===")
try:
    ts = sess_mod._token_stream(rt)
    if ts is None:
        print("  ✗ 返回 None（会退回回放）")
    else:
        toks = list(ts)
        print(f"  token 数: {len(toks)}")
        if toks:
            print(f"  内容: {''.join(toks)[:150]}")
except Exception as e:
    import traceback
    traceback.print_exc()

registry.unregister(SID)
