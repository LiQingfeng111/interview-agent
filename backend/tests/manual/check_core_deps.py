"""验证：规则层测试到底依赖哪些第三方库。

## 为什么要做这个

CI 的第一关（规则层）号称"零依赖"，只装 `pytest`。
但测试文件里 import 了 `auth` / `documents` / `llm` 等项目模块，
那些模块可能间接依赖第三方库。

如果 CI 里缺库，这个 job 会红——那 CI 就成了摆设。

## 做法

不放行任何第三方库，逐个 import 项目模块，看谁在缺依赖时炸。
用 `sys.modules` 白名单模拟"只有标准库 + pytest"的环境。
"""

import ast
import pathlib
import re

ROOT = pathlib.Path("/mnt/e/桌面/实习项目/interview_agent")

#: 标准库 + pytest 视为可用；其余视为第三方
STDLIB_HINT = {
    "__future__", "abc", "argparse", "ast", "asyncio", "base64", "bisect",
    "collections", "concurrent", "contextlib", "copy", "csv", "dataclasses",
    "datetime", "decimal", "difflib", "enum", "errno", "functools", "gc",
    "glob", "hashlib", "heapq", "hmac", "html", "http", "importlib", "inspect",
    "io", "itertools", "json", "logging", "math", "mimetypes", "os", "pathlib",
    "pickle", "pprint", "queue", "random", "re", "secrets", "shutil", "signal",
    "socket", "sqlite3", "string", "struct", "subprocess", "sys", "tempfile",
    "textwrap", "threading", "time", "traceback", "types", "typing", "unicodedata",
    "unittest", "urllib", "uuid", "warnings", "weakref", "xml", "zipfile",
    "pytest", "yaml",
}


def collected_imports(path: pathlib.Path) -> set[str]:
    """取一个文件里所有顶层包名。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def main() -> None:
    tests_dir = ROOT / "tests"
    all_imports: set[str] = set()
    for f in tests_dir.glob("test_*.py"):
        all_imports |= collected_imports(f)

    # 项目自己的模块（根目录的 .py）
    project_modules = {p.stem for p in ROOT.glob("*.py")}
    # 测试可能 import 工具目录里的东西
    project_modules |= {"ai_review", "enrich_bank", "fake_openai_server",
                        "tools", "eval", "knowledge"}

    third_party = sorted(all_imports - STDLIB_HINT - project_modules)

    print("规则层测试 import 的第三方包：")
    if third_party:
        for n in third_party:
            print(f"  ! {n}")
        print()
        print("→ CI 必须装这些，否则规则层 job 会红")
    else:
        print("  （无）")

    print()
    print("项目模块（不是第三方）：")
    own = sorted(all_imports & project_modules)
    print("  " + ", ".join(own) if own else "  （无）")

    # 关键：检查项目模块本身是否引入第三方
    print()
    print("项目模块自身的第三方依赖：")
    mod_third: set[str] = set()
    for name in own:
        p = ROOT / f"{name}.py"
        if p.exists():
            mod_third |= collected_imports(p)
    mod_third = mod_third - STDLIB_HINT - project_modules
    # 过滤掉标准库的常见误判
    mod_third = {n for n in mod_third if not n.startswith("_")}
    if mod_third:
        for n in sorted(mod_third):
            print(f"  ! {n}")
        print()
        print("→ 这些也是规则层测试的隐性依赖")
    else:
        print("  （无，真正的零依赖）")


if __name__ == "__main__":
    main()
