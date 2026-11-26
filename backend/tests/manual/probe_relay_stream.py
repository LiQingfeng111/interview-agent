"""测 relay 的流式支持：直接发 stream:true 请求，看返回什么。

不复用项目代码，纯粹验证"这个 relay 到底支不支持 SSE 流式"。
"""

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent/backend")
sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent")
os.environ.setdefault("IA_LEGACY_ROOT", "/mnt/e/桌面/实习项目/interview_agent")

from dotenv import load_dotenv
load_dotenv("/mnt/e/桌面/实习项目/interview_agent/.env")

base = os.environ.get("LLM_BASE_URL", "")
key = os.environ.get("LLM_API_KEY", "")
model = os.environ.get("LLM_MODEL", "")

print(f"base  = {base}")
print(f"model = {model}")
print(f"key   = {'有' if key else '无'} ({len(key)} 字符)")
print()

url = base.rstrip("/") + "/chat/completions"
payload = {
    "model": model,
    "messages": [{"role": "user", "content": "说三个字：你好呀"}],
    "stream": True,
    "max_tokens": 30,
}
body = json.dumps(payload, ensure_ascii=False).encode()
headers = {"Content-Type": "application/json"}
if key:
    headers["Authorization"] = f"Bearer {key}"

print("=== 发送 stream:true 请求 ===")
req = urllib.request.Request(url, data=body, headers=headers, method="POST")
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(f"  HTTP {resp.status}")
        print(f"  Content-Type: {resp.headers.get('Content-Type')}")
        n = 0
        lines = []
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if line:
                lines.append(line)
                n += 1
            if n >= 8:
                break
        print(f"  收到 {n} 行")
        for line in lines[:6]:
            print(f"    {line[:110]}")
        if n > 0 and any("data:" in x for x in lines):
            print("  ✓ relay 支持流式")
        else:
            print("  ✗ 返回的不是 SSE 格式")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}")
    print(f"  响应体: {e.read()[:400]}")
except Exception as e:
    print(f"  {type(e).__name__}: {e}")
