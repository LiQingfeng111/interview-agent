"""运行时联网 + Web UI 的离线测试。

联网部分只测"判断逻辑"（垃圾识别、跑题识别、查询构造、缓存），不发真实请求——
测试依赖外网就会变成随机失败。真实链路用 `python -m eval.smoke_web` 手动验。

UI 部分测纯函数接口（api_meta / api_bank / api_progress），不启动服务器。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import websearch
from websearch import build_queries, is_junk, is_off_domain, quality


class TestJunkDetection(unittest.TestCase):
    def test_dictionary_page_is_junk(self):
        self.assertTrue(is_junk({"url": "https://hanyu.baidu.com/zidian", "title": "多 汉语文字"}))
        self.assertTrue(is_junk({"url": "https://www.zdic.net/hans", "title": "多"}))
        self.assertTrue(is_junk({"url": "https://x.com/a", "title": "X 字的意思"}))

    def test_normal_page_not_junk(self):
        self.assertFalse(is_junk({"url": "https://zhuanlan.zhihu.com/p/1", "title": "RAG 实践"}))


class TestOffDomain(unittest.TestCase):
    def test_asset_appraisal_is_off_domain(self):
        item = {"title": "中国资产评估协会", "snippet": "会长出席论坛并讲话"}
        self.assertTrue(is_off_domain(item))

    def test_tech_content_is_on_domain(self):
        item = {"title": "Faiss 索引类型", "snippet": "向量索引与检索延迟"}
        self.assertFalse(is_off_domain(item))


class TestQuality(unittest.TestCase):
    def test_quality_counts_only_good(self):
        results = [
            {"url": "https://zhihu.com/p/1", "title": "RAG 检索延迟优化", "snippet": "模型 检索"},
            {"url": "https://zdic.net/a", "title": "多 汉语文字", "snippet": "读音"},
            {"url": "https://x.com/b", "title": "资产评估协会", "snippet": "论坛"},
            {"url": "https://juejin.cn/p/2", "title": "Faiss 调优", "snippet": "索引 向量"},
        ]
        self.assertAlmostEqual(quality(results), 2 / 4)

    def test_empty_quality_zero(self):
        self.assertEqual(quality([]), 0.0)


class TestQueries(unittest.TestCase):
    def test_fact_query_has_site_hint(self):
        qs = build_queries("FAISS 取舍", kind="fact")
        self.assertTrue(qs)
        self.assertTrue(any("site:" in q for q in qs), "必须有站点限定兜底，否则中文查询会被字典页占满")

    def test_interview_query(self):
        qs = build_queries("评估体系", kind="interview")
        self.assertIn("评估体系 面试题", qs)

    def test_empty_term(self):
        self.assertEqual(build_queries(""), [])


class TestCache(unittest.TestCase):
    def test_cache_roundtrip(self):
        old = websearch.CACHE_DIR
        with tempfile.TemporaryDirectory() as d:
            websearch.CACHE_DIR = d
            try:
                self.assertIsNone(websearch._load_cache("x"))
                websearch._save_cache("x", [{"title": "t"}])
                self.assertEqual(websearch._load_cache("x"), [{"title": "t"}])
            finally:
                websearch.CACHE_DIR = old


class TestUIApi(unittest.TestCase):
    def test_meta_lists_roles_and_personas(self):
        import webui

        meta = webui.api_meta()
        keys = [r["key"] for r in meta["roles"]]
        self.assertIn("agent_app", keys)
        self.assertGreaterEqual(len(meta["personas"]), 4)

    def test_bank_api(self):
        import webui

        out = webui.api_bank("agent_app")
        if out["total"] == 0:
            self.skipTest("题库为空")
        self.assertGreater(out["total"], 0)
        self.assertTrue(out["preview"])
        self.assertIn("question", out["preview"][0])

    def test_history_api_shape(self):
        import webui

        out = webui.api_history()
        self.assertIn("count", out)
        self.assertIn("text", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
