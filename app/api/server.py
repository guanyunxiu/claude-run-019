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
    def _send(self, status: int, payload, *, ctype="application/json; charset=utf-8",
              extra_headers=None):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        else:
            body = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        extra_headers = extra_headers or {}
        # 允许调用方通过 extra_headers 覆盖 Content-Type（如 CSV 导出），避免重复头
        ctype = extra_headers.pop("Content-Type", ctype)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra_headers.items():
            self.send_header(k, v)
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
                # 审计来源 IP：优先反向代理透传的 X-Forwarded-For
                fwd = self.headers.get("X-Forwarded-For")
                ctx.ip = (fwd.split(",")[0].strip() if fwd else
                          (self.client_ip if hasattr(self, "client_ip") else
                           self.client_address[0]))
            ctx.gconn.commit()
            result = handler_fn(self, ctx, **kwargs)
            ctx.commit()
            if isinstance(result, tuple):
                payload, status, extra_headers = result
                self._send(status, payload, extra_headers=extra_headers)
            elif result is not None:
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
        "clearance": u.get("clearance") or "internal",
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
             "role": r["role"], "team": r["team"], "department": r["department"],
             "clearance": r["clearance"]}
            for r in rows]


@Handler.route("POST", "/api/admin/members")
def add_member(h, ctx):
    if ctx.user.get("tenant_role") != "admin":
        raise ApiError(403, "仅租户管理员可管理成员")
    data = h._json()
    email = (data.get("email") or "").strip().lower()
    role = data.get("role", "member")
    clearance = data.get("clearance", "internal")
    if role not in ("admin", "member"):
        raise ApiError(400, "role 必须是 admin 或 member")
    if clearance not in permissions.CLASSIFICATIONS:
        raise ApiError(400, f"clearance 必须是 {permissions.CLASSIFICATIONS}")
    user = db_global.get_user_by_email(ctx.gconn, email)
    if user is None:
        raise ApiError(404, "用户不存在，请先让其注册")
    new_team = data.get("team") or None
    new_dept = data.get("department") or None
    is_new, before = db_global.upsert_tenant_member(
        ctx.gconn, ctx.user["tenant_id"], user["id"], role,
        new_team, new_dept, clearance,
    )
    after = {"user_id": user["id"], "email": email, "role": role,
             "team": new_team, "department": new_dept, "clearance": clearance}

    if is_new:
        _audit(ctx, action="member.add", object_type="member", object_id=user["id"],
               summary=f"成员 {user['display_name']}（{email}）加入租户，角色 {role}，密级 {clearance}",
               after=after)
        message = f"已将 {email} 加入租户"
    else:
        # 已在租户内：这是一次“更新”，必须留改前/改后；密级变化走与专门接口一致的动作
        changes = []
        if before["clearance"] != clearance:
            changes.append(f"密级 {before['clearance']}→{clearance}")
            _audit(ctx, action="member.clearance_update", object_type="member",
                   object_id=user["id"],
                   summary=f"经成员接口调整 {user['display_name']} 密级："
                           f"{before['clearance']} → {clearance}",
                   before={"clearance": before["clearance"]},
                   after={"clearance": clearance})
        if before["role"] != role:
            changes.append(f"角色 {before['role']}→{role}")
        if before["team"] != new_team:
            changes.append(f"团队 {before['team']}→{new_team}")
        if before["department"] != new_dept:
            changes.append(f"部门 {before['department']}→{new_dept}")
        change_desc = "，".join(changes) if changes else "无实质字段变化"
        _audit(ctx, action="member.update", object_type="member", object_id=user["id"],
               summary=f"更新成员 {user['display_name']}（{email}）：{change_desc}",
               before=before, after=after)
        message = f"已更新成员 {email}：{change_desc}"
    return {"message": message, "is_new": is_new, "role": role,
            "team": new_team, "department": new_dept, "clearance": clearance}


@Handler.route("PUT", "/api/admin/members/{user_id}/clearance")
def set_member_clearance(h, ctx, user_id):
    """调整成员密级许可（clearance）；下次请求令牌上下文即时生效。"""
    if ctx.user.get("tenant_role") != "admin":
        raise ApiError(403, "仅租户管理员可调整密级许可")
    target_id = _parse_id(user_id, "user_id")
    data = h._json()
    clearance = data.get("clearance")
    if clearance not in permissions.CLASSIFICATIONS:
        raise ApiError(400, f"clearance 必须是 {permissions.CLASSIFICATIONS}")
    before_row = db_global.get_membership(ctx.gconn, ctx.user["tenant_id"], target_id)
    if before_row is None:
        raise ApiError(404, "该用户不是当前租户成员")
    target = db_global.get_user(ctx.gconn, target_id)
    ok = db_global.update_member_clearance(ctx.gconn, ctx.user["tenant_id"], target_id, clearance)
    _audit(ctx, action="member.clearance_update", object_type="member", object_id=target_id,
           summary=f"调整成员 {target['display_name']} 密级：{before_row['clearance']} → {clearance}",
           before={"clearance": before_row["clearance"]},
           after={"clearance": clearance})
    if not ok:
        raise ApiError(404, "该用户不是当前租户成员")
    return {"message": f"密级许可已更新为 {clearance}", "clearance": clearance}


@Handler.route("DELETE", "/api/admin/members/{user_id}")
def remove_member(h, ctx, user_id):
    """把用户移出租户：立即删除成员关系并吊销其在本租户的全部会话。"""
    if ctx.user.get("tenant_role") != "admin":
        raise ApiError(403, "仅租户管理员可管理成员")
    target_id = _parse_id(user_id, "user_id")
    if target_id == ctx.user["user_id"]:
        raise ApiError(400, "不能移除当前登录的自己")
    before_row = db_global.get_membership(ctx.gconn, ctx.user["tenant_id"], target_id)
    if before_row is None:
        raise ApiError(404, "该用户不是当前租户成员")
    target = db_global.get_user(ctx.gconn, target_id)
    # 先落审计（与成员删除在同一逻辑变更中），再移除并吊销会话
    _audit(ctx, action="member.remove", object_type="member", object_id=target_id,
           summary=f"移出租户并吊销其全部会话：{target['display_name']}（{target['email']}）",
           before={"user_id": target_id, "email": target["email"],
                   "role": before_row["role"], "team": before_row["team"],
                   "department": before_row["department"],
                   "clearance": before_row["clearance"]})
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
    classification = fields.get("classification", "internal")
    try:
        result = ingest_upload(
            tconn=ctx.tconn,
            tenant_id=ctx.user["tenant_id"],
            tenant_slug=ctx.tenant_slug,
            owner_user_id=ctx.user["user_id"],
            filename=f["filename"],
            content=f["content"],
            title=fields.get("title") or None,
            visibility=visibility,
            owner_team=(fields.get("owner_team") or "").strip() or ctx.user.get("team"),
            owner_dept=(fields.get("owner_dept") or "").strip() or ctx.user.get("department"),
            classification=classification,
            actor=ctx.actor,
        )
    except ValueError as e:
        # 入库参数/解析类错误属于客户端问题，返回 400 而非 500
        raise ApiError(400, str(e))
    return result


@Handler.route("GET", "/api/documents")
def list_documents(h, ctx):
    where, params = permissions.accessible_document_where(ctx.identity)
    rows = ctx.tconn.execute(
        f"""SELECT d.id, d.title, d.source_name, d.file_type,
                   d.visibility, d.owner_user_id, d.owner_team, d.owner_dept,
                   d.created_at, d.updated_at
            FROM documents d WHERE {where} ORDER BY d.id DESC""",
        params,
    ).fetchall()
    stats = permissions.visible_document_stats(
        ctx.tconn, ctx.identity, [r["id"] for r in rows]
    )
    result = []
    for r in rows:
        item = dict(r)
        item.update(stats.get(r["id"], {"chunk_count": 0, "char_count": 0,
                                        "classification": None}))
        result.append(item)
    return result


def _audit(ctx, **kw):
    """向当前租户审计表追加一条记录（操作者/IP 自动带入）。"""
    a = ctx.actor
    return db_tenant.append_audit(
        ctx.tconn, tenant_id=ctx.user["tenant_id"],
        actor_id=a["user_id"], actor_name=a.get("display_name"),
        actor_email=a.get("email"), ip=a.get("ip"), **kw,
    )


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
    # 只返回当前用户可见片段的聚合信封，避免暴露整篇密级/隐藏片段数/全文字数
    stats = permissions.visible_document_stats(
        ctx.tconn, ctx.identity, [doc["id"]]
    ).get(doc["id"], {"chunk_count": 0, "char_count": 0, "classification": None})
    return {
        "id": doc["id"], "title": doc["title"], "source_name": doc["source_name"],
        "file_type": doc["file_type"], "visibility": doc["visibility"],
        "owner_user_id": doc["owner_user_id"], "owner_team": doc["owner_team"],
        "owner_dept": doc["owner_dept"], "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
        "char_count": stats["char_count"],
        "chunk_count": stats["chunk_count"],
        "classification": stats["classification"],
    }


@Handler.route("DELETE", "/api/documents/{doc_id}")
def delete_document(h, ctx, doc_id):
    doc = _load_managed_doc(ctx, _parse_id(doc_id, "document_id"))
    before = {"title": doc["title"], "source_name": doc["source_name"],
              "visibility": doc["visibility"], "classification": doc["classification"],
              "char_count": doc["char_count"], "owner_user_id": doc["owner_user_id"]}
    # 审计须在删除前写入同一事务
    _audit(ctx, action="document.delete", object_type="document",
           object_id=doc["id"], document_id=doc["id"],
           summary=f"删除文档《{doc['title']}》（{doc['source_name']}）", before=before)
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
            "classification": c["classification"] or f"inherit({doc['classification']})",
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
    """溯源：返回片段在原文中的精确定位与上下文。

    安全要点：before/after 不是简单地按 char_start±120 截取全文，而是先用
    当前用户在本文档内「可访问片段」的字符边界裁切——上下文窗口不得滑入相邻
    无权片段（混密级 / 邻接 deny / 过期授权），否则会通过原文窗口泄权。
    """
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

    # 本文档内全部可访问片段的字符区间，按文档顺序排列
    allowed_ids = permissions.accessible_chunk_ids(ctx.tconn, ctx.identity, doc_id)
    neighbors = ctx.tconn.execute(
        """SELECT id, char_start, char_end, heading_path FROM chunks
           WHERE document_id=? ORDER BY char_start""",
        (doc_id,),
    ).fetchall()

    full = row["full_text"]
    s, e = row["char_start"], row["char_end"]
    win_start, win_end = max(0, s - 120), min(len(full), e + 120)

    block_left = block_right = None  # 造成裁切的最近无权片段（可能连带其标题）
    for nb in neighbors:
        if nb["id"] in allowed_ids:
            continue
        ns, ne = nb["char_start"], nb["char_end"]
        # 左侧：无权片段与候选窗口有重叠（其结尾落在窗口起点之后、本片段之前）
        if ne <= s and ne > win_start:
            win_start = max(win_start, ne)
            block_left = nb if block_left is None or ne > block_left["char_end"] else block_left
        # 右侧：无权片段与候选窗口有重叠（其起点落在本片段之后、窗口终点之前）
        if ns >= e and ns < win_end:
            win_end = min(win_end, ns)
            block_right = nb if block_right is None or ns < block_right["char_start"] else block_right

    win_start = min(win_start, s)
    win_end = max(win_end, e)
    before = full[win_start:s]
    after = full[e:win_end]

    def _heading_tail(heading_path):
        return heading_path.split("/")[-1].strip() if heading_path else ""

    # 左侧阻断：其标题位于正文之前，已落在 win_start 之外；仅去掉边界残余空白
    if block_left is not None:
        before = before.rstrip("\n 　")
    # 右侧阻断：无权片段的标题行位于其正文之前、仍可能夹在 [e, win_end) 内，需剥除
    if block_right is not None:
        title = _heading_tail(block_right["heading_path"])
        lines = after.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        if title and lines and lines[0].strip() == title:
            lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
        after = "\n".join(lines)

    clipped = block_left is not None or block_right is not None
    return {
        "title": row["title"], "source_name": row["source_name"],
        "heading_path": row["heading_path"],
        "page_start": row["page_start"], "page_end": row["page_end"],
        "char_range": [s, e],
        "before": before,
        "exact_text": full[s:e],
        "after": after,
        "context_clipped": clipped,
        "clipped_side": ("left" if block_left is not None else "")
                        + (",right" if block_right is not None else ""),
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
    before = {"visibility": row["visibility"], "heading_path": row["heading_path"]}
    db_tenant.set_chunk_visibility(ctx.tconn, row["id"], vis)
    _audit(ctx, action="chunk.visibility_update", object_type="chunk",
           object_id=row["id"], document_id=row["document_id"],
           summary=f"修改片段#{row['chunk_index']+1}可见性：{row['visibility'] or '继承'} → {vis or '继承'}",
           before=before, after={"visibility": vis})
    db_tenant.bump_document_version(ctx.tconn, row["document_id"])
    return {"chunk_id": row["id"], "visibility": vis,
            "message": "片段可见性已更新" + ("（继承文档）" if vis is None else "")}


@Handler.route("PUT", "/api/chunks/{chunk_id}/classification")
def set_chunk_classification(h, ctx, chunk_id):
    """设置片段密级（internal/sensitive/secret）或 null 继承文档密级。"""
    row = _load_managed_chunk(ctx, chunk_id)
    data = h._json()
    cls = data.get("classification")
    if cls is not None and cls not in permissions.CLASSIFICATIONS:
        raise ApiError(400, f"classification 必须是 {permissions.CLASSIFICATIONS} 或 null(继承)")
    before = {"classification": row["classification"], "heading_path": row["heading_path"]}
    db_tenant.set_chunk_classification(ctx.tconn, row["id"], cls)
    _audit(ctx, action="chunk.classification_update", object_type="chunk",
           object_id=row["id"], document_id=row["document_id"],
           summary=f"修改片段#{row['chunk_index']+1}密级：{row['classification'] or '继承'} → {cls or '继承'}",
           before=before, after={"classification": cls})
    db_tenant.bump_document_version(ctx.tconn, row["document_id"])
    return {"chunk_id": row["id"], "classification": cls,
            "message": "片段密级已更新" + ("（继承文档）" if cls is None else f"（{cls}）")}


def _resolve_expires(data: dict):
    """支持 expires_in（相对秒数）或 expires_at（绝对 unix 秒）；二者皆无则长期有效。"""
    import time as _time
    if data.get("expires_in") is not None:
        try:
            seconds = float(data["expires_in"])
        except (TypeError, ValueError):
            raise ApiError(400, "expires_in 必须是秒数")
        if seconds <= 0:
            raise ApiError(400, "expires_in 必须为正数")
        return _time.time() + seconds
    if data.get("expires_at") is not None:
        try:
            return float(data["expires_at"])
        except (TypeError, ValueError):
            raise ApiError(400, "expires_at 必须是 unix 秒时间戳")
    return None


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
    effect = data.get("effect", "allow")
    if effect not in permissions.VALID_EFFECTS:
        raise ApiError(400, f"effect 必须是 {permissions.VALID_EFFECTS}")
    expires_at = _resolve_expires(data)
    gid = db_tenant.add_grant(ctx.tconn, row["id"], stype, svalue, effect, expires_at)
    word = "拒绝规则" if effect == "deny" else "授权"
    ttl = f"，到期 {expires_at:.0f}" if expires_at else "，长期有效"
    _audit(ctx, action="grant.add", object_type="grant", object_id=gid,
           document_id=row["document_id"],
           summary=f"为片段#{row['chunk_index']+1} 添加{word}：{stype}={svalue}{ttl}",
           after={"grant_id": gid, "chunk_id": row["id"], "subject_type": stype,
                  "subject_value": svalue, "effect": effect, "expires_at": expires_at})
    db_tenant.bump_document_version(ctx.tconn, row["document_id"])
    return {"grant_id": gid, "chunk_id": row["id"],
            "subject_type": stype, "subject_value": svalue,
            "effect": effect, "expires_at": expires_at}


@Handler.route("DELETE", "/api/chunks/{chunk_id}/grants/{grant_id}")
def remove_chunk_grant(h, ctx, chunk_id, grant_id):
    row = _load_managed_chunk(ctx, chunk_id)
    gid = _parse_id(grant_id, "grant_id")
    existing = ctx.tconn.execute(
        "SELECT * FROM chunk_grants WHERE id=? AND chunk_id=?", (gid, row["id"])
    ).fetchone()
    ok = db_tenant.delete_grant(ctx.tconn, gid, row["id"])
    if not ok:
        raise ApiError(404, "授权记录不存在")
    _audit(ctx, action="grant.delete", object_type="grant", object_id=gid,
           document_id=row["document_id"],
           summary=f"移除片段#{row['chunk_index']+1}规则："
                   f"{existing['effect']}:{existing['subject_type']}={existing['subject_value']}",
           before={"subject_type": existing["subject_type"],
                   "subject_value": existing["subject_value"], "effect": existing["effect"],
                   "expires_at": existing["expires_at"]})
    db_tenant.bump_document_version(ctx.tconn, row["document_id"])
    return {"message": "规则已移除"}


# ============================== 文档级密级 / 规则 ==============================

@Handler.route("PUT", "/api/documents/{doc_id}/classification")
def set_document_classification(h, ctx, doc_id):
    doc = _load_managed_doc(ctx, _parse_id(doc_id, "document_id"))
    data = h._json()
    cls = data.get("classification")
    if cls not in permissions.CLASSIFICATIONS:
        raise ApiError(400, f"classification 必须是 {permissions.CLASSIFICATIONS}")
    before = {"classification": doc["classification"]}
    db_tenant.set_document_classification(ctx.tconn, doc["id"], cls)
    _audit(ctx, action="document.classification_update", object_type="document",
           object_id=doc["id"], document_id=doc["id"],
           summary=f"修改文档《{doc['title']}》密级：{doc['classification']} → {cls}",
           before=before, after={"classification": cls})
    db_tenant.bump_document_version(ctx.tconn, doc["id"])
    return {"document_id": doc["id"], "classification": cls}


@Handler.route("GET", "/api/documents/{doc_id}/rules")
def list_document_rules(h, ctx, doc_id):
    doc = _load_managed_doc(ctx, _parse_id(doc_id, "document_id"))
    return [dict(r) for r in db_tenant.list_document_rules(ctx.tconn, doc["id"])]


@Handler.route("POST", "/api/documents/{doc_id}/rules")
def add_document_rule(h, ctx, doc_id):
    """文档级规则：对整篇文档所有片段 allow/deny，可时限。"""
    doc = _load_managed_doc(ctx, _parse_id(doc_id, "document_id"))
    data = h._json()
    stype = data.get("subject_type")
    svalue = (data.get("subject_value") or "").strip()
    effect = data.get("effect", "deny")
    if stype not in permissions.VALID_SUBJECTS:
        raise ApiError(400, f"subject_type 必须是 {permissions.VALID_SUBJECTS}")
    if effect not in permissions.VALID_EFFECTS:
        raise ApiError(400, f"effect 必须是 {permissions.VALID_EFFECTS}")
    if not svalue:
        raise ApiError(400, "subject_value 必填")
    if stype == "user":
        if not svalue.isdigit():
            raise ApiError(400, "user 规则的 subject_value 必须是用户数字 ID")
        svalue = str(int(svalue))
    expires_at = _resolve_expires(data)
    rid = db_tenant.add_document_rule(ctx.tconn, doc["id"], stype, svalue, effect, expires_at)
    word = "拒绝" if effect == "deny" else "授权"
    ttl = f"，到期 {expires_at:.0f}" if expires_at else "，长期有效"
    _audit(ctx, action="document_rule.add", object_type="document_rule", object_id=rid,
           document_id=doc["id"],
           summary=f"对文档《{doc['title']}》添加{word}规则：{stype}={svalue}{ttl}",
           after={"rule_id": rid, "subject_type": stype, "subject_value": svalue,
                  "effect": effect, "expires_at": expires_at})
    db_tenant.bump_document_version(ctx.tconn, doc["id"])
    return {"rule_id": rid, "document_id": doc["id"],
            "subject_type": stype, "subject_value": svalue,
            "effect": effect, "expires_at": expires_at}


@Handler.route("DELETE", "/api/documents/{doc_id}/rules/{rule_id}")
def remove_document_rule(h, ctx, doc_id, rule_id):
    doc = _load_managed_doc(ctx, _parse_id(doc_id, "document_id"))
    rid = _parse_id(rule_id, "rule_id")
    existing = ctx.tconn.execute(
        "SELECT * FROM document_rules WHERE id=? AND document_id=?", (rid, doc["id"])
    ).fetchone()
    ok = db_tenant.delete_document_rule(ctx.tconn, rid, doc["id"])
    if not ok:
        raise ApiError(404, "规则不存在")
    _audit(ctx, action="document_rule.delete", object_type="document_rule", object_id=rid,
           document_id=doc["id"],
           summary=f"移除文档《{doc['title']}》规则："
                   f"{existing['effect']}:{existing['subject_type']}={existing['subject_value']}",
           before={"subject_type": existing["subject_type"],
                   "subject_value": existing["subject_value"], "effect": existing["effect"],
                   "expires_at": existing["expires_at"]})
    db_tenant.bump_document_version(ctx.tconn, doc["id"])
    return {"message": "文档规则已移除"}


# ============================== 检索 / 问答 ==============================

def _require_tenant_admin(ctx):
    if ctx.user.get("tenant_role") != "admin":
        raise ApiError(403, "仅租户管理员可查看审计")


def _audit_filters(query: dict):
    import time as _time
    filters = {}
    try:
        filters["start"] = float(query["start"]) if query.get("start") else None
        filters["end"] = float(query["end"]) if query.get("end") else None
        filters["actor_id"] = int(query["actor_id"]) if query.get("actor_id") else None
        filters["document_id"] = int(query["document_id"]) if query.get("document_id") else None
        filters["limit"] = min(int(query.get("limit") or 200), 2000)
        filters["offset"] = max(int(query.get("offset") or 0), 0)
    except (TypeError, ValueError):
        raise ApiError(400, "审计过滤参数非法")
    filters["action"] = (query.get("action") or "").strip() or None
    return filters


@Handler.route("GET", "/api/audit")
def get_audit(h, ctx):
    """查询当前租户审计（按时间倒序）。普通成员 403；查询强制 tenant_id 闸门。"""
    _require_tenant_admin(ctx)
    from urllib.parse import parse_qs
    q = {k: v[0] for k, v in parse_qs(urlparse(h.path).query).items()}
    f = _audit_filters(q)
    rows = db_tenant.query_audit(
        ctx.tconn, ctx.user["tenant_id"], start=f["start"], end=f["end"],
        actor_id=f["actor_id"], document_id=f["document_id"], action=f["action"],
        limit=f["limit"], offset=f["offset"],
    )
    return {"count": len(rows), "results": [_audit_row(r) for r in rows]}


@Handler.route("GET", "/api/audit/export")
def export_audit(h, ctx):
    """导出当前租户审计为 CSV（同样仅管理员、同样租户闸门）。"""
    import csv
    import io
    _require_tenant_admin(ctx)
    from urllib.parse import parse_qs
    q = {k: v[0] for k, v in parse_qs(urlparse(h.path).query).items()}
    f = _audit_filters(q)
    # 导出上限更高，但仍封顶，防止一次拉取全库
    f["limit"] = min(f.get("limit", 2000) if f.get("limit") else 2000, 10000)
    rows = db_tenant.query_audit(
        ctx.tconn, ctx.user["tenant_id"], start=f["start"], end=f["end"],
        actor_id=f["actor_id"], document_id=f["document_id"], action=f["action"],
        limit=f["limit"], offset=f["offset"],
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "created_at", "actor_id", "actor_name", "actor_email",
                     "action", "object_type", "object_id", "document_id",
                     "summary", "before_json", "after_json", "ip"])
    for r in rows:
        writer.writerow([r["id"], r["created_at"], r["actor_id"], r["actor_name"],
                         r["actor_email"], r["action"], r["object_type"], r["object_id"],
                         r["document_id"], r["summary"], r["before_json"],
                         r["after_json"], r["ip"]])
    csv_bytes = ("﻿" + buf.getvalue()).encode("utf-8")
    import time as _time
    fname = f"audit_{ctx.tenant_slug}_{int(_time.time())}.csv"
    return (csv_bytes, 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{fname}"',
    })


def _audit_row(r):
    import json as _json
    def _load(s):
        if not s:
            return None
        try:
            return _json.loads(s)
        except ValueError:
            return s
    return {
        "id": r["id"], "created_at": r["created_at"], "actor_id": r["actor_id"],
        "actor_name": r["actor_name"], "actor_email": r["actor_email"],
        "action": r["action"], "object_type": r["object_type"],
        "object_id": r["object_id"], "document_id": r["document_id"],
        "summary": r["summary"], "before": _load(r["before_json"]),
        "after": _load(r["after_json"]), "ip": r["ip"],
    }


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
