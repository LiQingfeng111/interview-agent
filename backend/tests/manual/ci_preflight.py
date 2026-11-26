"""CI / 部署前检查：建表 + 启动自检。

## 为什么要单独一个脚本，而不是在 CI 里写 `python -c`

`python -c "..."` 嵌在 YAML 里要处理三层转义（YAML / shell / Python），
实测很容易写错，而且报错信息难读。

抽成脚本后：
- CI 里只有一行 `python backend/tests/manual/ci_preflight.py`
- 本地也能直接跑，行为和 CI 完全一致

## 这个脚本做什么

1. `init_schema()` —— 建表（幂等，重复跑安全）
2. `selfcheck.run(strict=True)` —— 检查 36 个关键模块能否 import
   + 数据库结构是否完整

第 2 步比第 1 步更有价值：它抓的是"模块缺失被 except 吞掉"
这类隐性故障（踩过：documents 模块缺失，表现为"上传成功但开场失败"）。

## 用法

    PYTHONPATH=backend DATABASE_URL=... python backend/tests/manual/ci_preflight.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能直接跑（CI 里不设 PYTHONPATH 时也能工作）
#
# 路径推导（容易数错，写清楚）：
#   __file__          backend/tests/manual/ci_preflight.py
#   parents[0]        backend/tests/manual
#   parents[1]        backend/tests
#   parents[2]        backend          ← 要让 `import app` 生效的是这个
#
# 第一版写了 parents[2] / "backend"，变成 backend/backend，所以
# `ModuleNotFoundError: No module named 'app'`。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    ok = True

    print("=== 1/2 建表（幂等）===")
    try:
        from app.repos.pg import init_schema

        result = init_schema()
        print(f"  ✓ 执行 {result['statements']} 条语句")
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 建表失败：{type(e).__name__}: {e}")
        ok = False

    print()
    print("=== 2/2 启动自检 ===")
    try:
        from app.core import selfcheck

        result = selfcheck.run(strict=True)
        print(f"  ✓ {result['checked_modules']} 个模块 + 数据库结构")

        # 题库状态是信息性的，不阻断
        try:
            from app.repos.pg import get_conn
            with get_conn() as c:
                cur = c.cursor()
                cur.execute("SELECT COUNT(*), COUNT(embedding) FROM bank_questions")
                total, vec = cur.fetchone()
                cur.close()
            print(f"  题库：{total} 题（已向量化 {vec}）")
            if total and not vec:
                print("  ! 题库未向量化 —— 语义检索会返回空（CI 里没跑 Ollama 是正常的）")
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 自检失败：{type(e).__name__}: {e}")
        ok = False

    print()
    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
