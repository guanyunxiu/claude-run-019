"""HTTP 服务与路由（仅标准库 http.server）。

接口一览：
  公开：  POST /api/auth/register   POST /api/auth/login   POST /api/auth/logout
          GET  /api/tenants（可浏览租户，用于选择登录上下文）
  认证后：GET  /api/me
  平台管理员：POST /api/admin/tenants
  租户管理员：GET/POST /api/admin/members，DELETE /api/admin/members/{user_id}（踢人并立即吊销令牌）
  文档：  POST /api/documents        GET /api/documents     GET /api/documents/{id}
          DELETE /api/documents/{id} GET /api/documents/{id}/chunks
          GET /api/documents/{id}/chunks/{cid}/source  （溯源原文定位）
  片段权限：PUT /api/chunks/{cid}/visibility
            POST /api/chunks/{cid}/grants  DELETE /api/chunks/{cid}/grants/{gid}
  检索/问答：POST /api/search  POST /api/ask
"""
from __future__ import annotations

import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .. import config
from ..core import db_global, db_tenant, permissions
from ..core.qa import answer_question
from ..core.retrieval import search_index
from ..core.security import verify_password
from .context import RequestContext
from .documents_service import ingest_upload
from .request_utils import parse_request_body


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_id(value: str, name: str = "id") -> int:
    """路径参数转整数；非法（如 undefined / abc）返回 400，而不是抛 500。"""
    try:
        ivalue = int(value)
        if ivalue <= 0:
            raise ValueError
        return ivalue
    except (TypeError, ValueError):
        raise ApiError(400, f"非法的 {name}: {value}")


def _parse_topk(value) -> int:
    try:
        k = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "top_k 必须是正整数")
    if not (1 <= k <= 50):
        raise ApiError(400, "top_k 取值范围为 1-50")
    return k


class Handler(BaseHTTPRequestHandler):
    server_version = "ResearchKB/1.0"

    # ---- 基础 IO ----
    def _send(self, status: int, payload, *, ctype="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            return parse_request_body(self)
        except ValueError as e:
            raise ApiError(400, str(e))

    def _json(self) -> dict:
        body = self._body()
        if body["type"] != "json":
            raise ApiError(400, "需要 application/json 请求体")
        return body["json"]

    # ---- 日志静音（避免污染测试输出） ----
    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---- 路由 ----
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _dispatch(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        ctx = RequestContext(self.command, path)
        ctx.gconn = db_global.connect()
        try:
            # 静态页面
            if self.command == "GET" and (path == "/" or path.startswith("/web/")):
                return self._serve_static(path)

            route = self._match_route(self.command, path)
            if route is None:
                raise ApiError(404, f"接口不存在: {self.command} {path}")

            auth_required, handler_fn, kwargs = route
            if auth_required:
                if not ctx.authenticate(ctx.gconn, self.headers.get("Authorization")):
                    raise ApiError(401, "未登录或登录已过期")
            ctx.gconn.commit()
            result = handler_fn(self, ctx, **kwargs)
            ctx.commit()
            if result is not None:
                self._send(200, result)
        except ApiError as e:
            ctx.gconn.rollback()
            if ctx.tconn:
                ctx.tconn.rollback()
            self._send(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            ctx.gconn.rollback()
            if ctx.tconn:
                ctx.tconn.rollback()
            self._send(500, {"error": f"服务器内部错误: {e}"})
        finally:
            ctx.close()
            ctx.gconn.close()

    # ---- 路由表 ----
    _ROUTES = []

    @classmethod
    def route(cls, method, pattern, auth=True):
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", "^" + pattern + "$")
        rx = re.compile(regex)

        def deco(fn):
            cls._ROUTES.append((method, rx, auth, fn))
            return fn
        return deco

    @classmethod
    def _match_route(cls, method, path):
        for m, rx, auth, fn in cls._ROUTES:
            if m != method:
                continue
            match = rx.match(path)
            if match:
                return auth, fn, match.groupdict()
        return None

    # ---- 静态资源 ----
    def _serve_static(self, path: str):
        if path == "/":
            file = config.WEB_DIR / "index.html"
        else:
            rel = path.removeprefix("/web/")
            file = (config.WEB_DIR / rel).resolve()
            if not str(file).startswith(str(config.WEB_DIR.resolve())):
                raise ApiError(403, "非法路径")
        if not file.is_file():
            raise ApiError(404, "页面不存在")
        ctype = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        if file.suffix in (".html", ".js", ".css"):
            ctype += "; charset=utf-8"
        self._send(200, file.read_bytes(), ctype=ctype)


# ============================== 认证 / 账号 ==============================

@Handler.route("POST", "/api/auth/register", auth=False)
def register(h, ctx):
    data = h._json()
    email = (data.get("email") or "").strip().lower()
    name = (data.get("display_name") or "").strip()
    password = data.get("password") or ""
    if not email or not name or len(password) < 6:
        raise ApiError(400, "email、display_name 必填，密码至少 6 位")
    if db_global.get_user_by_email(ctx.gconn, email):
        raise ApiError(409, "该邮箱已注册")
    uid = db_global.create_user(ctx.gconn, email, name, password)
    ctx.gconn.commit()
    return {"user_id": uid, "email": email, "display_name": name,
            "message": "注册成功，请联系平台管理员加入租户后登录"}


@Handler.route("POST", "/api/auth/login", auth=False)
def login(h, ctx):
    data = h._json()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    tenant_slug = (data.get("tenant_slug") or "").strip()
    user = db_global.get_user_by_email(ctx.gconn, email)
    if user is None or not verify_password(password, user["password_hash"]):
        raise ApiError(401, "邮箱或密码错误")
    tenants = db_global.list_user_tenants(ctx.gconn, user["id"])
    if not tenants:
        raise ApiError(403, "账号尚未加入任何租户，请联系管理员")
    if tenant_slug:
        tenant = next((t for t in tenants if t["slug"] == tenant_slug), None)
        if tenant is None:
            raise ApiError(403, "您不属于该租户")
    else:
        tenant = tenants[0]
    token = db_global.create_session(ctx.gconn, user["id"], tenant["id"])
    ctx.gconn.commit()
    return {
        "token": token,
        "user": {"id": user["id"], "email": email, "display_name": user["display_name"]},
        "tenant": {"id": tenant["id"], "slug": tenant["slug"], "name": tenant["name"],
                   "role": tenant["role"]},
    }


@Handler.route("POST", "/api/auth/logout")
def logout(h, ctx):
    db_global.revoke_session(ctx.gconn, ctx.token)
    return {"message": "已退出登录"}


@Handler.route("GET", "/api/me")
def me(h, ctx):
    u = ctx.user
    return {
        "user": {"id": u["user_id"], "email": u["email"],
                 "display_name": u["display_name"],
                 "is_platform_admin": bool(u["is_platform_admin"])},
        "tenant": {"id": u["tenant_id"], "slug": u["tenant_slug"],
                   "name": u["tenant_name"], "role": u.get("tenant_role")},
        "team": u.get("team"), "department": u.get("department"),
    }


@Handler.route("GET", "/api/tenants", auth=False)
def list_tenants_public(h, ctx):
    rows = db_global.list_tenants(ctx.gconn)
    return [{"id": r["id"], "slug": r["slug"], "name": r["name"]} for r in rows]


# ============================== 管理：租户 / 成员 ==============================

@Handler.route("POST", "/api/admin/tenants")
def create_tenant(h, ctx):
    if not ctx.user["is_platform_admin"]:
        raise ApiError(403, "仅平台管理员可创建租户")
    data = h._json()
    slug = (data.get("slug") or "").strip().lower()
    name = (data.get("name") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,31}", slug):
        raise ApiError(400, "slug 需为 2-32 位小写字母/数字/-/_")
    if not name:
        raise ApiError(400, "name 必填")
    if db_global.get_tenant_by_slug(ctx.gconn, slug):
        raise ApiError(409, "租户标识已存在")
    tid = db_global.create_tenant(ctx.gconn, slug, name)
    # 惰性初始化租户库（独立文件）
    db_tenant.connect(slug, tid).close()
    # 创建者自动成为租户管理员（如指定）
    if data.get("admin_email"):
        admin = db_global.get_user_by_email(ctx.gconn, data["admin_email"].strip().lower())
        if admin:
            db_global.add_tenant_member(ctx.gconn, tid, admin["id"], "admin")
    return {"tenant_id": tid, "slug": slug, "name": name}


@Handler.route("GET", "/api/admin/members")
def list_members(h, ctx):
    if ctx.user.get("tenant_role") != "admin":
        raise ApiError(403, "仅租户管理员可查看成员")
    rows = db_global.list_tenant_members(ctx.gconn, ctx.user["tenant_id"])
    return [{"user_id": r["id"], "email": r["email"], "display_name": r["display_name"],
             "role": r["role"], "team": r["team"], "department": r["department"]}
            for r in rows]


@Handler.route("POST", "/api/admin/members")
def add_member(h, ctx):
    if ctx.user.get("tenant_role") != "admin":
        raise ApiError(403, "仅租户管理员可管理成员")
    data = h._json()
    email = (data.get("email") or "").strip().lower()
    role = data.get("role", "member")
    if role not in ("admin", "member"):
        raise ApiError(400, "role 必须是 admin 或 member")
    user = db_global.get_user_by_email(ctx.gconn, email)
    if user is None:
        raise ApiError(404, "用户不存在，请先让其注册")
    db_global.add_tenant_member(
        ctx.gconn, ctx.user["tenant_id"], user["id"], role,
        data.get("team") or None, data.get("department") or None,
    )
    return {"message": f"已将 {email} 加入租户", "role": role,
            "team": data.get("team"), "department": data.get("department")}


@Handler.route("DELETE", "/api/admin/members/{user_id}")
def remove_member(h, ctx, user_id):
    """把用户移出租户：立即删除成员关系并吊销其在本租户的全部会话。"""
    if ctx.user.get("tenant_role") != "admin":
        raise ApiError(403, "仅租户管理员可管理成员")
    target_id = _parse_id(user_id, "user_id")
    if target_id == ctx.user["user_id"]:
        raise ApiError(400, "不能移除当前登录的自己")
    removed = db_global.remove_tenant_member(ctx.gconn, ctx.user["tenant_id"], target_id)
    if not removed:
        raise ApiError(404, "该用户不是当前租户成员")
    return {"message": "成员已移除，其登录令牌已立即失效"}


# ============================== 文档 ==============================

@Handler.route("POST", "/api/documents")
def upload_document(h, ctx):
    body = h._body()
    if body["type"] != "multipart":
        raise ApiError(400, "请使用 multipart/form-data 上传文件")
    files = body["files"]
    if "file" not in files:
        raise ApiError(400, "缺少 file 字段")
    f = files["file"]
    fields = body["fields"]
    visibility = fields.get("visibility", "private")
    result = ingest_upload(
        tconn=ctx.tconn,
        tenant_id=ctx.user["tenant_id"],
        tenant_slug=ctx.tenant_slug,
        owner_user_id=ctx.user["user_id"],
        filename=f["filename"],
        content=f["content"],
        title=fields.get("title") or None,
        visibility=visibility,
        owner_team=fields.get("owner_team") or ctx.user.get("team"),
        owner_dept=fields.get("owner_dept") or ctx.user.get("department"),
    )
    return result


@Handler.route("GET", "/api/documents")
def list_documents(h, ctx):
    where, params = permissions.accessible_document_where(ctx.identity)
    rows = ctx.tconn.execute(
        f"""SELECT d.id, d.title, d.source_name, d.file_type, d.char_count,
                   d.visibility, d.owner_user_id, d.owner_team, d.owner_dept,
                   d.created_at, d.updated_at,
                   (SELECT COUNT(*) FROM chunks c WHERE c.document_id=d.id) AS chunk_count
            FROM documents d WHERE {where} ORDER BY d.id DESC""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _load_managed_doc(ctx, doc_id) -> dict:
    """需要管理权限（删除等）的文档加载。"""
    doc = db_tenant.get_document(ctx.tconn, doc_id, ctx.user["tenant_id"])
    if doc is None:
        raise ApiError(404, "文档不存在或不属于当前租户")
    if not permissions.can_manage_document(ctx.identity, doc):
        raise ApiError(403, "无权管理该文档")
    return doc


def _load_readable_doc(ctx, doc_id) -> dict:
    """需要读权限（详情/片段列表）的文档加载。同团队/同部门/被授权成员可读。"""
    doc = db_tenant.get_document(ctx.tconn, doc_id, ctx.user["tenant_id"])
    if doc is None or not permissions.can_read_document(ctx.tconn, ctx.identity, doc):
        raise ApiError(404, "文档不存在或无权访问")
    return doc


@Handler.route("GET", "/api/documents/{doc_id}")
def get_document(h, ctx, doc_id):
    doc = _load_readable_doc(ctx, _parse_id(doc_id, "document_id"))
    return {k: doc[k] for k in
            ("id", "title", "source_name", "file_type", "char_count", "visibility",
             "owner_user_id", "owner_team", "owner_dept", "created_at", "updated_at")}


@Handler.route("DELETE", "/api/documents/{doc_id}")
def delete_document(h, ctx, doc_id):
    doc = _load_managed_doc(ctx, _parse_id(doc_id, "document_id"))
    blob_rel = db_tenant.delete_document(ctx.tconn, doc["id"], ctx.user["tenant_id"])
    ctx.tconn.commit()
    if blob_rel:
        (db_tenant.blob_dir_for(ctx.tenant_slug) / blob_rel).unlink(missing_ok=True)
    return {"message": "文档已删除", "document_id": doc["id"]}


@Handler.route("GET", "/api/documents/{doc_id}/chunks")
def list_document_chunks(h, ctx, doc_id):
    """列出文档片段及每个片段的可见性/授权（仅管理者看全量；普通用户只看可见片段）。"""
    doc_id = _parse_id(doc_id, "document_id")
    doc = _load_readable_doc(ctx, doc_id)
    identity = ctx.identity
    is_manager = permissions.can_manage_document(identity, doc)
    chunks = db_tenant.list_chunks(ctx.tconn, doc["id"])
    result = []
    for c in chunks:
        item = {
            "document_id": doc["id"],
            "chunk_id": c["id"], "chunk_index": c["chunk_index"],
            "heading_path": c["heading_path"],
            "visibility": c["visibility"] or f"inherit({doc['visibility']})",
            "char_start": c["char_start"], "char_end": c["char_end"],
            "page_start": c["page_start"], "page_end": c["page_end"],
            "content": c["content"][:300],
        }
        if is_manager:
            item["grants"] = [dict(g) for g in db_tenant.list_grants(ctx.tconn, c["id"])]
        result.append(item)
    if not is_manager:
        # 非管理者：用权限谓词过滤
        w, p = permissions.accessible_chunks_where(identity)
        allowed = {
            r["id"] for r in ctx.tconn.execute(
                "SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id WHERE " + w, p
            ).fetchall()
        }
        result = [x for x in result if x["chunk_id"] in allowed]
    return result


@Handler.route("GET", "/api/documents/{doc_id}/chunks/{chunk_id}/source")
def chunk_source(h, ctx, doc_id, chunk_id):
    """溯源：返回片段在原文中的精确定位与上下文（受权限控制）。"""
    doc_id = _parse_id(doc_id, "document_id")
    chunk_id = _parse_id(chunk_id, "chunk_id")
    where, params = permissions.accessible_chunks_where(ctx.identity)
    row = ctx.tconn.execute(
        f"""SELECT c.*, d.title, d.source_name, d.full_text
            FROM chunks c JOIN documents d ON d.id=c.document_id
            WHERE c.id=? AND c.document_id=? AND {where}""",
        [chunk_id, doc_id] + params,
    ).fetchone()
    if row is None:
        raise ApiError(404, "片段不存在或无权访问")
    full = row["full_text"]
    s, e = row["char_start"], row["char_end"]
    ctx_start = max(0, s - 120)
    ctx_end = min(len(full), e + 120)
    return {
        "title": row["title"], "source_name": row["source_name"],
        "heading_path": row["heading_path"],
        "page_start": row["page_start"], "page_end": row["page_end"],
        "char_range": [s, e],
        "before": full[ctx_start:s],
        "exact_text": full[s:e],
        "after": full[e:ctx_end],
    }


# ============================== 片段级权限 ==============================

def _load_managed_chunk(ctx, chunk_id):
    chunk_id = _parse_id(chunk_id, "chunk_id")
    row = ctx.tconn.execute(
        "SELECT * FROM chunks WHERE id=?", (chunk_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "片段不存在")
    doc = db_tenant.get_document(ctx.tconn, row["document_id"], ctx.user["tenant_id"])
    if doc is None:
        raise ApiError(404, "片段不存在")
    if not permissions.can_manage_chunk(ctx.identity, ctx.tconn, row):
        raise ApiError(403, "仅文档所有者或租户管理员可修改片段权限")
    return row


@Handler.route("PUT", "/api/chunks/{chunk_id}/visibility")
def set_chunk_visibility(h, ctx, chunk_id):
    row = _load_managed_chunk(ctx, chunk_id)
    data = h._json()
    vis = data.get("visibility")
    if vis is not None and vis not in permissions.VALID_VIS:
        raise ApiError(400, f"visibility 必须是 {permissions.VALID_VIS} 或 null(继承)")
    db_tenant.set_chunk_visibility(ctx.tconn, row["id"], vis)
    db_tenant.bump_document_version(ctx.tconn, row["document_id"])
    return {"chunk_id": row["id"], "visibility": vis,
            "message": "片段可见性已更新" + ("（继承文档）" if vis is None else "")}


@Handler.route("POST", "/api/chunks/{chunk_id}/grants")
def add_chunk_grant(h, ctx, chunk_id):
    row = _load_managed_chunk(ctx, chunk_id)
    data = h._json()
    stype = data.get("subject_type")
    svalue = (data.get("subject_value") or "").strip()
    if stype not in permissions.VALID_SUBJECTS:
        raise ApiError(400, f"subject_type 必须是 {permissions.VALID_SUBJECTS}")
    if not svalue:
        raise ApiError(400, "subject_value 必填（用户ID/角色名/团队名/部门名）")
    if stype == "user":
        if not svalue.isdigit():
            raise ApiError(400, "user 授权的 subject_value 必须是用户数字 ID")
        svalue = str(int(svalue))
    gid = db_tenant.add_grant(ctx.tconn, row["id"], stype, svalue)
    db_tenant.bump_document_version(ctx.tconn, row["document_id"])
    return {"grant_id": gid, "chunk_id": row["id"],
            "subject_type": stype, "subject_value": svalue}


@Handler.route("DELETE", "/api/chunks/{chunk_id}/grants/{grant_id}")
def remove_chunk_grant(h, ctx, chunk_id, grant_id):
    row = _load_managed_chunk(ctx, chunk_id)
    ok = db_tenant.delete_grant(
        ctx.tconn, _parse_id(grant_id, "grant_id"), row["id"]
    )
    if not ok:
        raise ApiError(404, "授权记录不存在")
    db_tenant.bump_document_version(ctx.tconn, row["document_id"])
    return {"message": "授权已移除"}


# ============================== 检索 / 问答 ==============================

@Handler.route("POST", "/api/search")
def search(h, ctx):
    data = h._json()
    query = (data.get("query") or "").strip()
    if not query:
        raise ApiError(400, "query 必填")
    top_k = _parse_topk(data.get("top_k") or config.SEARCH_TOP_K)
    results = search_index.search(ctx.tenant_slug, ctx.tconn, ctx.identity, query, top_k)
    db_tenant.log_query(ctx.tconn, ctx.user["user_id"], query, len(results))
    return {"query": query, "count": len(results), "results": results}


@Handler.route("POST", "/api/ask")
def ask(h, ctx):
    data = h._json()
    question = (data.get("question") or data.get("query") or "").strip()
    if not question:
        raise ApiError(400, "question 必填")
    top_k = _parse_topk(data.get("top_k") or config.SEARCH_TOP_K)
    result = answer_question(ctx.tenant_slug, ctx.tconn, ctx.identity, question, top_k)
    db_tenant.log_query(ctx.tconn, ctx.user["user_id"], question, len(result["sources"]))
    return result


# ============================== 启动 ==============================

def build_server(host=None, port=None, verbose=False) -> ThreadingHTTPServer:
    db_global.init_db()
    server = ThreadingHTTPServer((host or config.HOST, port or config.PORT), Handler)
    server.verbose = verbose
    return server


def main():
    server = build_server(verbose=True)
    print(f"权限感知型科研知识库问答系统已启动: http://{config.HOST}:{config.PORT}")
    print(f"数据目录: {config.DATA_DIR}（每租户独立 .db 与 blobs 子目录）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭...")
        server.shutdown()
