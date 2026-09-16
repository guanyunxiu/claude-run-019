"""multipart/form-data 与 JSON 请求体解析（仅用标准库 email 模块）。"""
from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import default


def parse_request_body(handler) -> dict:
    """返回 {"type": "json"|"multipart", ...}。

    multipart: {"fields": {name: str}, "files": {name: {"filename","content","content_type"}}}
    json:      {"json": dict}
    """
    ctype = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length) if length else b""

    if ctype.startswith("application/json"):
        try:
            return {"type": "json", "json": json.loads(raw.decode("utf-8") or "{}")}
        except json.JSONDecodeError:
            raise ValueError("请求体不是合法 JSON")

    if ctype.startswith("multipart/form-data"):
        header = (
            f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        )
        msg = BytesParser(policy=default).parsebytes(header + raw)
        fields: dict[str, str] = {}
        files: dict[str, dict] = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename is not None:
                files[name] = {
                    "filename": filename,
                    "content": payload,
                    "content_type": part.get_content_type(),
                }
            else:
                fields[name] = payload.decode("utf-8", errors="replace")
        return {"type": "multipart", "fields": fields, "files": files}

    raise ValueError(f"不支持的 Content-Type: {ctype or '(空)'}")
