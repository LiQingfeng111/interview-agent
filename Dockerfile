# 面试 Agent（可审计版）——一键起
# 为什么要 Dockerfile：GitHub 上主流同类项目都能 docker run 起来，
# 我们不提供就等于把"想试一下的人"挡在门外（要装 Python、找路径、配模型）。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 只装运行必需的轻依赖：本项目默认零依赖也能跑，
# 这里装的是简历解析/图表/评测需要的可选件，缺失时会优雅降级。
RUN pip install --no-cache-dir pymupdf python-docx openpyxl 2>/dev/null || true

COPY . .

# 非 root 运行：容器里以 root 跑一切是最常见的低成本风险
RUN useradd -m -u 10001 appuser \
    && mkdir -p /app/data /app/runs /app/reports /app/logs /app/docs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8770/api/health',timeout=3)" || exit 1

# 首次启动会自动创建 data/users.db 与默认管理员 admin/admin123（控制台会提示改密）
CMD ["python", "webui.py", "--host", "0.0.0.0", "--port", "8770"]
