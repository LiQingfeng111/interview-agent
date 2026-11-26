"""签名密钥的并发安全与边界测试。

背景：全量测试跑两遍才复现的一次失败（`test_roundtrip` 偶发返回 None），
根因是 `auth._secret()` 的"先检查再创建"竞争：两个调用同时判断文件不存在，
各自生成密钥并互相覆盖，于是先签发的票验不过。

这三条用例分别锁住：
1. 并发首次创建 → 所有线程拿到同一把密钥，签的票互相能验
2. 空文件 → 视为不可用并重新生成（不能退化成空密钥签名）
3. 已存在的合法密钥 → 保持不变（不能每次调用都换密钥，否则历史票全失效）
"""

from __future__ import annotations

import os
import threading

import auth


def test_concurrent_creation_is_consistent(tmp_path):
    path = str(tmp_path / "secret.bin")
    results: list[bytes] = []
    lock = threading.Lock()

    def worker():
        s = auth._secret(path)
        with lock:
            results.append(s)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 16
    assert len({r for r in results}) == 1, "并发创建必须收敛到同一把密钥"
    assert len(results[0]) >= auth.MIN_SECRET_BYTES

    # 用并发里拿到的密钥签票，再用磁盘上的密钥验票，必须通过
    token = auth.make_token("alice", "user", path=path)
    assert auth.parse_token(token, path=path) == {"user": "alice", "role": "user"}


def test_empty_file_is_regenerated(tmp_path):
    path = str(tmp_path / "secret.bin")
    with open(path, "wb") as f:
        f.write(b"")                      # 半途被创建但没写成功
    secret = auth._secret(path)
    assert len(secret) >= auth.MIN_SECRET_BYTES, "空密钥必须被视为不可用"

    token = auth.make_token("bob", "admin", path=path)
    assert auth.parse_token(token, path=path) == {"user": "bob", "role": "admin"}


def test_existing_secret_is_stable(tmp_path):
    path = str(tmp_path / "secret.bin")
    first = auth._secret(path)
    second = auth._secret(path)
    assert first == second, "密钥一旦落盘就必须稳定，否则历史票全部失效"
    assert os.path.getsize(path) >= auth.MIN_SECRET_BYTES
