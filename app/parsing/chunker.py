"""超长文档语义自适应分段（需求 2）。

策略：
  1. 基于标题样式/编号识别论文章节（如“1 引言”“2.3.1 实验方法”
     “摘要”“Abstract”“材料与方法”），构建标题层级路径 heading_path；
  2. 逻辑段落（以空行分隔）是最小的不可割裂单元——分段绝不会从段落中间切断；
  3. 小段向后合并到目标长度，超大章节先按段落、再按句末标点温和切分，
     保证每段不超过上限；
  4. 记录每个片段在全文中的字符偏移 [char_start, char_end) 与页码区间，
     为精准溯源提供定位信息。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import config

_HEADING_ENUM = re.compile(
    r"^(?:第[一二三四五六七八九十百\d]+[章节部分篇]\s*"
    r"|(?:\d{1,2}(?:\.\d{1,2}){0,3})[\s、.．]"
    r"|[一二三四五六七八九十]+[、.．])\s*.{0,34}$"
)
_HEADING_KEYWORDS = {
    "摘要", "abstract", "关键词", "keywords", "引言", "前言", "背景", "绪论",
    "材料与方法", "方法", "实验方法", "实验设计", "实验方案", "实验结果",
    "结果", "结果与分析", "讨论", "结论", "结论与展望", "参考文献", "致谢",
    "附录", "缩略词表", "相关工作", "研究背景", "研究内容", "技术路线",
    "数据集", "评价指标", "对比实验", "消融实验",
}
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])")


@dataclass
class Block:
    kind: str                  # "h" 标题 / "p" 段落
    text: str
    start: int                 # 在全文中的字符偏移
    end: int
    level: int = 1


@dataclass
class ChunkSpec:
    content: str
    heading_path: str | None
    char_start: int
    char_end: int
    page_start: int | None
    page_end: int | None


def _is_heading(line: str) -> tuple[bool, int]:
    s = line.strip()
    if not s or len(s) > config.HEADING_MAX_CHARS:
        return False, 1
    if s.endswith(("。", "，", ",", ".", "！", "？", "：", ":")):
        return False, 1
    m = re.match(r"^(#{1,6})\s+\S", s)
    if m:
        return True, len(m.group(1))
    low = s.lower()
    if low in _HEADING_KEYWORDS:
        return True, 1
    if _HEADING_ENUM.match(s):
        m2 = re.match(r"^(\d{1,2})(?:\.(\d{1,2}))?(?:\.(\d{1,2}))?", s)
        if m2:
            level = sum(1 for g in m2.groups() if g) + 1
            return True, level
        return True, 1
    return False, 1


def _scan_blocks(full_text: str) -> list[Block]:
    """逐行扫描全文，生成 标题/段落 块，偏移量严格对应全文。

    不依赖空行分隔：标题行由编号/样式正则识别；连续正文行累积为段落，
    遇到标题或空行结束当前段落。
    """
    blocks: list[Block] = []
    para_lines: list[tuple[str, int, int]] = []  # (文本, 行起始, 行结束)

    def flush_para():
        if not para_lines:
            return
        text = "\n".join(x[0] for x in para_lines)
        start = para_lines[0][1]
        end = para_lines[-1][2]
        blocks.append(Block("p", text, start, end))
        para_lines.clear()

    pos = 0
    n = len(full_text)
    for line in full_text.split("\n"):
        line_start = pos
        line_end = pos + len(line)
        stripped = line.strip()
        is_h, level = _is_heading(stripped)
        if is_h:
            flush_para()
            hs = line_start + line.index(stripped)
            title = stripped.lstrip("# ").strip()
            hs2 = line_start + line.index(title)
            blocks.append(Block("h", title, hs2, hs2 + len(title), level))
        elif not stripped:
            flush_para()
        else:
            # 段落内首行也可能是漏网标题（整行较短且符合编号规则时）
            if not para_lines:
                is_h2, level2 = _is_heading(stripped)
                if is_h2:
                    title = stripped.lstrip("# ").strip()
                    hs2 = line_start + line.index(title)
                    blocks.append(Block("h", title, hs2, hs2 + len(title), level2))
                    pos = line_end + 1
                    continue
            rs = line_start + line.index(stripped)
            para_lines.append((stripped, rs, rs + len(stripped)))
        pos = line_end + 1  # 跳过 \n
    flush_para()
    return blocks


def build_chunks(full_text: str, page_spans,
                 target: int | None = None,
                 limit: int | None = None) -> list[ChunkSpec]:
    target = target or config.CHUNK_TARGET_CHARS
    limit = limit or config.CHUNK_MAX_CHARS

    blocks = _scan_blocks(full_text)

    # 维护标题层级，把段落归属到章节
    sections: list[tuple[str | None, list[Block]]] = []
    stack: list[tuple[int, str]] = []

    def heading_path():
        return " / ".join(t for _, t in stack) or None

    def add_para(blk: Block):
        if not sections or sections[-1][0] != heading_path():
            sections.append((heading_path(), []))
        sections[-1][1].append(blk)

    for blk in blocks:
        if blk.kind == "h":
            while stack and stack[-1][0] >= blk.level:
                stack.pop()
            stack.append((blk.level, blk.text))
        else:
            add_para(blk)

    # 以章节为边界组织片段：同章节内小段落合并到目标长度；绝不跨标题合并，
    # 保证每个片段的章节归属与逻辑完整性不被割裂。
    specs: list[ChunkSpec] = []
    buf: list[Block] = []
    buf_heading: str | None = None

    def flush():
        if not buf:
            return
        content = full_text[buf[0].start:buf[-1].end].strip()
        s = buf[0].start + full_text[buf[0].start:buf[-1].end].index(content[0])
        e = s + len(content)
        ps, pe = _page_range(page_spans, s, e)
        specs.append(ChunkSpec(content, buf_heading, s, e, ps, pe))
        buf.clear()

    def cur_len():
        return (buf[-1].end - buf[0].start) if buf else 0

    for heading, paras in sections:
        sec_len = sum(b.end - b.start for b in paras) + 2 * max(len(paras) - 1, 0)
        if sec_len <= limit:
            # 新章节开始，先输出上一章节缓冲
            if buf and buf_heading != heading:
                flush()
            buf_heading = heading
            for b in paras:
                blen = b.end - b.start
                if buf and cur_len() + blen > target and cur_len() > 0:
                    flush()
                buf.append(b)
            flush()
        else:
            flush()
            # 超大章节：段落累积到 target；单段仍过长则按句切
            for b in paras:
                blen = b.end - b.start
                if blen > limit:
                    flush()
                    for content, s, e in _split_by_sentence(full_text, b, target, limit):
                        ps, pe = _page_range(page_spans, s, e)
                        specs.append(ChunkSpec(content, heading, s, e, ps, pe))
                    continue
                if buf and cur_len() + blen > target:
                    flush()
                buf_heading = heading
                buf.append(b)
            flush()
    flush()
    return specs


def _split_by_sentence(full_text: str, blk: Block, target: int, limit: int):
    """把一个超长段落切成不超过 limit 的片段。

    优先按句末标点切分；若整段没有任何标点（极端情况），按 limit 硬切分，
    保证片段长度有上界。返回片段的绝对偏移，char_start/char_end 严格准确。
    """
    text = full_text[blk.start:blk.end]
    sentences = [s for s in _SENT_SPLIT.split(text) if s.strip()]

    # 极端情况：没有句末标点 -> 按 limit 硬切
    if len(sentences) <= 1:
        pieces = []
        step = limit
        for i in range(0, len(text), step):
            seg = text[i:i + step].strip()
            if not seg:
                continue
            lead = text[i:i + step].index(seg[0])
            abs_s = blk.start + i + lead
            pieces.append((seg, abs_s, abs_s + len(seg)))
        return pieces

    # 顺序定位每个句段在段落中的相对偏移
    rel: list[tuple[int, int]] = []
    search = 0
    for sent in sentences:
        idx = text.find(sent, search)
        rel.append((idx, idx + len(sent)))
        search = idx + len(sent)

    pieces = []
    cur_start = 0
    for i, (rs, re_) in enumerate(rel):
        length = re_ - cur_start
        next_len = (rel[i + 1][1] - cur_start) if i + 1 < len(rel) else length
        if length >= target or (i + 1 < len(rel) and next_len > limit):
            seg_text = text[cur_start:re_].strip()
            lead = text[cur_start:re_].index(seg_text[0])
            abs_s = blk.start + cur_start + lead
            pieces.append((seg_text, abs_s, abs_s + len(seg_text)))
            cur_start = re_
    if cur_start < len(text):
        tail = text[cur_start:].strip()
        if tail:
            lead = text[cur_start:].index(tail[0])
            abs_s = blk.start + cur_start + lead
            pieces.append((tail, abs_s, abs_s + len(tail)))

    # 单句本身仍超过 limit（超长句无标点）的兜底硬切
    final = []
    for seg, s, e in pieces:
        if len(seg) <= limit:
            final.append((seg, s, e))
        else:
            for j in range(0, len(seg), limit):
                part = seg[j:j + limit]
                final.append((part, s + j, s + j + len(part)))
    return final


def _page_range(page_spans, start, end):
    ps_no = pe_no = None
    for s, e, pstart, pend in page_spans:
        if s <= start < e:
            ps_no = pstart
        if s < end <= e + 2:
            pe_no = pend
    if ps_no is None and page_spans:
        for s, e, pstart, _ in page_spans:
            if start <= e:
                ps_no = pstart
                break
        ps_no = ps_no or page_spans[0][2]
    if pe_no is None and page_spans:
        pe_no = page_spans[-1][3]
    return ps_no, pe_no or ps_no
