"""配置：全部走环境变量，带合理默认值。

## 为什么集中在这里

旧代码的配置散在 `config.py`（419 行）+ 各处 `os.getenv`。
换 FastAPI 后要做一件事：**把所有可变的东西收进一个地方**，
这样 docker compose 里只需要一个 `.env` 文件。

## Ollama 地址三视角问题

同一个 Ollama 服务，三个进程看到的地址不同（实测过）：

| 谁访问 | 地址 | 为什么 |
|---|---|---|
| Windows 脚本 | `127.0.0.1:11800` | 同机 |
| WSL 进程 | `172.22.48.1:11800` | WSL 的 127.0.0.1 是它自己（实测连不通） |
| 容器内 | `host.docker.internal:11800` | compose 里已配 extra_hosts |

所以 `OLLAMA_BASE_URL` 必须由环境注入，不能硬编码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key, "")
    if not raw.strip():
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # ---------------------------------------------------------- 服务
    host: str = field(default_factory=lambda: _env("IA_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("IA_PORT", 8080))
    debug: bool = field(default_factory=lambda: _env("IA_DEBUG", "").lower() in ("1", "true", "yes"))
    cors_origins: list[str] = field(default_factory=lambda: _env_list(
        "IA_CORS_ORIGINS",
        ["http://localhost:5173", "http://127.0.0.1:5173", "http://127.0.0.1:8080"],
    ))

    # ---------------------------------------------------------- 数据库
    # 只支持 PostgreSQL（用户已定），不做 SQLite 双 profile
    database_url: str = field(default_factory=lambda: _env(
        "DATABASE_URL",
        "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent",
    ))
    db_pool_min: int = field(default_factory=lambda: _env_int("IA_DB_POOL_MIN", 2))
    db_pool_max: int = field(default_factory=lambda: _env_int("IA_DB_POOL_MAX", 10))

    # ---------------------------------------------------------- Redis
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://127.0.0.1:6379/0"))
    redis_enabled: bool = field(default_factory=lambda: _env("IA_REDIS", "1") not in ("0", "false", "off"))

    # ---------------------------------------------------------- Embedding
    ollama_base_url: str = field(default_factory=lambda: _env(
        "OLLAMA_BASE_URL", "http://127.0.0.1:11800"))
    embedding_model: str = field(default_factory=lambda: _env("IA_EMBED_MODEL", "bge-m3"))
    embedding_dim: int = field(default_factory=lambda: _env_int("IA_EMBED_DIM", 1024))

    # ---------------------------------------------------------- 模型
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "http://127.0.0.1:18326/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "lobsterai/deepseek-flash"))
    llm_backend: str = field(default_factory=lambda: _env("IA_LLM_BACKEND", "langchain"))
    llm_timeout: int = field(default_factory=lambda: _env_int("IA_LLM_TIMEOUT", 60))
    llm_retries: int = field(default_factory=lambda: _env_int("IA_LLM_RETRIES", 3))

    # ---------------------------------------------------------- 检索阈值
    # 实测标定（见 docs/PRODUCT-PLAN.md §4.1）：
    # bge-m3 分布保守，同义句只有 0.70，正负间隔仅 0.022
    threshold_resume: float = field(default_factory=lambda: _env_float("IA_TH_RESUME", 0.45))
    threshold_bank: float = field(default_factory=lambda: _env_float("IA_TH_BANK", 0.55))
    threshold_history: float = field(default_factory=lambda: _env_float("IA_TH_HISTORY", 0.45))
    threshold_web: float = field(default_factory=lambda: _env_float("IA_TH_WEB", 0.50))

    # ---------------------------------------------------------- 鉴权
    jwt_secret: str = field(default_factory=lambda: _env("IA_JWT_SECRET", ""))
    jwt_algorithm: str = field(default_factory=lambda: "HS256")
    access_token_minutes: int = field(default_factory=lambda: _env_int("IA_ACCESS_MIN", 15))
    refresh_token_days: int = field(default_factory=lambda: _env_int("IA_REFRESH_DAYS", 7))
    allow_registration: bool = field(default_factory=lambda: _env("IA_ALLOW_REGISTRATION", "1") not in ("0", "false", "off"))

    # ---------------------------------------------------------- 面试默认值
    default_max_turns: int = field(default_factory=lambda: _env_int("IA_MAX_TURNS", 16))
    min_turns: int = 6
    max_turns: int = 30
    default_role: str = field(default_factory=lambda: _env("IA_DEFAULT_ROLE", "ai_algo"))

    # ---------------------------------------------------------- 上传
    upload_max_mb: float = field(default_factory=lambda: _env_float("IA_UPLOAD_MAX_MB", 10))
    upload_dir: str = field(default_factory=lambda: _env("IA_UPLOAD_DIR", "data/uploads"))

    # ---------------------------------------------------------- 预算
    max_cost_cny: float = field(default_factory=lambda: _env_float("IA_MAX_COST", 1.0))
    max_tokens: int = field(default_factory=lambda: _env_int("IA_MAX_TOKENS", 200_000))

    # ---------------------------------------------------------- 复用旧核心
    # 旧 core 模块在仓库根目录，通过 sys.path 注入复用（一行不改）
    legacy_root: str = field(default_factory=lambda: _env("IA_LEGACY_ROOT", ""))


settings = Settings()
