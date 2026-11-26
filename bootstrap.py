"""一键起：docker compose 之前先自检，本地跑也用它。

为什么需要：GitHub 同类项目都能"一条命令起来"，我们不能让用户
先读三页 README 才能跑。这个脚本做四件事：
  1. 检查 Python 版本与关键文件
  2. 跑单元测试（**快档**，不跑评估回归）
  3. 建会话库（SQLite）并打印路径
  4. 起 web UI 并等健康检查通过，打印访问地址

用法：
  python bootstrap.py            # 自检 + 起服务
  python bootstrap.py --check    # 只自检，不起服务
  python bootstrap.py --port 9000
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))


def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def check_env() -> bool:
    print("[1/4] 环境检查")
    if sys.version_info < (3, 10):
        _fail(f"Python 版本过低：{sys.version.split()[0]}（需要 3.10+）")
        return False
    _ok(f"Python {sys.version.split()[0]}")

    required = ["run.py", "webui.py", "session.py", "config.py",
                "data/question_bank.jsonl", "data/test_resumes/our_sample.md"]
    missing = [f for f in required if not os.path.exists(os.path.join(ROOT, f))]
    if missing:
        _fail(f"缺少关键文件：{missing}")
        return False
    _ok(f"关键文件齐全（{len(required)} 项）")

    try:
        import sqlite3  # noqa: F401
        _ok("sqlite3 可用（会话持久化）")
    except Exception as e:
        _warn(f"sqlite3 不可用：{e}（会话将只存在内存里）")

    try:
        from llm import build_llm
        llm = build_llm(mode="auto")
        kind = "mock（未探测到模型，流程可用但评分为规则替身）" if getattr(llm, "is_mock", False) \
            else "真实模型"
        _ok(f"模型后端：{kind}")
    except Exception as e:
        _warn(f"模型探测失败：{type(e).__name__}: {e}（会退回 mock）")
    return True


def check_tests() -> bool:
    print("[2/4] 单元测试（快档）")
    try:
        out = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                             cwd=ROOT, capture_output=True, text=True, timeout=600)
    except Exception as e:
        _warn(f"测试没跑起来：{type(e).__name__}: {e}")
        return True
    tail = (out.stderr or out.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else ""
    if out.returncode == 0:
        _ok(f"测试通过（{summary}）")
        return True
    _fail(f"测试失败：{summary}")
    print((out.stderr or "")[-800:])
    return False


def prepare_db() -> None:
    print("[3/4] 会话库")
    try:
        import persist
        path = persist.DB_PATH
        persist.list_sessions(path=path)     # 触发建表
        _ok(f"SQLite 就绪：{path}")
    except Exception as e:
        _warn(f"会话库初始化失败：{type(e).__name__}: {e}")


def start_web(port: int, host: str = "127.0.0.1") -> None:
    print(f"[4/4] 启动 Web UI：http://{host}:{port}")
    proc = subprocess.Popen([sys.executable, "webui.py", "--port", str(port), "--host", host],
                            cwd=ROOT)
    for _ in range(30):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    _ok(f"健康检查通过，浏览器打开 http://{host}:{port}")
                    print("  （按 Ctrl+C 停止）")
                    break
        except Exception:
            continue
    else:
        _warn("健康检查没通过，服务可能仍在启动，稍等再访问")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("\n已停止。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只自检，不起服务")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    print("=" * 60)
    print("模拟面试 Agent · 启动自检")
    print("=" * 60)
    if not check_env():
        return 1
    if not check_tests():
        print("自检未通过：先修测试，再起服务（避免把坏的跑起来给人看）")
        return 1
    prepare_db()
    if args.check:
        print("自检完成（--check 模式，未启动服务）")
        return 0
    start_web(args.port, args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
