"""Redis 缓存：会话热态、检索缓存、限流、登录失败计数。

## 为什么需要它（而不是进程内字典）

四个用途，各有硬需求：

| 用途 | 为什么进程内字典不够 |
|---|---|
| 会话热态 | 多 worker 时请求可能打到不同进程 |
| 检索缓存 | 重新向量化 + 查库要 200ms，缓存后 <1ms |
| 限流 | 单进程计数可以被多进程绕过 |
| 登录失败锁定 | 同上，安全相关不能用单进程计数 |

## 降级设计

`IA_REDIS=0` 时自动退化为内存实现（接口完全相同）。
理由：演示环境不想多起一个服务；但代码里不能出现两套分支。

内存实现**只用于单进程**，测试和多 worker 场景必须用真 Redis。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.settings import settings

log = logging.getLogger("interview.cache")

_client: Any = None


# ---------------------------------------------------------------- 接口
class CacheBackend:
    """统一接口。Redis 与内存实现都满足它。"""

    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def incr(self, key: str, ttl: int | None = None) -> int: ...
    def get_json(self, key: str) -> Any: ...
    def set_json(self, key: str, value: Any, ttl: int | None = None) -> None: ...
    def health(self) -> dict[str, Any]: ...


class MemoryCache(CacheBackend):
    """进程内实现。带 TTL 检查。"""

    def __init__(self):
        self._data: dict[str, tuple[str, float | None]] = {}

    def _alive(self, key: str) -> bool:
        item = self._data.get(key)
        if item is None:
            return False
        _, exp = item
        if exp is not None and exp < time.time():
            self._data.pop(key, None)
            return False
        return True

    def get(self, key: str) -> str | None:
        return self._data[key][0] if self._alive(key) else None

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self._data[key] = (value, time.time() + ttl if ttl else None)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def incr(self, key: str, ttl: int | None = None) -> int:
        cur = int(self.get(key) or 0) + 1
        remain = None
        if self._alive(key):
            _, exp = self._data[key]
            remain = int(exp - time.time()) if exp else None
        self.set(key, str(cur), remain if remain else ttl)
        return cur

    def get_json(self, key: str) -> Any:
        raw = self.get(key)
        return json.loads(raw) if raw else None

    def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.set(key, json.dumps(value, ensure_ascii=False), ttl)

    def health(self) -> dict[str, Any]:
        # 顺手清理过期项，避免长时间运行内存涨
        now = time.time()
        for k in [k for k, (_, e) in self._data.items() if e and e < now]:
            self._data.pop(k, None)
        return {"ok": True, "backend": "memory", "keys": len(self._data)}


class RedisCache(CacheBackend):
    def __init__(self, url: str):
        import redis
        self.r = redis.Redis.from_url(url, decode_responses=True,
                                      socket_connect_timeout=5, socket_timeout=5)
        self.r.ping()

    def get(self, key: str) -> str | None:
        return self.r.get(key)

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self.r.set(key, value, ex=ttl)

    def delete(self, key: str) -> None:
        self.r.delete(key)

    def incr(self, key: str, ttl: int | None = None) -> int:
        n = self.r.incr(key)
        if n == 1 and ttl:
            self.r.expire(key, ttl)
        return int(n)

    def get_json(self, key: str) -> Any:
        raw = self.get(key)
        return json.loads(raw) if raw else None

    def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.set(key, json.dumps(value, ensure_ascii=False), ttl)

    def health(self) -> dict[str, Any]:
        try:
            info = self.r.info("memory")
            return {"ok": True, "backend": "redis",
                    "used_memory_mb": round(info.get("used_memory", 0) / 1024 / 1024, 1)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "backend": "redis", "error": str(e)}


# ---------------------------------------------------------------- 单例
def get_cache() -> CacheBackend:
    global _client
    if _client is not None:
        return _client

    if settings.redis_enabled:
        try:
            _client = RedisCache(settings.redis_url)
            log.info("Redis 已连接：%s", settings.redis_url)
            return _client
        except Exception as e:  # noqa: BLE001
            log.warning("Redis 不可用（%s），退化为内存缓存", e)

    _client = MemoryCache()
    log.info("使用内存缓存（单进程）")
    return _client


def cache_health() -> dict[str, Any]:
    try:
        return get_cache().health()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 便捷封装
def session_cache_key(sid: str) -> str:
    return f"ia:sess:{sid}"


def rag_cache_key(index: str, query: str) -> str:
    import hashlib
    h = hashlib.sha1(f"{index}|{query}".encode()).hexdigest()[:16]
    return f"ia:rag:{index}:{h}"


def web_cache_key(query: str) -> str:
    import hashlib
    h = hashlib.sha1(query.encode()).hexdigest()[:16]
    return f"ia:web:{h}"
