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
    classification TEXT NOT NULL DEFAULT 'internal', -- 密级：internal/sensitive/secret
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
    classification TEXT,                          -- NULL=继承文档密级；可单独上调/下调
    index_version INTEGER NOT NULL DEFAULT 0,
    UNIQUE(document_id, chunk_index)
);

-- 片段级 ACL 规则（allow 授权 / deny 显式拒绝；支持有效期）
-- 一个 (chunk, subject) 可同时存在 allow 与 deny；deny 永远优先。
CREATE TABLE IF NOT EXISTS chunk_grants (
    id           INTEGER PRIMARY KEY,
    chunk_id     INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    subject_type TEXT NOT NULL,                   -- user / role / team / department
    subject_value TEXT NOT NULL,
    effect       TEXT NOT NULL DEFAULT 'allow',   -- allow / deny
    expires_at   REAL,                            -- NULL=长期；unix 秒，过期即失效
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_grants_chunk ON chunk_grants(chunk_id);

-- 文档级 ACL 规则（对文档的全部片段生效；典型用途：对某人/某团队整体 deny）
CREATE TABLE IF NOT EXISTS document_rules (
    id            INTEGER PRIMARY KEY,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    subject_type  TEXT NOT NULL,                  -- user / role / team / department
    subject_value TEXT NOT NULL,
    effect        TEXT NOT NULL DEFAULT 'allow',  -- allow / deny
    expires_at    REAL,                           -- NULL=长期
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_rules_doc ON document_rules(document_id);

CREATE TABLE IF NOT EXISTS query_logs (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    question    TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

-- 租户级单调递增的「索引代数」：任何会改变检索白名单/倒排内容的操作都 +1。
-- 用单调计数器而不是“版本号加总+行数”，避免删除后重传同构文档指纹撞回旧值。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta(key, value) VALUES ('index_generation', 0);
"""

# 增量迁移：对旧库补齐新列/新表
_COLUMN_MIGRATIONS = {
    "documents": [("classification", "TEXT NOT NULL DEFAULT 'internal'")],
    "chunks": [("classification", "TEXT")],
    "chunk_grants": [("effect", "TEXT NOT NULL DEFAULT 'allow'"),
                     ("expires_at", "REAL")],
}


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    for table, columns in _COLUMN_MIGRATIONS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")



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
    _migrate(conn)
    conn.commit()
    return conn


# ---------------- 文档 ----------------

def bump_index_generation(conn) -> int:
    """租户检索索引代数 +1，返回新值。任何改变检索白名单/倒排内容的操作都调用它。"""
    conn.execute("UPDATE meta SET value = value + 1 WHERE key='index_generation'")
    return conn.execute("SELECT value FROM meta WHERE key='index_generation'").fetchone()[0]


def index_generation(conn) -> int:
    return conn.execute("SELECT value FROM meta WHERE key='index_generation'").fetchone()[0]


def create_document(conn, *, tenant_id: int, title: str, source_name: str, file_type: str,
                    blob_path: str, full_text: str, visibility: str,
                    owner_user_id: int, owner_team: str | None, owner_dept: str | None,
                    classification: str = "internal") -> int:
    now = time.time()
    cur = conn.execute(
        """INSERT INTO documents(tenant_id, title, source_name, file_type, blob_path,
               char_count, full_text, visibility, classification,
               owner_user_id, owner_team, owner_dept,
               status, index_version, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'ready', 1, ?,?)""",
        (tenant_id, title, source_name, file_type, blob_path, len(full_text), full_text,
         visibility, classification,
         owner_user_id, owner_team, owner_dept, now, now),
    )
    return int(cur.lastrowid)


def bump_document_version(conn, document_id: int) -> None:
    """权限或内容变更时：文档版本号 +1，并使租户 BM25 内存索引（按代数缓存）失效。"""
    conn.execute(
        "UPDATE documents SET index_version=index_version+1, updated_at=? WHERE id=?",
        (time.time(), document_id),
    )
    bump_index_generation(conn)


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
    bump_index_generation(conn)
    return row["blob_path"]


# ---------------- 片段 ----------------

def insert_chunk(conn, *, document_id: int, chunk_index: int, heading_path: str | None,
                 content: str, char_start: int, char_end: int,
                 page_start: int | None, page_end: int | None,
                 visibility: str | None, classification: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO chunks(document_id, chunk_index, heading_path, content,
               char_start, char_end, page_start, page_end, visibility, classification,
               index_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,1)""",
        (document_id, chunk_index, heading_path, content, char_start, char_end,
         page_start, page_end, visibility, classification),
    )
    return int(cur.lastrowid)


def list_chunks(conn, document_id: int):
    return conn.execute(
        "SELECT * FROM chunks WHERE document_id=? ORDER BY chunk_index", (document_id,)
    ).fetchall()


def set_chunk_visibility(conn, chunk_id: int, visibility: str | None) -> None:
    conn.execute("UPDATE chunks SET visibility=? WHERE id=?", (visibility, chunk_id))


def set_chunk_classification(conn, chunk_id: int, classification: str | None) -> None:
    conn.execute("UPDATE chunks SET classification=? WHERE id=?", (classification, chunk_id))


def set_document_classification(conn, document_id: int, classification: str) -> None:
    conn.execute("UPDATE documents SET classification=? WHERE id=?",
                 (classification, document_id))


def add_grant(conn, chunk_id: int, subject_type: str, subject_value: str,
              effect: str = "allow", expires_at: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO chunk_grants(chunk_id, subject_type, subject_value, effect,"
        " expires_at, created_at) VALUES (?,?,?,?,?,?)",
        (chunk_id, subject_type, subject_value, effect, expires_at, time.time()),
    )
    return int(cur.lastrowid)


def list_grants(conn, chunk_id: int):
    return conn.execute(
        "SELECT id, subject_type, subject_value, effect, expires_at"
        " FROM chunk_grants WHERE chunk_id=? ORDER BY id",
        (chunk_id,),
    ).fetchall()


def delete_grant(conn, grant_id: int, chunk_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM chunk_grants WHERE id=? AND chunk_id=?", (grant_id, chunk_id)
    )
    return cur.rowcount > 0


# ---------------- 文档级规则（allow / deny，可时限） ----------------

def add_document_rule(conn, document_id: int, subject_type: str, subject_value: str,
                      effect: str = "deny", expires_at: float | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO document_rules(document_id, subject_type, subject_value, effect,"
        " expires_at, created_at) VALUES (?,?,?,?,?,?)",
        (document_id, subject_type, subject_value, effect, expires_at, time.time()),
    )
    return int(cur.lastrowid)


def list_document_rules(conn, document_id: int):
    return conn.execute(
        "SELECT id, subject_type, subject_value, effect, expires_at"
        " FROM document_rules WHERE document_id=? ORDER BY id",
        (document_id,),
    ).fetchall()


def delete_document_rule(conn, rule_id: int, document_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM document_rules WHERE id=? AND document_id=?", (rule_id, document_id)
    )
    return cur.rowcount > 0


def log_query(conn, user_id: int, question: str, hit_count: int) -> None:
    conn.execute(
        "INSERT INTO query_logs(user_id, question, hit_count, created_at) VALUES (?,?,?,?)",
        (user_id, question, hit_count, time.time()),
    )
