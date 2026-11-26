"""生成测试用 PDF（中英文 + 扫描件），用来验证文档摄入的三条降级路径。

为什么要自己造：
  GitHub 上公开 PDF 简历不好找（多数是 LaTeX 源码 + png 预览），
  没有真实 PDF 就没法证明"别人传 PDF 给你，你能读懂"。
  这里用 PyMuPDF 造三种输入：
    1. resume_cn.pdf  中文简历（CID 字体）→ 验证 PyMuPDF 路径与 pdftotext 路径
    2. resume_en.pdf  英文简历（简单编码）→ 验证纯标准库兜底也能抽
    3. scanned.pdf    整页图片、无文本层 → 必须明确报错"需要 OCR"，不能假装成功

用法：python tools/make_test_pdfs.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "test_docs")

CN_TEXT = """张小明
AI 方向 2027 届 · 求职意向：Agent 开发实习

项目经历
高校课程问答助手
- 技术栈：Python / FAISS / LangChain / FastAPI
- 覆盖 300 份课件，召回率从 71% 提升到 82%
- 自建 20 条回归用例，空召回率超过 5% 触发告警
- 重排让延迟从 0.9 秒涨到 1.8 秒，换取 11 个点召回，经评估值得

校园二手交易平台
- 技术栈：FastAPI / PostgreSQL / Redis
- 负责商品检索与订单状态机，订单共 6 个状态
- 用数据库乐观锁解决并发超卖，冲突率约 3%

实习经历
某公司 后端开发实习
- 参与课程资料检索服务，负责切块与索引部分
"""

EN_TEXT = """Alex Zhang
AI Intern Candidate
Email: alex@example.com | GitHub: github.com/alex

Experience
Course QA Assistant
- Built retrieval pipeline with Python, FAISS and LangChain
- Improved recall from 71% to 82% on 300 lecture documents
- Added 20 regression cases, alert on empty recall above 5%

Campus Marketplace
- FastAPI and PostgreSQL backend, 6-state order machine
- Solved oversell with optimistic locking, conflict rate 3%
"""


def main() -> int:
    import fitz

    os.makedirs(OUT, exist_ok=True)

    # 1) 中文 PDF：用 MuPDF 内置 CJK 字体（不用嵌 20MB 字体文件）
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 560, 780), CN_TEXT, fontname="china-s", fontsize=10)
    doc.save(os.path.join(OUT, "resume_cn.pdf"))
    doc.close()

    # 2) 英文 PDF：Helvetica，简单编码
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 560, 780), EN_TEXT, fontname="helv", fontsize=10)
    doc.save(os.path.join(OUT, "resume_en.pdf"))
    doc.close()

    # 3) 扫描件：只画图形，没有文本层
    doc = fitz.open()
    page = doc.new_page()
    for i in range(12):
        page.draw_rect(fitz.Rect(40, 40 + i * 30, 520, 60 + i * 30), color=(0.7, 0.7, 0.7))
    doc.save(os.path.join(OUT, "scanned.pdf"))
    doc.close()

    for name in ("resume_cn.pdf", "resume_en.pdf", "scanned.pdf"):
        p = os.path.join(OUT, name)
        print(f"{name:<18} {os.path.getsize(p) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
