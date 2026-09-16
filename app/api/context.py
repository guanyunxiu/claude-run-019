"""每请求上下文：解析 Bearer 令牌，绑定全局库与对应租户库连接。

租户隔离在这里再次收口：租户 slug 与 tenant_id 全部来自服务端令牌记录，
任何 API 都不接受客户端自报的租户标识。
"""
from __future__ import annotations

import sqlite3

from ..core import db_global, db_tenant


class RequestContext:
    def __init__(self, method: str, path: str):
        self.method = method
        self.path = path
        self.user: dict | None = None
        self.gconn: sqlite3.Connection | None = None
        self.tconn: sqlite3.Connection | None = None
        self.tenant_slug: str | None = None

    @property
    def identity(self) -> dict:
        """传给权限/检索层的身份对象。"""
        u = self.user
        return {
            "user_id": u["user_id"],
            "tenant_id": u["tenant_id"],
            "tenant_role": u.get("tenant_role"),
            "team": u.get("team"),
            "department": u.get("department"),
            "clearance": u.get("clearance") or "internal",
            "email": u.get("email"),
            "display_name": u.get("display_name"),
        }

    def authenticate(self, gconn: sqlite3.Connection, auth_header: str | None) -> bool:
        if not auth_header or not auth_header.startswith("Bearer "):
            return False
        token = auth_header[len("Bearer "):].strip()
        row = db_global.resolve_session(gconn, token)
        if row is None:
            return False
        self.user = row
        self.token = token
        self.tenant_slug = row["tenant_slug"]
        # 只打开令牌所属租户的库——物理上无法跨租户查询
        self.tconn = db_tenant.connect(row["tenant_slug"], row["tenant_id"])
        return True

    def commit(self):
        if self.gconn:
            self.gconn.commit()
        if self.tconn:
            self.tconn.commit()

    def close(self):
        if self.tconn:
            self.tconn.close()
