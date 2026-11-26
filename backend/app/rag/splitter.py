"""切片器：把简历 / JD / 题库切成可检索的块。

## 三类文档三种切法（不能统一）

| 文档 | 切法 | 为什么 |
|---|---|---|
| 简历 | 按**项目**切，每个项目一块 + 保留整项目文本 | 按行切会丢上下文（"召回从71%到82%"脱离项目就没意义） |
| JD | 按**职责/要求条目**切 | 每条天然是语义单元 |
| 题库 | 一题一块（题干 + points 拼接） | 考点在 points 里，只切题干会丢考点 |

## 简历的 parent_text（用户明确要求的）

命中某个 chunk 后，**发整个项目的完整描述**给模型，而不是只发那一段。
理由：切片是为了检索精度，但生成需要上下文完整。

结构：
    chunk.text        = "召回从 71% 提到 82%，代价索引体积 +30%"   （检索用）
    chunk.parent_text = 整个「课程问答助手」项目的 800 字描述       （喂模型用）

检索时用 text 算相似度，命中后取 parent_text 进 prompt。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("interview.splitter")


@dataclass
class Chunk:
    seq: int
    text: str
    project: str = ""
    parent_text: str = ""
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 简历
#: 项目标题的常见形态：项目一 / 项目1 / 【项目】/ 1. xxx系统 / xxx项目（2026.03-2026.09）
#:
#: 注意：这里只负责"定位标题出现在哪里"，真正的项目名要取**整行**（见 split_resume）。
#: 第一版直接用 `m.group(0)` 当项目名，而这条正则只匹配到 `项目一：` 这个前缀，
#: 结果 project 字段成了 `"项目一："` —— 报告里会写成"你在项目一：这个项目上…"。
PROJECT_HEAD = re.compile(
    r"^\s*(?:"
    r"项目\s*[一二三四五六七八九十\d]+\s*[:：.、]?"
    r"|【[^】]{2,30}】"
    r"|[\d]+\s*[.、]\s*.{2,40}(?:系统|平台|助手|服务|工具)"
    r"|.{2,40}(?:系统|平台|助手|服务|工具)\s*[（(]\s*\d{4}"
    r")",
    re.M,
)


def _title_line_at(text: str, pos: int) -> str:
    """取 pos 所在的整行作为项目标题。

    这样 `项目一：课程问答助手（2026.03 - 2026.09）` 会拿到完整一行，
    而不是只有正则匹配到的 `项目一：`。

    坑记录：PROJECT_HEAD 以 `^\\s*` 开头，所以 m.start() 指向的是
    **前一个换行之前的位置**，直接用它找行尾会得到空字符串。
    这里先把 pos 推进到真正的行首（跳过多余空白与换行）。
    """
    # 跳到真正的行首：跳过 \n 和它们之间的空白
    start = pos
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    end = text.find("\n", start)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def split_resume(text: str, max_chunk_chars: int = 500) -> list[Chunk]:
    """把简历切成块。

    策略：
    1. 先按"项目标题"粗分（这是简历天然的语义边界）
    2. 每个项目内部按段落/换行切成 300-500 字
    3. 每块的 parent_text = 整个项目文本（供命中后还原上下文）
    """
    text = (text or "").strip()
    if not text:
        return []

    # 找项目边界
    heads = [(m.start(), _title_line_at(text, m.start())) for m in PROJECT_HEAD.finditer(text)]
    sections: list[tuple[str, str]] = []

    if not heads:
        # 没识别出项目结构 → 整篇当一块（并记录这个事实，供上层提示用户）
        sections.append(("（未识别项目）", text))
    else:
        # 第一个项目之前的内容（个人信息）单独成块
        if heads[0][0] > 20:
            sections.append(("个人信息", text[:heads[0][0]].strip()))
        for i, (start, title) in enumerate(heads):
            end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
            body = text[start:end].strip()
            name = _clean_project_name(title)
            sections.append((name, body))

    chunks: list[Chunk] = []
    seq = 0
    for name, body in sections:
        if not body.strip():
            continue
        for piece in _split_by_length(body, max_chunk_chars):
            chunks.append(Chunk(
                seq=seq,
                text=piece,
                project=name,
                parent_text=body,      # ← 整项目文本，命后喂模型用这个
                meta={"section": name},
            ))
            seq += 1
    return chunks


def _clean_project_name(title: str) -> str:
    """从项目标题里提取干净的项目名。

    输入形如：
        `项目一：课程问答助手（2026.03 - 2026.09）`
        `项目1、智能客服工单分类`
        `【课程问答助手】`
        `RAG 问答系统（2026.03-2026.09）`

    要提取出：`课程问答助手` / `智能客服工单分类` / `RAG 问答系统`

    第一版只去掉了 `项目一` 三个字，留下 `：课程问答助手（...）`，
    结果 project 字段成了 `"项目一："` —— 报告里会写成
    "你在项目一：这个项目上…"。测试抓到后改成现在这样：
    先剥编号前缀，再剥括号里的时间，最后清洗残留标点。
    """
    s = title.strip()

    # 1) 去掉"项目一："/"项目1、"/"项目 二:" 这类编号前缀
    s = re.sub(r"^项目\s*[一二三四五六七八九十\d]+\s*[:：.、,，]?\s*", "", s)
    # 2) 去掉包裹符号
    s = s.strip("【】[]（）()<>《》 ").strip()
    # 3) 去掉结尾的时间区间，如（2026.03 - 2026.09）/ 2026.03-2026.09
    s = re.sub(r"[（(]?\s*\d{4}[.\-/年]\s*\d{1,2}.*?[）)]?\s*$", "", s).strip()
    # 4) 去掉残留的标点
    s = s.strip("：:，,、.。-— ").strip()

    return (s or title)[:40]


def _split_by_length(text: str, max_chars: int) -> list[str]:
    """按段落累积到 max_chars，超了才切；不在句子中间切。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    buf = ""
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(buf) + len(line) + 1 <= max_chars:
            buf = f"{buf}\n{line}".strip()
        else:
            if buf:
                pieces.append(buf)
            # 单行就超长 → 按句号切
            if len(line) > max_chars:
                for sent in re.split(r"(?<=[。！？；])", line):
                    if len(sent) > max_chars:
                        for i in range(0, len(sent), max_chars):
                            pieces.append(sent[i:i + max_chars])
                    elif sent.strip():
                        pieces.append(sent.strip())
                buf = ""
            else:
                buf = line
    if buf:
        pieces.append(buf)
    return pieces


# ---------------------------------------------------------------- JD
def split_jd(text: str, max_chunk_chars: int = 300) -> list[Chunk]:
    """JD 按条目切：每条职责/要求天然是一个语义单元。"""
    text = (text or "").strip()
    if not text:
        return []

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    items: list[str] = []
    for ln in lines:
        # 列表项形态：- xxx / 1. xxx / · xxx / * xxx
        if re.match(r"^(?:[-·*•]|\d+[.、)])\s*", ln):
            items.append(re.sub(r"^(?:[-·*•]|\d+[.、)])\s*", "", ln))
        elif items and len(ln) < 60:
            # 短行可能是上一条的续行
            items[-1] = items[-1] + " " + ln
        else:
            items.append(ln)

    chunks: list[Chunk] = []
    for i, it in enumerate(items):
        if not it.strip():
            continue
        for j, piece in enumerate(_split_by_length(it, max_chunk_chars)):
            chunks.append(Chunk(seq=len(chunks), text=piece,
                                project=f"JD-{i}", parent_text=it))
    return chunks


# ---------------------------------------------------------------- 题库
def split_bank_question(question: str, points: list[str]) -> str:
    """题库一题一块：题干 + 评分要点拼接后向量化。

    为什么要带 points：考点信息主要在要点里。
    例如"多头注意力头数怎么取"，题干看不出考的是"维度除法"还是"子空间划分"，
    但 points 里写着 `d_k = d_model / h`，这就把考点锚定了。
    """
    pts = "；".join(p.strip() for p in (points or []) if p and p.strip())
    if pts:
        return f"{question.strip()}\n考点：{pts}"
    return question.strip()


# ---------------------------------------------------------------- 统一入口
def split(kind: str, text: str) -> list[Chunk]:
    if kind == "resume":
        return split_resume(text)
    if kind == "jd":
        return split_jd(text)
    raise ValueError(f"未知文档类型：{kind}")
