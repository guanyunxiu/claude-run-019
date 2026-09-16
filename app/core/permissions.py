"""细粒度权限模型（需求 3 / 4 的核心）

三级（四级）可见性：
  private    仅文档所有者可见
  team       文档归属团队成员可见
  department 文档归属部门成员可见
  public     租户内全员公开（"部门公开"之上的租户公开级）

片段（chunk）可见性：
  - chunks.visibility 为 NULL 时继承文档 visibility；
  - 可单独覆盖为 private/team/department/public；
  - 额外授权 chunk_grants 可向 指定用户/角色/团队/部门 放行单个片段。

租户管理员 (membership.role='admin') 对本租户全部内容可见（便于审计与运维），
但跨租户一律不可见——租户库本身就是硬边界。
"""
from __future__ import annotations

# 有效可见性取片段覆盖值，否则继承文档
_EFF_VIS = "COALESCE(c.visibility, d.visibility)"

VALID_VIS = ("private", "team", "department", "public")
VALID_SUBJECTS = ("user", "role", "team", "department")


def _chunk_access_predicate(uid, role, team, dept, uid_s) -> tuple[str, list]:
    """单个片段对当前用户可访问的内部条件（别名固定为 c / d）。

    覆盖：片段有效可见性（COALESCE(c.visibility, d.visibility)）的四级判定，
    以及 chunk_grants 的 用户/角色/团队/部门 四类附加授权。
    """
    pred = f"""(
            ? = 'admin'
            OR {_EFF_VIS} = 'public'
            OR ({_EFF_VIS} = 'private'    AND d.owner_user_id = ?)
            OR ({_EFF_VIS} = 'team'       AND d.owner_team IS NOT NULL AND d.owner_team = ?)
            OR ({_EFF_VIS} = 'department' AND d.owner_dept IS NOT NULL AND d.owner_dept = ?)
            OR EXISTS (
                SELECT 1 FROM chunk_grants g
                WHERE g.chunk_id = c.id AND (
                       (g.subject_type='user'       AND g.subject_value = ?)
                    OR (g.subject_type='role'       AND g.subject_value = ?)
                    OR (g.subject_type='team'       AND g.subject_value = ?)
                    OR (g.subject_type='department' AND g.subject_value = ?)
                )
            )
        )"""
    params = [role, uid, team, dept, uid_s, role, team, dept]
    return pred, params


def accessible_chunks_where(ctx: dict) -> tuple[str, list]:
    """生成「用户可访问片段」的 SQL 片段与参数（权限穿透式检索的核心过滤谓词）。

    ctx 由请求令牌解析得到：user_id / tenant_role / team / department。
    该谓词同时绑定 d.tenant_id，任何情况下都不会跨出租户库。
    """
    uid = ctx["user_id"]
    role = ctx.get("tenant_role") or "member"
    team = ctx.get("team")
    dept = ctx.get("department")
    uid_s = str(uid)

    pred, pred_params = _chunk_access_predicate(uid, role, team, dept, uid_s)
    where = f"d.tenant_id = ? AND {pred}"
    return where, [ctx["tenant_id"]] + pred_params


def accessible_document_where(ctx: dict) -> tuple[str, list]:
    """文档列表可见性。

    文档可见，当且仅当满足下列任一：
      - 文档级可见性（public/private 所有者/team/department）直接放行；
      - 存在对该用户附加授权的片段（chunk_grants）；
      - 存在「片段级可见性覆盖后」对该用户可访问的片段（如 private 文档中
        某片段被覆盖为 public/team/department）。
    """
    uid = ctx["user_id"]
    role = ctx.get("tenant_role") or "member"
    team = ctx.get("team")
    dept = ctx.get("department")
    uid_s = str(uid)

    chunk_pred, chunk_params = _chunk_access_predicate(uid, role, team, dept, uid_s)
    where = f"""
        d.tenant_id = ?
        AND (
            ? = 'admin'
            OR d.visibility = 'public'
            OR (d.visibility = 'private'    AND d.owner_user_id = ?)
            OR (d.visibility = 'team'       AND d.owner_team IS NOT NULL AND d.owner_team = ?)
            OR (d.visibility = 'department' AND d.owner_dept IS NOT NULL AND d.owner_dept = ?)
            OR EXISTS (
                SELECT 1 FROM chunks c WHERE c.document_id = d.id AND {chunk_pred}
            )
        )
    """
    params = [ctx["tenant_id"], role, uid, team, dept] + chunk_params
    return where, params


def user_can_manage(ctx: dict) -> bool:
    """租户管理员可管理本租户所有文档/权限。"""
    return (ctx.get("tenant_role") or "") == "admin"


def can_manage_document(ctx: dict, doc) -> bool:
    return user_can_manage(ctx) or doc["owner_user_id"] == ctx["user_id"]


def can_read_document(conn, ctx: dict, doc) -> bool:
    """是否可读某文档（含其至少一个被授权片段）。

    与管理权不同：同团队/同部门/被片段授权的成员可读文档与可见片段，
    但不能修改权限或删除。
    """
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
