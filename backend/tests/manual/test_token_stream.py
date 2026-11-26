"""直接测 _urllib_token_stream：确认手写的流式请求能不能拿到 token。

这个是"真流式"的核心——如果它拿不到 token，整条链路就退化成回放了。
"""

import os
import sys

sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent/backend")
sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent")
os.environ.setdefault("IA_LEGACY_ROOT", "/mnt/e/桌面/实习项目/interview_agent")

import logging
logging.basicConfig(level=logging.INFO)

from app.core.bridge import ensure_legacy_importable
ensure_legacy_importable()

from app.api.sessions import _urllib_token_stream
from llm import build_llm

llm = build_llm(mode="auto")
print(f"LLM: {type(llm).__name__}  is_mock={getattr(llm, 'is_mock', None)}")

cfg = getattr(llm, "cfg", None)
print(f"cfg: {cfg}")
if cfg:
    print(f"  base_url = {cfg.base_url}")
    print(f"  model    = {cfg.model}")
    print(f"  api_key  = {'有' if cfg.api_key else '无'} ({len(cfg.api_key or '')} 字符)")
    print(f"  timeout  = {getattr(cfg, 'timeout', '?')}")

print()
print("=== 测试 _urllib_token_stream ===")
try:
    tokens = list(_urllib_token_stream(cfg))
    print(f"  收到 {len(tokens)} 个 token")
    if tokens:
        print(f"  内容: {''.join(tokens)[:100]}")
        print("  ✓ 真流式可用")
    else:
        print("  ✗ 没有收到 token")
except Exception as e:
    import traceback
    traceback.print_exc()
