"""统一解析入口与基础数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import config


@dataclass
class Page:
    """一页（或一段分页符分隔区域）的原始文本。TXT/日志类文档按行分页。"""
    page_no: int
    text: str


@dataclass
class ParsedDocument:
    pages: list[Page] = field(default_factory=list)


def parse_file(path: str | Path, file_type: str) -> ParsedDocument:
    """按扩展名分发到对应解析器。file_type 为小写扩展名（不含点）。"""
    ft = file_type.lower().lstrip(".")
    if ft == "pdf":
        from . import pdf_extract
        return pdf_extract.extract(path)
    if ft in ("docx", "doc"):
        from . import docx_extract
        return docx_extract.extract(path)
    if ft in ("txt", "md", "log"):
        from . import text_extract
        return text_extract.extract(path)
    raise ValueError(f"不支持的文件类型: .{ft}（支持: {sorted(config.SUPPORTED_EXT)}）")
