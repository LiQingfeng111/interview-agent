"""雷达图测试：两条路径（在线 API / 本地 SVG）都得能用。

为什么要测离线降级：
  "图裂了"是面试作品集最丢分的事故。在线 API 一挂，报告里就是空白图——
  所以这里强制验证：拿不到 PNG 时，SVG 仍然完整可看。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chart

DIMS = {"accuracy": 2.82, "depth": 1.0, "tradeoff": 1.91, "boundary": 1.91, "clarity": 3.73}


class TestRadarConfig(unittest.TestCase):
    def test_config_structure(self):
        cfg = chart.radar_config(DIMS)
        self.assertEqual(cfg["type"], "radar")
        self.assertEqual(len(cfg["data"]["labels"]), 5)
        self.assertEqual(len(cfg["data"]["datasets"]), 2)   # 本场 + 合格线
        self.assertEqual(cfg["data"]["datasets"][1]["data"], [chart.TARGET_SCORE] * 5)

    def test_labels_chinese(self):
        cfg = chart.radar_config(DIMS)
        self.assertIn("准确性", cfg["data"]["labels"])
        self.assertNotIn("accuracy", cfg["data"]["labels"])

    def test_missing_dims_tolerated(self):
        cfg = chart.radar_config({"accuracy": 3})
        self.assertEqual(len(cfg["data"]["labels"]), 1)

    def test_url_encodes_config(self):
        url = chart.quickchart_url(chart.radar_config(DIMS))
        self.assertTrue(url.startswith("https://quickchart.io/chart?"))
        self.assertIn("c=", url)
        self.assertIn("%22type%22", url)      # JSON 被编码


class TestLocalSvg(unittest.TestCase):
    def test_svg_is_svg(self):
        s = chart.local_svg(DIMS)
        self.assertTrue(s.startswith("<svg"))
        self.assertTrue(s.rstrip().endswith("</svg>"))

    def test_svg_contains_all_labels_and_values(self):
        s = chart.local_svg(DIMS)
        for name in ("准确性", "深度", "权衡", "边界", "表达"):
            self.assertIn(name, s)
        self.assertIn("2.8", s)      # accuracy 2.82 取一位

    def test_svg_has_target_line(self):
        self.assertIn("stroke-dasharray", chart.local_svg(DIMS))

    def test_extreme_values_clamped(self):
        s = chart.local_svg({"accuracy": 99, "depth": -5})
        self.assertIn("<svg", s)     # 不崩，超界被夹住


class TestFetchFallback(unittest.TestCase):
    def test_online_failure_falls_back_to_svg(self):
        with patch.object(chart, "fetch_png", return_value=None):
            out = chart.write_chart_files(DIMS, outdir="reports/_test", stem="t1")
        self.assertEqual(out["source"], "local-svg")
        self.assertEqual(out["png"], "")
        self.assertTrue(os.path.exists(out["svg"]))
        os.remove(out["svg"])
        os.rmdir("reports/_test")

    def test_online_success_marks_source(self):
        with patch.object(chart, "fetch_png", return_value=b"\x89PNG\r\n\x1a\nfakepng"):
            out = chart.write_chart_files(DIMS, outdir="reports/_test", stem="t2")
        self.assertEqual(out["source"], "quickchart")
        self.assertTrue(os.path.exists(out["png"]))
        for p in (out["svg"], out["png"]):
            os.remove(p)
        os.rmdir("reports/_test")

    def test_fetch_rejects_non_png(self):
        class Resp:
            def read(self):
                return b"<html>not an image</html>"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(chart.urllib.request, "urlopen", return_value=Resp()):
            self.assertIsNone(chart.fetch_png("https://example.com/x"))


class TestHtmlReport(unittest.TestCase):
    def test_html_embeds_svg_inline(self):
        html = chart.report_html(DIMS, {"轮次": 8}, {"fatal": 0})
        self.assertIn("<svg", html)
        self.assertIn("轮次", html)
        self.assertIn("fatal", html)
        # 不依赖外部图片：离线也能看
        self.assertNotIn("<img", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
