"""TXT / Markdown / 实验日志解析：编码探测、分页符识别、无效字符清理。"""
from __future__ import annotations

from pathlib import Path

from .base import Page, ParsedDocument


def _read_text(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def extract(path: str | Path) -> ParsedDocument:
    text = _read_text(path)
    # 统一换行；去掉空字符等控制符（保留换行/制表）
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 去掉空字符等无效控制符（保留换行/制表/换页）
    text = "".join(ch for ch in text if ch >= " " or ch in "\n\t\f")

    pages: list[Page] = []
    # 显式换页符优先
    for i, part in enumerate(text.split("\f"), start=1):
        part = part.strip("\n")
        if part.strip():
            pages.append(Page(i, part))

    if not pages:
        return ParsedDocument(pages=[])

    # 无换页符的长文本：按每 80 行合成一页，便于溯源页码展示
    if len(pages) == 1 and pages[0].text.count("\n") > 240:
        lines = pages[0].text.split("\n")
        pages = [Page(i + 1, "\n".join(lines[i:i + 80]))
                 for i in range(0, len(lines), 80)]
    return ParsedDocument(pages=pages)
