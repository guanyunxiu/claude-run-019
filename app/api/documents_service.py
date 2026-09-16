"""文档入库流水线：保存原文件 -> 解析 -> 清洗 -> 语义分段 -> 写入租户库。"""
from __future__ import annotations

import secrets
from pathlib import Path

from .. import config
from ..core import db_tenant
from ..parsing import parse_file
from ..parsing.cleaner import clean_pages, join_pages
from ..parsing.chunker import build_chunks


def _ext(filename: str) -> str:
    return Path(filename).suffix.lower()


def ingest_upload(*, tconn, tenant_id: int, tenant_slug: str,
                  owner_user_id: int, filename: str, content: bytes,
                  title: str | None, visibility: str,
                  owner_team: str | None, owner_dept: str | None) -> dict:
    ext = _ext(filename)
    if ext not in config.SUPPORTED_EXT:
        raise ValueError(f"不支持的文件类型 {ext}")
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"文件超过大小上限 {config.MAX_UPLOAD_MB}MB")
    if visibility not in ("private", "team", "department", "public"):
        raise ValueError("visibility 必须是 private/team/department/public")

    file_type = ext.lstrip(".")
    # 原文件保存在该租户的独立目录（多租户文件级隔离）
    blob_dir = db_tenant.blob_dir_for(tenant_slug)
    stored_name = f"{secrets.token_hex(8)}{ext}"
    blob_path = blob_dir / stored_name
    blob_path.write_bytes(content)

    parsed = parse_file(blob_path, file_type)
    cleaned = clean_pages(parsed)
    full_text, page_spans = join_pages(cleaned.pages)

    if not full_text.strip():
        blob_path.unlink(missing_ok=True)
        raise ValueError(
            "未能从文档中提取到任何文本（可能是扫描件/纯图片 PDF，请先做 OCR 后再上传）"
        )

    doc_title = title or Path(filename).stem

    try:
        document_id = db_tenant.create_document(
            tconn,
            tenant_id=tenant_id,
            title=doc_title,
            source_name=filename,
            file_type=file_type,
            blob_path=stored_name,
            full_text=full_text,
            visibility=visibility,
            owner_user_id=owner_user_id,
            owner_team=owner_team,
            owner_dept=owner_dept,
        )
        specs = build_chunks(full_text, page_spans)
        for i, spec in enumerate(specs):
            db_tenant.insert_chunk(
                tconn,
                document_id=document_id,
                chunk_index=i,
                heading_path=spec.heading_path,
                content=spec.content,
                char_start=spec.char_start,
                char_end=spec.char_end,
                page_start=spec.page_start,
                page_end=spec.page_end,
                visibility=None,  # 默认继承文档可见性，可后续单独绑定
            )
        tconn.commit()
    except Exception:
        blob_path.unlink(missing_ok=True)
        raise

    return {
        "document_id": document_id,
        "title": doc_title,
        "source_name": filename,
        "file_type": file_type,
        "char_count": len(full_text),
        "chunk_count": len(specs),
        "visibility": visibility,
        "chunks_preview": [
            {
                "index": i + 1,
                "heading_path": s.heading_path,
                "chars": s.char_end - s.char_start,
                "page_start": s.page_start,
                "page_end": s.page_end,
            }
            for i, s in enumerate(specs[:20])
        ],
    }
