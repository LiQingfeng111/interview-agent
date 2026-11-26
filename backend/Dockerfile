# 后端镜像：FastAPI + 旧核心规则层 + RAG
#
# 为什么要 COPY 整个仓库而不是只 COPY backend/：
# backend 通过 sys.path 复用根目录的旧核心（state_machine / probe_tree /
# invariants / ledger / 722 题题库），那些文件在 /app 下。
# 这是"规则层零重写"的必然结果——镜像里必须同时有两部分。

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend:/app

WORKDIR /app

# 系统依赖：
# - curl：健康检查用
# - libpq5：psycopg 运行时
# - pymupdf 需要的基础库（PDF 解析）
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl libpq5 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用缓存层：源码改动不会重装依赖）
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir langgraph langgraph-checkpoint-postgres

# 再拷代码
COPY backend/ /app/backend/
COPY *.py /app/

# 题库（722 题）
COPY data/question_bank.jsonl /app/data/question_bank.jsonl

# 上传目录
RUN mkdir -p /app/data/uploads /app/logs

EXPOSE 8080

# 用 1 个 worker：会话热态在进程内存（Postgres checkpointer 负责持久化），
# 多 worker 时请求可能打到没有热态的进程 → 会走恢复路径（慢但正确）。
# 要横向扩展时把热态换成 Redis 缓存即可。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--app-dir", "/app/backend"]
