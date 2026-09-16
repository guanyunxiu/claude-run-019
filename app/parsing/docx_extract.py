"""Word(.docx) 解析。

优先使用 python-docx（若环境已安装）；否则使用标准库 zipfile + XML 直接解析
word/document.xml：提取段落文本、标题样式、换页符，保留文档逻辑结构。
.doc（97-2003 二进制）无法由标准库解析，给出明确提示。
"""
from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .base import Page, ParsedDocument

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_via_python_docx(path) -> ParsedDocument:
    import docx  # python-docx

    document = docx.Document(str(path))
    lines: list[str] = []
    pages: list[Page] = []
    page_no = 1
    for para in document.paragraphs:
        text = (para.text or "").strip()
        style = (para.style.name or "") if para.style else ""
        # python-docx 把显式分页符放在 runs 的 break 中
        for run in para.runs:
            for br in run._element.findall(f"{_W}br"):
                if br.get(f"{_W}type") == "page":
                    body = "\n".join(lines).strip()
                    if body:
                        pages.append(Page(page_no, body))
                    page_no += 1
                    lines = []
        if text:
            if style.startswith("Heading") or style in ("Title",):
                lines.append(text)  # 标题作为独立行，供分段器识别
            else:
                lines.append(text)
    # 表格内容（实验记录常用表格）
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append(" | ".join(cells))
    body = "\n".join(lines).strip()
    if body:
        pages.append(Page(page_no, body))
    return ParsedDocument(pages=pages or [Page(1, "")])


def _extract_via_stdlib(path) -> ParsedDocument:
    """零依赖 DOCX 解析：读取 ZIP 内 document.xml，遍历段落与换页符。"""
    pages: list[Page] = []
    with zipfile.ZipFile(path) as zf:
        xml_bytes = zf.read("word/document.xml")
    root = ET.fromstring(xml_bytes)

    lines: list[str] = []
    page_no = 1

    def para_text_and_breaks(p):
        """返回 (文本, 该段内换页符数量)。"""
        parts: list[str] = []
        breaks = 0
        for node in p.iter():
            tag = node.tag
            if tag == f"{_W}t":
                parts.append(node.text or "")
            elif tag == f"{_W}tab":
                parts.append("\t")
            elif tag == f"{_W}br" and node.get(f"{_W}type") == "page":
                breaks += 1
            elif tag == f"{_W}cr":
                parts.append("\n")
        return "".join(parts).strip(), breaks

    for p in root.iter(f"{_W}p"):
        text, breaks = para_text_and_breaks(p)
        for _ in range(breaks):
            body = "\n".join(lines).strip()
            if body:
                pages.append(Page(page_no, body))
            page_no += 1
            lines = []
        if text:
            lines.append(text)

    body = "\n".join(lines).strip()
    if body:
        pages.append(Page(page_no, body))
    return ParsedDocument(pages=pages or [Page(1, "")])


def extract(path: str | Path) -> ParsedDocument:
    p = Path(path)
    if p.suffix.lower() == ".doc":
        raise ValueError(
            "暂不支持旧版 .doc（Word 97-2003 二进制）格式，请另存为 .docx 后上传"
        )
    try:
        import docx  # noqa: F401
        return _extract_via_python_docx(p)
    except ImportError:
        return _extract_via_stdlib(p)
