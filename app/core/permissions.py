"""细粒度三维权限模型（可见性 ACL + 时限 + deny + 密级 clearance）。

可见性 visibility：private / team / department / public
密级 classification：internal(0) < sensitive(1) < secret(2)
成员许可 clearance：租户内成员的最高可读密级，存于全局库 tenant_users。

片段（chunk）规则：
  - visibility/classification 为 NULL 时继承文档；
  - chunk_grants 对某主体可挂 allow（可带 expires_at）或 deny；
  - document_rules 对整篇文档的所有片段挂 allow/deny（用于对某人/某团队整体拒绝）。

有效访问判定（普通成员）：
  可访问 = 基础可见性(或有效 allow 授权)
            ∧ 用户 clearance ≥ 片段有效密级
            ∧ 不存在命中的 deny（片段级或文档级，且 deny 未过期）
  deny 优先于一切 allow 与 public/team 等可见性覆盖。

租户管理员 (tenant_users.role='admin')：
  旁路密级/deny/时限，对本租户全部内容可读可管（用于审计与运维）；
  但跨租户一律不可见——租户独立库文件本身是硬边界，谓词始终绑定 tenant_id。

本模块用有序 SQL 构建器（_B）拼接，参数随 SQL 片段按出现顺序压入，
避免命名/位置参数混用及参数错序。
"""
from __future__ import annotations

import time as _time

_EFF_VIS = "COALESCE(c.visibility, d.visibility)"
_EFF_CLS = "COALESCE(c.classification, d.classification)"

CLASSIFICATIONS = ("internal", "sensitive", "secret")
CLEARANCE_LEVELS = {"internal": 0, "sensitive": 1, "secret": 2}
DEFAULT_CLEARANCE = "internal"

VALID_VIS = ("private", "team", "department", "public")
VALID_SUBJECTS = ("user", "role", "team", "department")
VALID_EFFECTS = ("allow", "deny")


def clearance_rank(name) -> int:
    return CLEARANCE_LEVELS.get(name, 0)


class _B:
    """有序 SQL 片段构建器：params 与 SQL 中 ? 的出现顺序严格一致。"""

    def __init__(self):
        self.parts: list[str] = []
        self.params: list = []

    def add(self, sql: str, *params):
        self.parts.append(sql)
        self.params.extend(params)
        return self

    def live(self, alias: str = "r"):
        """规则未过期（expires_at 为 NULL 表示长期）。"""
        self.add(f"({alias}.expires_at IS NULL OR {alias}.expires_at > ?)", self._now)
        return self

    def subject(self, ctx: dict, alias: str = "r"):
        """规则行命中当前主体（用户/角色/团队/部门之一）。"""
        self.add(
            f"("
            f"({alias}.subject_type='user'       AND {alias}.subject_value = ?)"
            f" OR ({alias}.subject_type='role'       AND {alias}.subject_value = ?)"
            f" OR ({alias}.subject_type='team'       AND {alias}.subject_value = ?)"
            f" OR ({alias}.subject_type='department' AND {alias}.subject_value = ?)"
            f")",
            str(ctx["user_id"]), ctx.get("tenant_role") or "member",
            ctx.get("team"), ctx.get("department"),
        )
        return self

    def sql(self, joiner: str = " AND ") -> str:
        return joiner.join(self.parts)


def _classification_case(column: str) -> str:
    return (f"(CASE {column} WHEN 'secret' THEN 2 WHEN 'sensitive' THEN 1"
            f" ELSE 0 END)")


def _rule_exists(ctx: dict, b: _B, table: str, link: str, effect: str, now: float):
    """拼接一个 chunk_grants / document_rules 的 EXISTS 子查询。"""
    b._now = now
    b.add(f"EXISTS (SELECT 1 FROM {table} r WHERE {link} AND r.effect=? AND", effect)
    # 上一行结尾是 AND，后面紧跟 live 与 subject，再闭合括号
    # 用嵌套构建器组装括号内条件
    inner = _B()
    inner._now = now
    inner.live("r")
    inner.add("AND")
    inner.subject(ctx, "r")
    b.parts[-1] = b.parts[-1] + " " + inner.sql(" ") + ")"
    b.params.extend(inner.params)
    return b


def _chunk_access_inner(ctx: dict, now: float) -> tuple[str, list]:
    """普通成员对单个片段（别名 c/d）的可访问谓词。"""
    b = _B()

    # 1) 不存在命中的 deny（片段级 / 文档级）
    deny = _B()
    _rule_exists(ctx, deny, "chunk_grants", "r.chunk_id = c.id", "deny", now)
    _rule_exists(ctx, deny, "document_rules", "r.document_id = d.id", "deny", now)
    b.add(f"NOT ({deny.sql(' OR ')})", *deny.params)

    # 2) 密级闸门
    b.add(f"({_classification_case(_EFF_CLS)} <= ?)", clearance_rank(ctx.get("clearance")))

    # 3) 基础可见性 或 有效 allow（片段级 / 文档级）
    vis = _B()
    vis.add(f"{_EFF_VIS} = 'public'")
    vis.add(f"({_EFF_VIS} = 'private' AND d.owner_user_id = ?)", ctx["user_id"])
    vis.add(f"({_EFF_VIS} = 'team' AND d.owner_team IS NOT NULL AND d.owner_team = ?)",
            ctx.get("team"))
    vis.add(f"({_EFF_VIS} = 'department' AND d.owner_dept IS NOT NULL AND d.owner_dept = ?)",
            ctx.get("department"))

    allow_chunk = _B()
    _rule_exists(ctx, allow_chunk, "chunk_grants", "r.chunk_id = c.id", "allow", now)
    allow_doc = _B()
    _rule_exists(ctx, allow_doc, "document_rules", "r.document_id = d.id", "allow", now)

    b.add(f"({vis.sql(' OR ')} OR {allow_chunk.sql(' ')} OR {allow_doc.sql(' ')})",
          *(vis.params + allow_chunk.params + allow_doc.params))
    return b.sql(" AND "), b.params


def accessible_chunks_where(ctx: dict, now: float | None = None) -> tuple[str, list]:
    """「用户可访问片段」完整谓词（检索/问答/溯源共用）。"""
    now = now if now is not None else _time.time()
    is_admin = 1 if (ctx.get("tenant_role") or "") == "admin" else 0
    inner_sql, inner_params = _chunk_access_inner(ctx, now)
    sql = f"d.tenant_id = ? AND (? = 1 OR ({inner_sql}))"
    params = [ctx["tenant_id"], is_admin] + inner_params
    return sql, params


def accessible_document_where(ctx: dict, now: float | None = None) -> tuple[str, list]:
    """文档列表/详情可见性，与检索、问答、片段列表严格一致。

    规则：租户管理员可见全部；否则文档可见 **当且仅当至少存在一个该用户
    可访问的片段**。片段谓词已内含：
      - 文档级 visibility 的继承（COALESCE(c.visibility, d.visibility)）；
      - 文档密级闸门（COALESCE(c.classification, d.classification)）；
      - 文档级 / 片段级 deny（deny 优先）；
      - 文档级 / 片段级 allow 与有效期。

    因此当一个 public/team 文档的全部片段都被 deny（或密级全部不足）时，
    它不会以“空壳文档”形式出现在列表/详情——能进列表就一定能看到至少一个片段。
    """
    now = now if now is not None else _time.time()
    is_admin = 1 if (ctx.get("tenant_role") or "") == "admin" else 0

    inner_sql, inner_params = _chunk_access_inner(ctx, now)
    sql = f"""
        d.tenant_id = ?
        AND (
            ? = 1
            OR EXISTS (
                SELECT 1 FROM chunks c WHERE c.document_id = d.id AND {inner_sql}
            )
        )
    """
    params = [ctx["tenant_id"], is_admin] + inner_params
    return sql, params


def accessible_chunk_ids(conn, ctx: dict, document_id: int,
                         now: float | None = None) -> set[int]:
    """返回某文档内当前用户可访问的全部片段 id（用于溯源窗口裁切等）。"""
    where, params = accessible_chunks_where(ctx, now)
    rows = conn.execute(
        f"""SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id
            WHERE c.document_id=? AND {where}""",
        [document_id] + params,
    ).fetchall()
    return {r[0] for r in rows}


# ---------------- 管理权限（仅限本租户管理员/文档所有者，不随密级/deny 变化） ----------------

def user_can_manage(ctx: dict) -> bool:
    return (ctx.get("tenant_role") or "") == "admin"


def can_manage_document(ctx: dict, doc) -> bool:
    return user_can_manage(ctx) or doc["owner_user_id"] == ctx["user_id"]


def can_read_document(conn, ctx: dict, doc) -> bool:
    """是否可读某文档（文档级或任一可访问片段）。与列表谓词语义一致。"""
    if doc is None or doc["tenant_id"] != ctx["tenant_id"]:
        return False
    where, params = accessible_document_where(ctx)
    row = conn.execute(
        f"SELECT 1 AS ok FROM documents d WHERE d.id=? AND {where}",
        [doc["id"]] + params,
    ).fetchone()
    return row is not None


def can_manage_chunk(ctx: dict, conn, chunk_row) -> bool:
    """修改片段权限需要：租户管理员，或该片段所属文档的所有者。"""
    if user_can_manage(ctx):
        return True
    doc = conn.execute(
        "SELECT owner_user_id FROM documents WHERE id=?", (chunk_row["document_id"],)
    ).fetchone()
    return doc is not None and doc["owner_user_id"] == ctx["user_id"]
