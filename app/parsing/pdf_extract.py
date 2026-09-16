"""PDF 文本提取。

优先使用 pypdf（若环境已安装）；否则使用内置标准库实现的精简 PDF 提取器，
支持：
  - xref / 起始交叉引用表定位、间接对象解析、对象流（FlateDecode）解压；
  - 页面树（Pages）遍历与资源继承；
  - 文本操作符 Tj / TJ / ' / "，按 BT、Td/TD/T* 还原换行；
  - ToUnicode CMap（bfchar / bfrange）解码，覆盖中文等 CID 字体；
  - 跳过内嵌图片（BI/ID/EI）等二进制数据。
对扫描版（纯图片无文字层）PDF 返回空页文本，便于上层给出提示。
"""
from __future__ import annotations

import re
import zlib
from pathlib import Path

from .base import Page, ParsedDocument


# ============================== pypdf 路径 ==============================

def _extract_via_pypdf(path) -> ParsedDocument:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append(Page(i, text.strip()))
    return ParsedDocument(pages=pages)


# ============================== 内置精简解析器 ==============================

_WS = b"\x00\t\n\x0c\r "


class _PdfReader:
    def __init__(self, data: bytes):
        self.data = data
        self.objects: dict[tuple[int, int], bytes] = {}
        self._load_objects()

    # ---- 对象收集 ----
    def _load_objects(self) -> None:
        for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b", self.data):
            start = m.end()
            end = self.data.find(b"endobj", start)
            if end == -1:
                continue
            num, gen = int(m.group(1)), int(m.group(2))
            self.objects[(num, gen)] = self.data[start:end].strip()

    def get_object(self, num: int, gen: int = 0) -> bytes | None:
        if (num, gen) in self.objects:
            return self.objects[(num, gen)]
        if (num, 0) in self.objects:
            return self.objects[(num, 0)]
        return None

    def _root_ref(self) -> tuple[int, int] | None:
        m = re.search(rb"/Root\s+(\d+)\s+(\d+)\s+R", self.data)
        if m:
            return int(m.group(1)), int(m.group(2))
        start = self.data.rfind(b"startxref")
        if start != -1:
            m = re.search(rb"/Root\s+(\d+)\s+(\d+)\s+R", self.data[start:start + 2000])
            if m:
                return int(m.group(1)), int(m.group(2))
        return None

    # ---- 对象辅助 ----
    @staticmethod
    def _dict_value(obj: bytes, key: bytes) -> bytes | None:
        m = re.search(rb"/" + key + rb"\b", obj)
        if not m:
            return None
        i = m.end()
        while i < len(obj) and obj[i] in _WS:
            i += 1
        if i >= len(obj):
            return None
        if obj[i:i + 2] == b"<<":
            depth = 0
            j = i
            while j < len(obj):
                if obj[j:j + 2] == b"<<":
                    depth += 1
                    j += 2
                elif obj[j:j + 2] == b">>":
                    depth -= 1
                    j += 2
                    if depth == 0:
                        return obj[i:j]
                else:
                    j += 1
            return None
        if obj[i:i + 1] == b"[":
            depth = 0
            j = i
            while j < len(obj):
                if obj[j:j + 1] == b"[":
                    depth += 1
                elif obj[j:j + 1] == b"]":
                    depth -= 1
                    if depth == 0:
                        return obj[i:j + 1]
                j += 1
            return None
        j = i
        # 间接引用 N G R 需作为整体读入
        tail = obj[i:j + 40]
        m_ref = re.match(rb"\d+\s+\d+\s+R", tail)
        if m_ref:
            return m_ref.group(0)
        while j < len(obj) and obj[j] not in _WS + b"<>[]()/":
            j += 1
        return obj[i:j]

    def _resolve_value(self, value: bytes) -> bytes | None:
        """把字典值解析为对象字节（解引用 R）。"""
        value = value.strip()
        m = re.fullmatch(rb"(\d+)\s+(\d+)\s+R", value)
        if m:
            return self.get_object(int(m.group(1)), int(m.group(2)))
        return value

    # ---- 流解压 ----
    def _stream_bytes(self, obj: bytes) -> bytes | None:
        m = re.search(rb"stream\r?\n", obj)
        if not m:
            return None
        start = m.end()
        end = obj.rfind(b"endstream")
        raw = obj[start:end]
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        filters = self._dict_value(obj, b"Filter") or b""
        has_flate = b"FlateDecode" in filters
        if not has_flate:
            # 某些生成器把 Filter 放在流字典后部或压缩后仍以 zlib 头 0x78 开头
            has_flate = raw[:1] == b"x" and (raw[1:2] in (b"\x01", b"\x9c", b"\xda"))
        if has_flate:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                # 宽容处理流前后多余/缺失的换行
                for cand in (raw[1:], raw[:-1], raw[2:], raw[:-2]):
                    try:
                        return zlib.decompress(cand)
                    except zlib.error:
                        continue
                return None
        return raw

    # ---- 页面收集 ----
    def collect_pages(self) -> list[tuple[bytes, bytes]]:
        """返回 [(content_bytes, resources_dict_bytes), ...]，按页序。"""
        root_ref = self._root_ref()
        if not root_ref:
            return []
        root = self.get_object(*root_ref)
        if not root:
            return []
        pages_ref = self._dict_value(root, b"Pages")
        m = re.fullmatch(rb"\s*(\d+)\s+(\d+)\s+R\s*", pages_ref or b"")
        if not m:
            return []
        pages_node = self.get_object(int(m.group(1)), int(m.group(2)))
        if not pages_node:
            return []

        result: list[tuple[bytes, bytes]] = []

        def walk(node: bytes, inherited: bytes | None):
            contents = self._dict_value(node, b"Contents")
            resources = self._dict_value(node, b"Resources") or inherited
            kids = self._dict_value(node, b"Kids")
            if contents is not None:  # 页节点
                content = self._join_content(contents)
                result.append((content, resources or b""))
            if kids:
                for ref in re.finditer(rb"(\d+)\s+(\d+)\s+R", kids):
                    child = self.get_object(int(ref.group(1)), int(ref.group(2)))
                    if child:
                        walk(child, resources)

        walk(pages_node, None)
        return result

    def _join_content(self, contents_val: bytes) -> bytes:
        parts: list[bytes] = []
        refs = list(re.finditer(rb"(\d+)\s+(\d+)\s+R", contents_val))
        if contents_val.lstrip().startswith(b"[") and refs:
            for ref in refs:
                obj = self.get_object(int(ref.group(1)), int(ref.group(2)))
                if obj:
                    s = self._stream_bytes(obj)
                    if s:
                        parts.append(s)
        else:
            m = re.fullmatch(rb"\s*(\d+)\s+(\d+)\s+R\s*", contents_val)
            if m:
                obj = self.get_object(int(m.group(1)), int(m.group(2)))
                if obj:
                    s = self._stream_bytes(obj)
                    if s:
                        parts.append(s)
        return b"\n".join(parts)

    # ---- 字体与 ToUnicode ----
    @staticmethod
    def _iter_name_refs(dict_bytes: bytes):
        """遍历字典中的 /Name N G R（或 /Name <<..>>）条目，返回 (name, ref|bytes)。"""
        i, n = 0, len(dict_bytes)
        while i < n:
            if dict_bytes[i:i + 1] != b"/":
                i += 1
                continue
            j = i + 1
            while j < n and dict_bytes[j] not in _WS + b"<>[]()/":
                j += 1
            name = dict_bytes[i + 1:j].decode("latin-1")
            k = j
            while k < n and dict_bytes[k] in _WS:
                k += 1
            if k < n and dict_bytes[k:k + 2] == b"<<":
                depth, k2 = 0, k
                while k2 < n:
                    if dict_bytes[k2:k2 + 2] == b"<<":
                        depth += 1
                        k2 += 2
                    elif dict_bytes[k2:k2 + 2] == b">>":
                        depth -= 1
                        k2 += 2
                        if depth == 0:
                            break
                    else:
                        k2 += 1
                yield name, ("inline", dict_bytes[k:k2])
                i = k2
            elif k < n and dict_bytes[k:k + 1] == b"[":
                depth, k2 = 0, k
                while k2 < n:
                    if dict_bytes[k2:k2 + 1] == b"[":
                        depth += 1
                    elif dict_bytes[k2:k2 + 1] == b"]":
                        depth -= 1
                        if depth == 0:
                            break
                    k2 += 1
                i = k2 + 1
            else:
                m = re.match(rb"(\d+)\s+(\d+)\s+R", dict_bytes[k:k + 40])
                if m:
                    yield name, ("ref", int(m.group(1)), int(m.group(2)))
                    i = k + m.end()
                else:
                    i = k

    def font_tounicode(self, resources: bytes) -> dict[str, dict[int, str]]:
        """返回 {资源字体名: {字符码: unicode字符}}。"""
        result: dict[str, dict[int, str]] = {}
        fonts = self._dict_value(resources, b"Font")
        font_obj = self._resolve_value(fonts) if fonts else None
        if not font_obj:
            return result
        for item in self._iter_name_refs(font_obj):
            name = item[0]
            kind = item[1][0]
            if kind == "ref":
                _, fnum, fgen = item[1]
                fobj = self.get_object(fnum, fgen)
            else:
                fobj = item[1][1]
            if not fobj:
                continue
            cmap = self._load_tounicode(fobj)
            if cmap:
                result[name] = cmap
        return result

    def _load_tounicode(self, font_obj: bytes) -> dict[int, str]:
        ref = self._dict_value(font_obj, b"ToUnicode")
        stream_obj = self._resolve_value(ref) if ref else None
        if not stream_obj:
            return {}
        stream = self._stream_bytes(stream_obj)
        if not stream:
            return {}
        return parse_cmap(stream.decode("latin-1", errors="ignore"))

    # ---- 页面文本 ----
    def page_text(self, content: bytes, resources: bytes) -> str:
        fonts = self.font_tounicode(resources)
        return extract_text_from_content(content, fonts)


# ---------- ToUnicode CMap ----------

def _hex_to_int(h: str) -> int:
    return int(h.replace(" ", "").replace("\n", "").replace("\r", ""), 16)


def _hex_to_unicode(h: str) -> str:
    raw = bytes.fromhex(re.sub(r"\s+", "", h))
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="ignore")


def parse_cmap(text: str) -> dict[int, str]:
    cmap: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>", block):
            cmap[_hex_to_int(src)] = _hex_to_unicode(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for line in block.splitlines():
            line = line.strip()
            m = re.match(r"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>\s*(.+)", line)
            if not m:
                continue
            lo, hi, tail = _hex_to_int(m.group(1)), _hex_to_int(m.group(2)), m.group(3)
            arr = re.findall(r"<([0-9A-Fa-f\s]+)>", tail)
            if len(arr) >= 3 and tail.lstrip().startswith("["):
                for i, code in enumerate(range(lo, hi + 1)):
                    if i < len(arr):
                        cmap[code] = _hex_to_unicode(arr[i])
            elif arr:
                base = _hex_to_unicode(arr[0])
                prefix, last = base[:-1], ord(base[-1])
                for i, code in enumerate(range(lo, hi + 1)):
                    cmap[code] = prefix + chr(last + i)
    return cmap


# ---------- 内容流文本提取（操作符栈机） ----------

def _decode_literal(raw: bytes) -> str:
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == 0x5C:  # backslash
            i += 1
            if i >= len(raw):
                break
            esc = raw[i]
            mapping = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t",
                       ord("b"): b"\b", ord("f"): b"\f", ord("("): b"(",
                       ord(")"): b")", ord("\\"): b"\\"}
            if esc in mapping:
                out += mapping[esc]
            elif 48 <= esc <= 55:  # octal
                oct_digits = bytes([esc])
                for _ in range(2):
                    if i + 1 < len(raw) and 48 <= raw[i + 1] <= 55:
                        i += 1
                        oct_digits += bytes([raw[i]])
                    else:
                        break
                out.append(int(oct_digits, 8) & 0xFF)
            i += 1
        else:
            out.append(ch)
            i += 1
    b = bytes(out)
    for enc in ("utf-16-be", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1", errors="ignore")


def extract_text_from_content(content: bytes, fonts: dict[str, dict[int, str]]) -> str:
    """扫描 PDF 内容流，按栈机解释文本操作符。"""
    lines: list[str] = []
    cur: list[str] = []
    stack: list = []
    current_font: dict[int, str] | None = None
    i, n = 0, len(content)
    last_was_newline = True

    def emit_newline():
        nonlocal last_was_newline
        if cur and "".join(cur).strip():
            lines.append("".join(cur).rstrip())
        cur.clear()
        last_was_newline = True

    def decode_text_token(s: str):
        if current_font:
            # CID/TrueType 字体：按 2 字节代码查 ToUnicode；查不到再退化逐字节
            data = s.encode("utf-16-be", errors="ignore")
            chars: list[str] = []
            used = False
            for k in range(0, len(data) - 1, 2):
                code = (data[k] << 8) | data[k + 1]
                if code in current_font:
                    chars.append(current_font[code])
                    used = True
                else:
                    # 单字节编码（标准字体）逐字节查
                    c1 = current_font.get(data[k])
                    c2 = current_font.get(data[k + 1])
                    if c1 is not None or c2 is not None:
                        if c1:
                            chars.append(c1)
                        if c2:
                            chars.append(c2)
                        used = True
                    else:
                        chars.append(s[k // 2] if k // 2 < len(s) else "")
            if used:
                return "".join(chars)
        return s

    def show(s: str, gap: str = " "):
        nonlocal last_was_newline
        text = decode_text_token(s)
        if not text:
            return
        if not last_was_newline and cur and not (cur[-1].endswith(" ") or text.startswith(" ")):
            cur.append(gap)
        cur.append(text)
        last_was_newline = False

    while i < n:
        c = content[i]
        if c in _WS or c == b"%"[0]:
            if c == b"%"[0]:
                while i < n and content[i] not in b"\r\n":
                    i += 1
            i += 1
            continue
        if content[i:i + 2] == b"BI":  # 内联图片，整体跳过
            eid = content.find(b"EI", i + 2)
            i = (eid + 2) if eid != -1 else n
            continue
        if content[i:i + 2] == b"<<":
            depth = 0
            while i < n:
                if content[i:i + 2] == b"<<":
                    depth += 1
                    i += 2
                elif content[i:i + 2] == b">>":
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                else:
                    i += 1
            stack.append("{}")
            continue
        if c == ord("["):
            stack.append("[")
            i += 1
            continue
        if c == ord("]"):
            arr: list = []
            while stack and stack[-1] != "[":
                arr.append(stack.pop())
            if stack:
                stack.pop()
            arr.reverse()
            stack.append(arr)
            i += 1
            continue
        if c == ord("("):  # 字面字符串
            depth_p = 1
            j = i + 1
            buf = bytearray()
            while j < n and depth_p:
                ch = content[j]
                if ch == 0x5C:
                    buf += content[j:j + 2]
                    j += 2
                    continue
                if ch == ord("("):
                    depth_p += 1
                elif ch == ord(")"):
                    depth_p -= 1
                    if depth_p == 0:
                        break
                buf.append(ch)
                j += 1
            stack.append(_decode_literal(bytes(buf)))
            i = j + 1
            continue
        if c == ord("<"):  # 十六进制字符串（CID 字体常用双字节码）
            j = content.find(b">", i + 1)
            j = j if j != -1 else i
            hexs = re.sub(rb"\s+", b"", content[i + 1:j])
            try:
                raw = bytes.fromhex(hexs.decode("ascii"))
            except ValueError:
                raw = b""
            if current_font:
                chars = []
                # 优先按双字节（Identity-H 类 CID 字体）
                for k in range(0, len(raw) - 1, 2):
                    code = (raw[k] << 8) | raw[k + 1]
                    chars.append(current_font.get(code, ""))
                # 双字节全未命中时退回单字节
                if not any(chars):
                    for byte in raw:
                        chars.append(current_font.get(byte, ""))
                stack.append("".join(chars))
            else:
                stack.append(raw.decode("latin-1"))
            i = j + 1
            continue
        if c == ord("/"):  # 名称
            j = i + 1
            while j < n and content[j] not in _WS + b"<>[]()/%":
                j += 1
            stack.append(content[i + 1:j].decode("latin-1"))
            i = j
            continue
        # 数字 / 操作符
        m = re.match(rb"[+\-]?\d*\.?\d+", content[i:])
        if m and (c in b"+-." or 48 <= c <= 57):
            token = m.group(0)
            num = float(token)
            stack.append(int(num) if b"." not in token else num)
            i += len(token)
            continue
        m = re.match(rb"[A-Za-z'\"]+", content[i:])
        if not m:
            i += 1
            continue
        op = m.group(0).decode("latin-1")
        i += len(m.group(0))
        if op == "BT":
            emit_newline()
        elif op == "Tf" and len(stack) >= 2:
            fname = stack[-2]
            current_font = fonts.get(fname)
            stack.clear()
        elif op in ("Td", "TD", "Tm", "T*"):
            emit_newline()
            stack.clear()
        elif op == "Tj" and stack:
            show(stack.pop() if isinstance(stack[-1], str) else "")
            stack.clear()
        elif op == "TJ" and stack and isinstance(stack[-1], list):
            for item in stack.pop():
                if isinstance(item, str):
                    show(item, gap="")
                elif isinstance(item, (int, float)) and item < -100:
                    cur.append(" ")
            stack.clear()
        elif op in ("'", '"'):
            emit_newline()
            if stack and isinstance(stack[-1], str):
                show(stack.pop())
            stack.clear()
        elif op == "ET":
            pass
        else:
            # 其它操作符：弹出其参数，避免污染栈
            stack.clear()
    emit_newline()
    return "\n".join(lines)


# ============================== 入口 ==============================

def extract(path: str | Path) -> ParsedDocument:
    p = Path(path)
    try:
        import pypdf  # noqa: F401
        return _extract_via_pypdf(p)
    except ImportError:
        pass
    data = p.read_bytes()
    reader = _PdfReader(data)
    pages = []
    for i, (content, resources) in enumerate(reader.collect_pages(), start=1):
        text = reader.page_text(content, resources)
        pages.append(Page(i, text.strip()))
    return ParsedDocument(pages=pages)
