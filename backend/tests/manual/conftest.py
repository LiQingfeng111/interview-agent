"""屏蔽 manual/ 目录下的诊断脚本，不让 pytest 收集。

## 为什么需要这个文件

`backend/tests/manual/` 里放的是**诊断脚本**，不是单元测试：
- 它们在模块级就 `build_llm()`，需要真实的 LLM_API_KEY
- 有的还要连 Ollama / relay / Docker
- 有的文件名以 `test_` 开头（习惯问题），会被 pytest 误收集

误收集的后果很严重：整个测试收集阶段直接失败（ERROR），
`pytest backend/tests` 一条测试都跑不了。

`pytest.ini` 的 `norecursedirs` 对**显式指定的 testpaths** 不生效，
所以在目录里放一个 `conftest.py` 并设置 `collect_ignore`，这是官方支持的屏蔽方式。

## 怎么跑这些脚本

单独执行，例如：

    PYTHONPATH='backend:.' .venv/bin/python backend/tests/manual/probe_relay_stream.py
"""

collect_ignore_glob = [
    "*.py",
]
