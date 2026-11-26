"""LLM 客户端链路测试：HTTP 调用、模型发现、日志落盘。

为什么要有这个测试：
  "跑起来没反应、没日志"是最难自查的一类问题——到底是连不上、模型没回、
  还是回了被吞了？这套测试用进程内的假服务把这些环节拆开验证，
  不依赖真模型、不联网、不需要 Key。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fake_openai_server as fake  # noqa: E402
from llm import (  # noqa: E402
    LLMLogger,
    MockLLM,
    OpenAICompatibleLLM,
    build_llm,
    discover_models,
    find_local_llm,
)
from config import LLMConfig  # noqa: E402


class FakeServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), fake.Handler)
        cls.port = cls.srv.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}/v1"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()


class TestDiscover(FakeServerMixin, unittest.TestCase):
    def test_discover_models(self):
        models = discover_models(self.base_url)
        self.assertEqual(models, [fake.MODEL_NAME])

    def test_discover_fails_gracefully(self):
        # 关闭的端口必须返回空列表，不能抛异常
        self.assertEqual(discover_models("http://127.0.0.1:1/v1", timeout=0.5), [])

    def test_find_local_llm_returns_none_when_nothing_running(self):
        """把探测目标换成一个必然关着的端口，避免"本机碰巧有中继在跑"导致随机失败。"""
        import llm as llm_mod

        old_candidates = getattr(llm_mod, "LOCAL_CANDIDATES", None)
        old_relay = getattr(llm_mod, "relay_api_key", None)
        llm_mod.LOCAL_CANDIDATES = [("dead", "http://127.0.0.1:1/v1", "none")]
        llm_mod.relay_api_key = lambda: ""
        try:
            self.assertIsNone(find_local_llm(timeout=0.3))
        finally:
            if old_candidates is not None:
                llm_mod.LOCAL_CANDIDATES = old_candidates
            if old_relay is not None:
                llm_mod.relay_api_key = old_relay


class TestChat(FakeServerMixin, unittest.TestCase):
    def _client(self, log_dir):
        cfg = LLMConfig(base_url=self.base_url, model=fake.MODEL_NAME,
                        api_key="", requires_key=False)
        return OpenAICompatibleLLM(cfg, logger=LLMLogger(echo=False, log_dir=log_dir))

    def test_chat_returns_content(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._client(d).chat(
                [{"role": "system", "content": "你是技术面试官"},
                 {"role": "user", "content": "问一个问题"}],
                task="interviewer",
            )
            self.assertTrue(out.strip())

    def test_chat_writes_log(self):
        with tempfile.TemporaryDirectory() as d:
            self._client(d).chat(
                [{"role": "system", "content": "你是面试教练"},
                 {"role": "user", "content": "给反馈"}],
                task="coach",
            )
            files = [f for f in os.listdir(d) if f.endswith(".jsonl")]
            self.assertEqual(len(files), 1, "每次运行应生成一个日志文件")
            with open(os.path.join(d, files[0]), encoding="utf-8") as f:
                rec = json.loads(f.readline())
            self.assertTrue(rec["ok"])
            self.assertEqual(rec["task"], "coach")
            self.assertIn("ms", rec)
            self.assertIn("response", rec)

    def test_chat_error_is_logged(self):
        cfg = LLMConfig(base_url="http://127.0.0.1:1/v1", model="x",
                        api_key="", requires_key=False, timeout=1, retries=1)
        with tempfile.TemporaryDirectory() as d:
            client = OpenAICompatibleLLM(cfg, logger=LLMLogger(False, d))
            with self.assertRaises(Exception):
                client.chat([{"role": "user", "content": "hi"}], task="t")
            files = [f for f in os.listdir(d) if f.endswith(".jsonl")]
            self.assertEqual(len(files), 1)
            with open(os.path.join(d, files[0]), encoding="utf-8") as f:
                rec = json.loads(f.readline())
            self.assertFalse(rec["ok"])
            self.assertTrue(rec["error"])


class TestBuildLLM(FakeServerMixin, unittest.TestCase):
    def test_build_local_with_explicit_base_url(self):
        llm = build_llm(mode="local", base_url=self.base_url)
        self.assertFalse(getattr(llm, "is_mock", False))
        self.assertEqual(llm.cfg.base_url, self.base_url)
        self.assertEqual(llm.cfg.model, fake.MODEL_NAME)

    def test_explicit_model_used(self):
        llm = build_llm(mode="local", base_url=self.base_url, model="my-model")
        self.assertEqual(llm.cfg.model, "my-model")

    def test_local_mode_raises_when_unreachable(self):
        with self.assertRaises(Exception):
            build_llm(mode="local", base_url="http://127.0.0.1:1/v1")

    def test_build_mock(self):
        self.assertTrue(build_llm(mock=True).is_mock)
        self.assertTrue(build_llm(mode="mock").is_mock)


class TestRemainingSlices(unittest.TestCase):
    def test_mock_llm_has_is_mock(self):
        self.assertTrue(MockLLM().is_mock)


if __name__ == "__main__":
    unittest.main(verbosity=2)
