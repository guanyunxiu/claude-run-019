"""全局数据库：仅存放租户、跨租户登录账号、会话令牌、租户成员关系。

多租户硬隔离设计（第 1 层：独立账号域）：
  - 每个租户拥有完全独立的 SQLite 库文件（data/tenants/<slug>.db）与
    独立文件存储目录（data/blobs/<slug>/）；
  - 全局库中不保存任何业务文档/片段/权限数据；
  - 每次 API 请求都由令牌解析出 (tenant_id, user_id)，后续数据库连接
    只打开对应租户库，应用层从不跨租户 JOIN 或查询。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional

from .security import hash_password, new_token
from .. import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,          -- 库文件/目录名（硬隔离边界）
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_platform_admin INTEGER NOT NULL DEFAULT 0,  -- 平台管理员（租户管理）
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tenant_users (
    tenant_id  INTEGER NOT NULL REFERENCES tenants(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    role       TEXT NOT NULL DEFAULT 'member',  -- admin / member
    team       TEXT,                            -- 所属团队（租户内业务分组）
    department TEXT,                            -- 所属部门
    clearance  TEXT NOT NULL DEFAULT 'internal', -- 密级许可：internal/sensitive/secret
    PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    tenant_id  INTEGER NOT NULL REFERENCES tenants(id),  -- 登录时选定的租户上下文
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_used  REAL NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """对已存在的全局库做增量列迁移（SQLite 无 DROP COLUMN IF EXISTS）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tenant_users)")}
    if "clearance" not in cols:
        conn.execute("ALTER TABLE tenant_users ADD COLUMN clearance TEXT NOT NULL DEFAULT 'internal'")


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.GLOBAL_DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


# ---------------- 租户 ----------------

def create_tenant(conn: sqlite3.Connection, slug: str, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO tenants(slug, name, created_at) VALUES (?,?,?)",
        (slug, name, time.time()),
    )
    return int(cur.lastrowid)


def get_tenant(conn: sqlite3.Connection, tenant_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()


def get_tenant_by_slug(conn: sqlite3.Connection, slug: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM tenants WHERE slug=?", (slug,)).fetchone()


def list_tenants(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, slug, name, created_at FROM tenants ORDER BY id").fetchall()


# ---------------- 用户 ----------------

def create_user(conn, email: str, display_name: str, password: str,
                is_platform_admin: bool = False) -> int:
    cur = conn.execute(
        "INSERT INTO users(email, display_name, password_hash, is_platform_admin, created_at)"
        " VALUES (?,?,?,?,?)",
        (email, display_name, hash_password(password), 1 if is_platform_admin else 0, time.time()),
    )
    return int(cur.lastrowid)


def get_user_by_email(conn, email: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()


def get_user(conn, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, email, display_name, is_platform_admin FROM users WHERE id=?", (user_id,)
    ).fetchone()


def add_tenant_member(conn, tenant_id: int, user_id: int, role: str = "member",
                      team: str | None = None, department: str | None = None,
                      clearance: str = "internal") -> None:
    conn.execute(
        "INSERT INTO tenant_users(tenant_id, user_id, role, team, department, clearance)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id, user_id) DO UPDATE SET"
        " role=excluded.role, team=excluded.team, department=excluded.department,"
        " clearance=excluded.clearance",
        (tenant_id, user_id, role, team, department, clearance),
    )


def upsert_tenant_member(conn, tenant_id: int, user_id: int, role: str = "member",
                         team: str | None = None, department: str | None = None,
                         clearance: str = "internal") -> tuple[bool, dict | None]:
    """新增或更新成员，返回 (是否为新增, 改前快照)。

    已存在时执行更新并返回旧值，便于调用方区分 member.add 与成员信息变更留痕。
    """
    old = get_membership(conn, tenant_id, user_id)
    before = None
    if old is not None:
        before = {"role": old["role"], "team": old["team"],
                  "department": old["department"], "clearance": old["clearance"]}
    conn.execute(
        "INSERT INTO tenant_users(tenant_id, user_id, role, team, department, clearance)"
        " VALUES (?,?,?,?,?,?) ON CONFLICT(tenant_id, user_id) DO UPDATE SET"
        " role=excluded.role, team=excluded.team, department=excluded.department,"
        " clearance=excluded.clearance",
        (tenant_id, user_id, role, team, department, clearance),
    )
    return old is None, before


def update_member_clearance(conn, tenant_id: int, user_id: int, clearance: str) -> bool:
    cur = conn.execute(
        "UPDATE tenant_users SET clearance=? WHERE tenant_id=? AND user_id=?",
        (clearance, tenant_id, user_id),
    )
    return cur.rowcount > 0


def get_membership(conn, tenant_id: int, user_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tenant_users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
    ).fetchone()


def remove_tenant_member(conn, tenant_id: int, user_id: int) -> bool:
    """把用户移出租户，并立即吊销其在该租户上下文下的全部会话。

    返回是否真的移除了成员关系。
    """
    cur = conn.execute(
        "DELETE FROM tenant_users WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
    )
    removed = cur.rowcount > 0
    # 吊销该用户在本租户的所有登录会话（旧 Bearer 令牌立刻失效）
    conn.execute(
        "DELETE FROM sessions WHERE tenant_id=? AND user_id=?", (tenant_id, user_id)
    )
    return removed


def list_user_tenants(conn, user_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.*, tu.role FROM tenants t
           JOIN tenant_users tu ON tu.tenant_id=t.id
           WHERE tu.user_id=? ORDER BY t.id""",
        (user_id,),
    ).fetchall()


def list_tenant_members(conn, tenant_id: int):
    return conn.execute(
        """SELECT u.id, u.email, u.display_name, tu.role, tu.team, tu.department, tu.clearance
           FROM tenant_users tu JOIN users u ON u.id=tu.user_id
           WHERE tu.tenant_id=? ORDER BY u.id""",
        (tenant_id,),
    ).fetchall()


# ---------------- 会话令牌 ----------------

SESSION_TTL = 12 * 3600


def create_session(conn, user_id: int, tenant_id: int) -> str:
    token = new_token()
    now = time.time()
    conn.execute(
        "INSERT INTO sessions(token, user_id, tenant_id, created_at, expires_at, last_used)"
        " VALUES (?,?,?,?,?,?)",
        (token, user_id, tenant_id, now, now + SESSION_TTL, now),
    )
    return token


def resolve_session(conn, token: str) -> Optional[dict]:
    """校验令牌并返回登录上下文。过期令牌立即删除。

    安全要点：与 tenant_users 使用 INNER JOIN——用户一旦被移出租户，其在该
    租户上下文下的所有会话立即失效，旧 Bearer 令牌无法再鉴权或写入租户库。
    """
    row = conn.execute(
        """SELECT s.token, s.user_id, s.tenant_id, s.expires_at,
                  u.email, u.display_name, u.is_platform_admin,
                  tu.role AS tenant_role, tu.team, tu.department, tu.clearance,
                  t.slug AS tenant_slug, t.name AS tenant_name
           FROM sessions s
           JOIN users u   ON u.id = s.user_id
           JOIN tenants t ON t.id = s.tenant_id
           JOIN tenant_users tu ON tu.tenant_id=s.tenant_id AND tu.user_id=s.user_id
           WHERE s.token=?""",
        (token,),
    ).fetchone()
    if row is None:
        # 可能是过期，也可能是成员关系已不存在：顺手清理失效/孤儿会话
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    if row["expires_at"] < time.time():
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    # 双重保险：role 必须是有效值，否则按未授权处理
    if not row["tenant_role"]:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    conn.execute("UPDATE sessions SET last_used=? WHERE token=?", (time.time(), token))
    return dict(row)


def revoke_session(conn, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
