"""查清：谁在失败，是 skip 判定失效还是别的。

在无 Ollama 环境下（模拟 CI），逐个看失败测试的 skip 条件。
"""

import os
import sys
from pathlib import Path

ROOT = Path("/mnt/e/桌面/实习项目/interview_agent")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ["DATABASE_URL"] = "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent"
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:1"   # 故意不可达
os.environ["IA_REDIS"] = "0"
os.environ["IA_JWT_SECRET"] = "x" * 32

import logging
logging.basicConfig(level=logging.CRITICAL)

print("=" * 70)
print("诊断：无 Ollama 时各测试文件的 skip 判定")
print("=" * 70)

# 1. 检查 embedder 是否真的会失败
print("\n[1] embedder 在 OLLAMA_BASE_URL=http://127.0.0.1:1 时")
try:
    from app.rag.embedder import get_embedder
    emb = get_embedder()
    v = emb.embed(["测试"])
    print(f"  ! 竟然成功了，维度 {len(v[0])}")
except Exception as e:
    print(f"  ✓ 失败（符合预期）: {type(e).__name__}")

# 2. 各测试文件的 _ready 判定
print("\n[2] 各测试模块的 skip 条件")


def check_ready(name, fn):
    try:
        ok = fn()
        print(f"  {name:<28} _ready() = {ok}  → {'运行' if ok else '跳过'}")
    except Exception as e:
        print(f"  {name:<28} _ready() 抛异常: {type(e).__name__}: {e}")


def ready_bank_select():
    from app.repos.pg import get_conn
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM bank_questions WHERE embedding IS NOT NULL")
        n = cur.fetchone()[0]
        cur.close()
    return int(n) >= 100


def ready_topic_filter():
    from app.repos.pg import get_conn
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM bank_questions WHERE embedding IS NOT NULL")
        n = cur.fetchone()[0]
        cur.close()
    if int(n) < 100:
        return False
    try:
        from app.rag.embedder import get_embedder
        get_embedder().embed(["探测"])
        return True
    except Exception:
        return False


check_ready("test_bank_select", ready_bank_select)
check_ready("test_topic_filter（我的）", ready_topic_filter)

# 3. 看 test_retriever 有没有 skip 条件
print("\n[3] test_retriever.py / test_quota.py 的 skip 条件")
for f in ("test_retriever.py", "test_quota.py"):
    p = ROOT / "backend" / "tests" / f
    if not p.exists():
        print(f"  {f}: 不存在")
        continue
    txt = p.read_text(encoding="utf-8")
    has_skip = "skipif" in txt or "pytestmark" in txt
    print(f"  {f}: {'有 skip 条件' if has_skip else '✗ 没有 skip 条件'}")
    # 打印前几行 skip 相关代码
    for i, line in enumerate(txt.split("\n")[:50], 1):
        if "skipif" in line or "pytestmark" in line or "_ready" in line:
            print(f"      L{i}: {line.strip()}")

print("\n" + "=" * 70)
print("结论")
print("=" * 70)
print("  如果 test_retriever / test_quota 没有 skip 条件，")
print("  那它们在上次 CI（120 passed, 21 skipped）里能过，")
print("  说明当时 env 里有 Ollama —— 但我刚看到 CI 配置是 OLLAMA_BASE_URL=http://127.0.0.1:1。")
print()
print("  矛盾点必须查清：到底 CI 里这些测试是 pass 还是 skip。")
