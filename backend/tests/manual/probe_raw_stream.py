"""抓 _urllib_token_stream 的原始响应，看每一行到底是什么。"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent/backend")
sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent")
os.environ.setdefault("IA_LEGACY_ROOT", "/mnt/e/桌面/实习项目/interview_agent")

from dotenv import load_dotenv
load_dotenv("/mnt/e/桌面/实习项目/interview_agent/.env")

base = os.environ["LLM_BASE_URL"]
key = os.environ["LLM_API_KEY"]
model = os.environ["LLM_MODEL"]

prompt = "你是技术面试官，请用一句话请候选人做个自我介绍。"

for max_tok in (1200, 200, 60):
    print(f"=== max_tokens={max_tok} ===")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_tok,
        "temperature": 0.7,
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=body, headers=headers, method="POST")
    raw_lines = 0
    data_lines = 0
    pieces = []
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            print(f"  HTTP {resp.status}  CT={resp.headers.get('Content-Type')}")
            for raw in resp:
                raw_lines += 1
                line = raw.decode("utf-8", "ignore").strip()
                if line.startswith("data:"):
                    data_lines += 1
                    d = line[5:].strip()
                    if d == "[DONE]":
                        print("  [DONE]")
                        break
                    try:
                        obj = json.loads(d)
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        c = delta.get("content")
                        r = delta.get("reasoning_content")
                        if c or r:
                            pieces.append(c or r)
                        if raw_lines <= 3:
                            print(f"  delta keys: {list(delta.keys())} "
                                  f"content={str(c)[:20]!r} reasoning={str(r)[:20]!r}")
                    except Exception as e:
                        print(f"  解析失败: {e}")
                if raw_lines > 200:
                    break
    except Exception as e:
        print(f"  {type(e).__name__}: {e}")
        continue
    print(f"  raw 行={raw_lines} data 行={data_lines} 内容片段={len(pieces)}")
    if pieces:
        print(f"  拼起来: {''.join(str(p) for p in pieces)[:80]}")
    print()
