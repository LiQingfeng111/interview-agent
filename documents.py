"""文档摄入：把 PDF / DOCX / TXT / MD 变成纯文本，交给简历解析器。

为什么要单独一层：
  用户手里多半是 PDF（招聘网站导出的、Word 另存的）。如果只收 markdown，
  这个工具就只对"自己写简历的人"有用——而真实用户不会配合你的格式。

多引擎降级（不绑死单一依赖）：
  1. PyMuPDF（fitz）：中文 PDF 提取质量最好，本机有；
  2. pdftotext.exe：MiKTeX/poppler 自带，命令行能力稳；
  3. 纯标准库兜底：zlib 解压 + 解析 Tj/TJ 文本操作符（对简单 PDF 有效）。
  三者都失败才报错，并明确告诉用户"可能是扫描件，需要 OCR"。

诚实边界：
  - 扫描版 PDF（整页是图）没有 OCR 就出不来文字，直接说明，不假装成功；
  - PPT/图片同样不支持；
  - 加密 PDF 需要先解密。
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
import zlib

SUPPORTED_EXT = (".pdf", ".docx", ".md", ".markdown", ".txt")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
LEGACY_OFFICE = (".doc", ".ppt", ".pptx", ".xls", ".xlsx")


class DocumentError(RuntimeError):
    pass


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- PDF
def _pdf_fitz(data: bytes) -> tuple[str, dict]:
    import fitz  # PyMuPDF

    doc = fitz.open(stream=data, filetype="pdf")
    pages = []
    for page in doc:
        pages.append(page.get_text("text"))
    meta = {"pages": doc.page_count}
    doc.close()
    return "\n".join(pages), meta


def _pdf_pdftotext(data: bytes) -> tuple[str, dict]:
    exe = shutil.which("pdftotext")
    if not exe:
        raise DocumentError("没有 pdftotext")
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in.pdf")
        dst = os.path.join(d, "out.txt")
        with open(src, "wb") as f:
            f.write(data)
        proc = subprocess.run([exe, "-layout", "-enc", "UTF-8", src, dst],
                              capture_output=True, timeout=60)
        if proc.returncode != 0 or not os.path.exists(dst):
            raise DocumentError(f"pdftotext 失败：{proc.stderr.decode('utf-8', 'ignore')[:120]}")
        with open(dst, encoding="utf-8", errors="ignore") as f:
            return f.read(), {"pages": None}


def _pdf_stdlib(data: bytes) -> tuple[str, dict]:
    """纯标准库兜底：解开 FlateDecode 流，抽 Tj/TJ 里的字符串。

    只对"文本以简单编码写成"的 PDF 有效（很多 LaTeX 导出的英文简历属于这类）。
    中文 PDF 常把字放进 CID 字体 + ToUnicode CMap，这里不做完整 CMap 解析，
    抽不出来就让上层继续降级/报错——**宁可说不会，不要吐乱码**。
    """
    chunks: list[bytes] = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        raw = m.group(1)
        try:
            chunks.append(zlib.decompress(raw))
        except zlib.error:
            continue
    if not chunks:
        raise DocumentError("PDF 里没有可解压的文本流")

    texts: list[str] = []
    for blob in chunks:
        for tm in re.finditer(rb"\((?:[^()\\]|\\.)*\)", blob):
            s = tm.group(0)[1:-1]
            s = s.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
            try:
                texts.append(s.decode("latin-1"))
            except Exception:
                continue
    text = " ".join(texts)
    if not re.search(r"[A-Za-z\u4e00-\u9fa5]{4,}", text):
        raise DocumentError("兜底解析没抽出可读文本（可能是扫描件或 CID 字体）")
    return text, {"pages": None}


def extract_pdf(data: bytes) -> tuple[str, dict]:
    errors = []
    for name, fn in (("pymupdf", _pdf_fitz), ("pdftotext", _pdf_pdftotext), ("stdlib", _pdf_stdlib)):
        try:
            text, meta = fn(data)
            text = _clean(text)
            if len(text) < 30:
                raise DocumentError("抽出的文字太少")
            meta["engine"] = name
            return text, meta
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {e}")
    raise DocumentError("PDF 解析失败（可能加密或扫描件，需要 OCR）：" + " | ".join(errors))


# ---------------------------------------------------------------- DOCX
def extract_docx(data: bytes) -> tuple[str, dict]:
    try:
        from docx import Document

        doc = Document(io.BytesIO(data))
        lines = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                lines.append(" | ".join(c.text.strip() for c in row.cells))
        return _clean("\n".join(lines)), {"engine": "python-docx"}
    except ImportError:
        pass
    except Exception as e:
        raise DocumentError(f"python-docx 解析失败：{e}")

    # 兜底：docx 就是个 zip，正文在 word/document.xml
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except Exception as e:
        raise DocumentError(f"不是合法的 docx：{e}")
    xml = re.sub(r"</w:p>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return _clean(text), {"engine": "zip+xml"}


# ---------------------------------------------------------------- 入口
def load_document(data: bytes, filename: str = "") -> dict:
    """把任意支持的文档转成文本。返回文本与元信息，失败抛 DocumentError。"""
    if not data:
        raise DocumentError("文件是空的")
    ext = os.path.splitext(filename.lower())[1]

    # 用魔数校正扩展名：用户经常把 pdf 存成 .txt 或没有后缀
    if data[:4] == b"%PDF":
        ext = ".pdf"
    elif data[:2] == b"PK" and ext not in (".docx", ".pdf"):
        ext = ".docx"

    if ext == ".pdf":
        text, meta = extract_pdf(data)
    elif ext == ".docx":
        text, meta = extract_docx(data)
    elif ext in IMAGE_EXT:
        raise DocumentError("这是图片，本机没有 OCR，请粘贴文字或上传 PDF/DOCX")
    elif ext in LEGACY_OFFICE:
        raise DocumentError(f"暂不支持 {ext}，请另存为 PDF 或 DOCX 再上传")
    else:
        for enc in ("utf-8", "gb18030", "utf-16"):
            try:
                text = data.decode(enc)
                if text.count("\ufffd") < len(text) * 0.05:
                    meta = {"engine": f"text/{enc}"}
                    break
            except UnicodeDecodeError:
                continue
        else:
            raise DocumentError("无法识别文本编码，请另存为 UTF-8")
        text = _clean(text)

    if len(text) < 30:
        raise DocumentError("抽出来的文字太少（可能是扫描件或图片型 PDF）")
    meta.setdefault("chars", len(text))
    meta["filename"] = filename or "未命名"
    return {"text": text, **meta}


def supported_hint() -> str:
    return "支持 PDF / DOCX / Markdown / TXT；扫描件与图片需要 OCR（未支持）"
