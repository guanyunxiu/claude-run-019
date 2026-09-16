"""租户数据库：每个租户一个独立 SQLite 文件，存放文档、片段、权限等全部业务数据。

隔离原则：本模块只接受服务端根据令牌解析出的 tenant_slug，从不接受客户端
直接传入的库路径，杜绝跨租户库访问。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .. import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    tenant_id     INTEGER NOT NULL,              -- 冗余存租户 id，便于审计核对
    title         TEXT NOT NULL,
    source_name   TEXT NOT NULL,                  -- 原始文件名
    file_type     TEXT NOT NULL,                  -- pdf/docx/txt/md/log
    blob_path     TEXT NOT NULL,                  -- 该租户目录下的相对路径
    char_count    INTEGER NOT NULL DEFAULT 0,
    full_text     TEXT NOT NULL DEFAULT '',       -- 清洗后的全文（片段溯源定位）
    visibility    TEXT NOT NULL DEFAULT 'private',  -- private/team/department/public
    owner_user_id INTEGER NOT NULL,
    owner_team    TEXT,
    owner_dept    TEXT,
    status        TEXT NOT NULL DEFAULT 'ready',
    index_version INTEGER NOT NULL DEFAULT 0,     -- 内容/权限变更时自增，使检索缓存失效
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,               -- 片段在文档中的顺序
    heading_path TEXT,                            -- 所属章节标题路径（语义结构）
    content      TEXT NOT NULL,
    char_start   INTEGER NOT NULL,               -- 在全文中的起始偏移（溯源定位）
    char_end     INTEGER NOT NULL,
    page_start   INTEGER,
    page_end     INTEGER,
    visibility   TEXT,                            -- NULL=继承文档；可单独覆盖
    index_version INTEGER NOT NULL DEFAULT 0,
    UNIQUE(document_id, chunk_index)
);

-- 片段级附加授权（细粒度权限绑定：用户/角色/团队/部门）
CREATE TABLE IF NOT EXISTS chunk_grants (
    id           INTEGER PRIMARY KEY,
    chunk_id     INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL,                   -- user / role / team / department
    subject_value TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_grants_chunk ON chunk_grants(chunk_id);

CREATE TABLE IF NOT EXISTS query_logs (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    question    TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
"""


def db_path_for(tenant_slug: str) -> Path:
    # slug 仅允许安全字符，防止任何路径穿越
    safe = "".join(c for c in tenant_slug if c.isalnum() or c in "-_")
    if not safe or safe != tenant_slug:
        raise ValueError("非法租户标识")
    return config.TENANT_DIR / f"{safe}.db"


def blob_dir_for(tenant_slug: str) -> Path:
    safe = "".join(c for c in tenant_slug if c.isalnum() or c in "-_")
    if not safe or safe != tenant_slug:
        raise ValueError("非法租户标识")
    return config.BLOB_DIR / safe


def connect(tenant_slug: str, tenant_id: int) -> sqlite3.Connection:
    """打开（并惰性初始化）指定租户的数据库连接。"""
    config.TENANT_DIR.mkdir(parents=True, exist_ok=True)
    blob_dir_for(tenant_slug).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path_for(tenant_slug), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    # tenant_id 一致性闸门：建库后所有业务写入都必须带本租户 id
    conn.execute("PRAGMA user_version")
    return conn


# ---------------- 文档 ----------------

def create_document(conn, *, tenant_id: int, title: str, source_name: str, file_type: str,
                    blob_path: str, full_text: str, visibility: str,
                    owner_user_id: int, owner_team: str | None, owner_dept: str | None) -> int:
    now = time.time()
    cur = conn.execute(
        """INSERT INTO documents(tenant_id, title, source_name, file_type, blob_path,
               char_count, full_text, visibility, owner_user_id, owner_team, owner_dept,
               status, index_version, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'ready', 1, ?,?)""",
        (tenant_id, title, source_name, file_type, blob_path, len(full_text), full_text,
         visibility, owner_user_id, owner_team, owner_dept, now, now),
    )
    return int(cur.lastrowid)


def bump_document_version(conn, document_id: int) -> None:
    """权限或内容变更时递增版本号，使该租户的 BM25 内存索引缓存失效。"""
    conn.execute(
        "UPDATE documents SET index_version=index_version+1, updated_at=? WHERE id=?",
        (time.time(), document_id),
    )


def get_document(conn, document_id: int, tenant_id: int):
    row = conn.execute(
        "SELECT * FROM documents WHERE id=? AND tenant_id=?", (document_id, tenant_id)
    ).fetchone()
    return row


def list_documents(conn, tenant_id: int):
    return conn.execute(
        """SELECT id, title, source_name, file_type, char_count, visibility,
                  owner_user_id, owner_team, owner_dept, created_at, updated_at,
                  (SELECT COUNT(*) FROM chunks c WHERE c.document_id=documents.id) AS chunk_count
           FROM documents WHERE tenant_id=? ORDER BY id DESC""",
        (tenant_id,),
    ).fetchall()


def delete_document(conn, document_id: int, tenant_id: int) -> str | None:
    row = get_document(conn, document_id, tenant_id)
    if row is None:
        return None
    conn.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
    conn.execute("DELETE FROM documents WHERE id=? AND tenant_id=?", (document_id, tenant_id))
    return row["blob_path"]


# ---------------- 片段 ----------------

def insert_chunk(conn, *, document_id: int, chunk_index: int, heading_path: str | None,
                 content: str, char_start: int, char_end: int,
                 page_start: int | None, page_end: int | None,
                 visibility: str | None) -> int:
    cur = conn.execute(
        """INSERT INTO chunks(document_id, chunk_index, heading_path, content,
               char_start, char_end, page_start, page_end, visibility, index_version)
           VALUES (?,?,?,?,?,?,?,?,?,1)""",
        (document_id, chunk_index, heading_path, content, char_start, char_end,
         page_start, page_end, visibility),
    )
    return int(cur.lastrowid)


def list_chunks(conn, document_id: int):
    return conn.execute(
        "SELECT * FROM chunks WHERE document_id=? ORDER BY chunk_index", (document_id,)
    ).fetchall()


def set_chunk_visibility(conn, chunk_id: int, visibility: str | None) -> None:
    conn.execute("UPDATE chunks SET visibility=? WHERE id=?", (visibility, chunk_id))


def add_grant(conn, chunk_id: int, subject_type: str, subject_value: str) -> int:
    cur = conn.execute(
        "INSERT INTO chunk_grants(chunk_id, subject_type, subject_value, created_at)"
        " VALUES (?,?,?,?)",
        (chunk_id, subject_type, subject_value, time.time()),
    )
    return int(cur.lastrowid)


def list_grants(conn, chunk_id: int):
    return conn.execute(
        "SELECT id, subject_type, subject_value FROM chunk_grants WHERE chunk_id=? ORDER BY id",
        (chunk_id,),
    ).fetchall()


def delete_grant(conn, grant_id: int, chunk_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM chunk_grants WHERE id=? AND chunk_id=?", (grant_id, chunk_id)
    )
    return cur.rowcount > 0


def log_query(conn, user_id: int, question: str, hit_count: int) -> None:
    conn.execute(
        "INSERT INTO query_logs(user_id, question, hit_count, created_at) VALUES (?,?,?,?)",
        (user_id, question, hit_count, time.time()),
    )
