"""文档摄入测试：PDF / DOCX / 文本 / 错误路径。

为什么必须测：
  用户传上来的十有八九是 PDF，而 PDF 的文本流没有 markdown 标题。
  这条链路一旦坏，表现是"传上去没反应"或"解析出张小明这种项目"——
  都不报错，但结果全错，比崩掉更难发现。

测试用 `tools/make_test_pdfs.py` 生成的文件（若不存在则跳过 PDF 部分）。
"""
from __future__ import annotations

import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import documents as D
from resume import parse_resume_best

DOCS = os.path.join(ROOT, "data", "test_docs")


def _read(name: str) -> bytes:
    with open(os.path.join(DOCS, name), "rb") as f:
        return f.read()


class TestTextFormats(unittest.TestCase):
    def test_utf8_md(self):
        data = "# 简历\n\n## 检索问答\n- 用 Python 与 FAISS 做检索，召回 82%\n".encode("utf-8")
        doc = D.load_document(data, "a.md")
        self.assertIn("检索", doc["text"])
        self.assertIn("utf-8", doc["engine"])

    def test_gbk_txt(self):
        data = ("项目经历\n订单系统\n- 技术栈：FastAPI，QPS 提升 3 倍\n").encode("gb18030")
        doc = D.load_document(data, "a.txt")
        self.assertIn("订单系统", doc["text"])

    def test_empty_file(self):
        with self.assertRaises(D.DocumentError):
            D.load_document(b"", "a.txt")

    def test_too_short(self):
        with self.assertRaises(D.DocumentError):
            D.load_document("短".encode("utf-8"), "a.txt")


class TestDocx(unittest.TestCase):
    def test_docx_roundtrip(self):
        try:
            from docx import Document
        except ImportError:
            self.skipTest("没有 python-docx")
        buf = io.BytesIO()
        d = Document()
        d.add_paragraph("课程问答助手")
        d.add_paragraph("技术栈：Python, FAISS，覆盖 300 份课件")
        d.save(buf)
        doc = D.load_document(buf.getvalue(), "a.docx")
        self.assertIn("课程问答助手", doc["text"])

    def test_fake_docx(self):
        with self.assertRaises(D.DocumentError):
            D.load_document(b"PK\x03\x04broken", "a.docx")


class TestPdf(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(os.path.join(DOCS, "resume_cn.pdf")):
            self.skipTest("没有测试 PDF，先跑 python tools/make_test_pdfs.py")

    def test_chinese_pdf_text(self):
        doc = D.load_document(_read("resume_cn.pdf"), "r.pdf")
        self.assertIn("高校课程问答助手", doc["text"])
        self.assertGreater(doc["chars"], 100)

    def test_chinese_pdf_projects(self):
        doc = D.load_document(_read("resume_cn.pdf"), "r.pdf")
        names = [p.name for p in parse_resume_best(doc["text"], llm=None)]
        self.assertTrue(any("问答" in n for n in names), names)
        self.assertNotIn("张小明", names, "人名不该被当成项目")

    def test_english_pdf(self):
        doc = D.load_document(_read("resume_en.pdf"), "r.pdf")
        self.assertIn("Course QA Assistant", doc["text"])

    def test_scanned_pdf_errors_clearly(self):
        with self.assertRaises(D.DocumentError) as ctx:
            D.load_document(_read("scanned.pdf"), "s.pdf")
        self.assertIn("OCR", str(ctx.exception))

    def test_magic_number_beats_extension(self):
        """用户把 PDF 存成 .txt 也要能认出来。"""
        doc = D.load_document(_read("resume_cn.pdf"), "weird.txt")
        self.assertIn("高校", doc["text"])


class TestErrors(unittest.TestCase):
    def test_image_rejected(self):
        with self.assertRaises(D.DocumentError) as ctx:
            D.load_document(b"\x89PNG\r\n\x1a\n" + b"0" * 40, "a.png")
        self.assertIn("OCR", str(ctx.exception))

    def test_legacy_office_rejected(self):
        with self.assertRaises(D.DocumentError):
            D.load_document(b"\xd0\xcf\x11\xe0" + b"0" * 40, "a.doc")

    def test_hint_mentions_supported(self):
        self.assertIn("PDF", D.supported_hint())


if __name__ == "__main__":
    unittest.main(verbosity=2)
