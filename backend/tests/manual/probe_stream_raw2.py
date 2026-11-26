"""把流式响应的每一行原样打印出来（不解析）。"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent")
from dotenv import load_dotenv
load_dotenv("/mnt/e/桌面/实习项目/interview_agent/.env")

base = os.environ["LLM_BASE_URL"]
key = os.environ["LLM_API_KEY"]
model = os.environ["LLM_MODEL"]

print("=== A) 非流式（对照）===")
payload = {
    "model": model,
    "messages": [{"role": "user", "content": "说三个字：你好呀"}],
    "max_tokens": 50,
}
body = json.dumps(payload, ensure_ascii=False).encode()
req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                             headers={"Content-Type": "application/json",
                                      "Authorization": f"Bearer {key}"},
                             method="POST")
with urllib.request.urlopen(req, timeout=60) as r:
    obj = json.loads(r.read())
    ch = (obj.get("choices") or [{}])[0]
    print(f"  finish_reason={ch.get('finish_reason')}")
    msg = ch.get("message") or {}
    print(f"  content={str(msg.get('content'))[:60]!r}")
    print(f"  reasoning_content={str(msg.get('reasoning_content'))[:60]!r}")
    print(f"  usage={obj.get('usage')}")

print()
print("=== B) 流式：原样打印每一行 ===")
payload["stream"] = True
body = json.dumps(payload, ensure_ascii=False).encode()
req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                             headers={"Content-Type": "application/json",
                                      "Authorization": f"Bearer {key}"},
                             method="POST")
with urllib.request.urlopen(req, timeout=90) as r:
    n = 0
    for raw in r:
        n += 1
        line = raw.decode("utf-8", "ignore").rstrip()
        print(f"  [{n}] {line[:300]}")
        if n >= 10:
            print("  ... (截断)")
            break
