"""文档冗余格式清洗：页眉页脚自动识别去除、空行/空白规整。

页眉页脚识别采用「跨页边界公共行」法：将每页首尾各若干行做数字归一化后
逐位置对齐，在 >=3 页且占比 >=60% 的页面上稳定出现的短行判为噪声。
"""
from __future__ import annotations

import re

from .base import ParsedDocument, Page

_BOUNDARY_LINES = 3   # 检查每页首尾各 3 行
_MIN_PAGES = 3
_MIN_RATIO = 0.6
_MAX_NOISE_LEN = 60


def _clean_line(line: str) -> str:
    line = line.replace(" ", " ").replace("\t", " ")
    line = re.sub(r"[ ]{2,}", " ", line)
    return line.strip()


def _norm(line: str, boundary_side: str | None = None) -> str:
    """归一化页眉页脚：数字替换为 # 用于跨页对齐；
    boundary_side 为 'head'/'tail' 时额外剥离仅出现在边界的页码片段
    （如正文行首误带的“第1页 ”、行尾的“ - 3 -”）。
    """
    s = re.sub(r"\d+", "#", line).strip()
    if boundary_side == "head":
        s = re.sub(r"^(?:第?#页?[\s、.．-]*)+", "", s)
    if boundary_side == "tail":
        s = re.sub(r"[\s\-—]*第?#页?$", "", s)
    return s.strip()


def _detect_noise(pages: list[Page]) -> set[str]:
    """返回归一化后的噪声行集合。"""
    if len(pages) < _MIN_PAGES:
        return set()

    page_line_lists = []
    for pg in pages:
        lines = [_clean_line(x) for x in pg.text.split("\n")]
        lines = [l for l in lines if l]
        page_line_lists.append(lines)

    # 收集「某位置上出现的归一化行」的出现页数
    from collections import defaultdict
    head_counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    tail_counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total = len(page_line_lists)

    for lines in page_line_lists:
        for pos in range(min(_BOUNDARY_LINES, len(lines))):
            head_counts[pos][_norm(lines[pos], "head")] += 1
            tail_counts[pos][_norm(lines[-(pos + 1)], "tail")] += 1

    noise: set[str] = set()

    def _harvest(counts):
        for pos, freq in counts.items():
            for text, cnt in freq.items():
                if cnt >= _MIN_PAGES and cnt / total >= _MIN_RATIO and len(text) <= _MAX_NOISE_LEN:
                    noise.add(text)

    _harvest(head_counts)
    _harvest(tail_counts)
    return noise


def clean_pages(parsed: ParsedDocument) -> ParsedDocument:
    noise = _detect_noise(parsed.pages)
    out_pages: list[Page] = []
    for pg in parsed.pages:
        lines = [_clean_line(x) for x in pg.text.split("\n")]
        lines = [l for l in lines if l]
        n = len(lines)
        kept = []
        for idx, line in enumerate(lines):
            side = "head" if idx < _BOUNDARY_LINES else (
                "tail" if idx >= n - _BOUNDARY_LINES else None)
            if side is not None and _norm(line, side) in noise:
                continue
            kept.append(line)
        # 在页面保留下来的首行剥离残留页码前缀（如“第1页 正文…”）
        if kept:
            stripped = re.sub(r"^第?\d+\s*页[\s、.．-]*", "", kept[0])
            if stripped and 0 < len(kept[0]) - len(stripped) <= 8:
                kept[0] = stripped
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
        if text:
            out_pages.append(Page(pg.page_no, text))
    return ParsedDocument(pages=out_pages)


def join_pages(pages: list[Page]) -> tuple[str, list[tuple[int, int, int, int]]]:
    """把清洗后的页拼接为全文，并记录每个字符区间对应的原始页码。

    返回 (full_text, spans)；span = (char_start, char_end, page_start, page_end)。
    """
    buf: list[str] = []
    spans: list[tuple[int, int, int, int]] = []
    offset = 0
    for pg in pages:
        text = pg.text
        start = offset
        buf.append(text)
        offset += len(text)
        spans.append((start, offset, pg.page_no, pg.page_no))
        buf.append("\n\n")
        offset += 2
    return "".join(buf).rstrip(), spans
