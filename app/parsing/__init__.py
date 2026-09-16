"""文档解析包：PDF / Word / TXT / 科研报告 / 实验日志等统一提取为 Page 结构。

外部包可选：pypdf、python-docx；缺失时自动使用内置标准库提取器。
"""
from .base import Page, ParsedDocument, parse_file  # noqa: F401
