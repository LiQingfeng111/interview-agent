"""扫描硬截断，并按"用户会不会读到"分类。

## 为什么要分类

不是所有 `[:N]` 都要改成 `_smart_truncate`：

  · **展示给用户的**（报告、API 响应）→ 必须按句子边界，
    半句话比少说一句更糟
  · **塞给模型的 prompt** → 硬切可以，模型能理解被截断的上下文，
    而且 token 预算要严格控制
  · **日志 / 去重键** → 硬切最合适，越短越好

一刀切全改反而会让 prompt 变长、日志变啰嗦。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path("/mnt/e/桌面/实习项目/interview_agent/backend/app")

#: 这些位置的截断需要按句子边界（用户会读到）
USER_FACING = {
    "core/service.py": "报告 markdown（用户直接看）",
    "api/sessions.py": "SSE 流式内容（用户看到出题过程）",
}

#: 这些位置的硬切是刻意的
INTERNAL_OK = {
    "api/diagnostics.py": "诊断接口返回的比对样本（固定 40 字便于并排看）",
    "api/speech.py": "TTS 输入上限（音频时长控制）",
    "rag/bank_select.py": "日志 + prompt 片段（token 预算）",
    "graph/interview_graph.py": "日志 + prompt 片段（token 预算）",
    "core/runtime.py": "评分 prompt 里的简历片段（token 预算）",
}

pat = re.compile(r"\[:(\d{2,})\]")

print("=" * 68)
print("硬截断清单（按是否面向用户分类）")
print("=" * 68)

for f in sorted(ROOT.rglob("*.py")):
    if "__pycache__" in str(f):
        continue
    rel = str(f.relative_to(ROOT)).replace("\\", "/")
    try:
        lines = f.read_text(encoding="utf-8").split("\n")
    except Exception:
        continue

    hits = []
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("#") or line.lstrip().startswith('"'):
            continue
        for m in pat.finditer(line):
            n = int(m.group(1))
            if n < 20:          # 忽略 [:20] 这类短截断（多半是 id/hex）
                continue
            hits.append((i, n, line.strip()[:70]))

    if not hits:
        continue

    tag = USER_FACING.get(rel)
    if tag:
        mark = "需改"
        note = tag
    elif rel in INTERNAL_OK:
        mark = "保留"
        note = INTERNAL_OK[rel]
    else:
        mark = "待定"
        note = ""

    print(f"\n{mark}  {rel}")
    if note:
        print(f"       {note}")
    for ln, n, txt in hits:
        print(f"       L{ln}: [:{n}]  {txt}")

print()
print("=" * 68)
print("结论")
print("=" * 68)
print("  需改的已改成 _smart_truncate（core/service.py）")
print("  保留的是刻意的硬切，见上面说明")
