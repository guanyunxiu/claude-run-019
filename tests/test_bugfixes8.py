#!/usr/bin/env python3
"""第八轮缺陷回归：
  Bug1 审计导出使用独立高上限：默认/上限 10000，不受普通查询 200/2000 限制；
  Bug2 seed 与建租户写路径补审计：文档上传/片段授权/密级可见性覆盖/成员加入、
        建租户指定 admin_email 都要写入对应租户 audit_log。

前置：python3 -m scripts.seed 并启动服务。
"""
import csv
import io
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("KB_BASE", "http://127.0.0.1:8080")
PASS = "test123"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_passed = _failed = 0


def call(method, path, token=None, data=None):
    headers = {}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ct = r.headers.get("Content-Type", "")
            raw = r.read()
            if "csv" in ct:
                return r.status, raw.decode("utf-8-sig")
            return r.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, b


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def login(email, password=PASS, slug="biolab"):
    st, r = call("POST", "/api/auth/login",
                 data={"email": email, "password": password, "tenant_slug": slug})
    assert st == 200, (email, st, r)
    return r


def tenant_db(slug):
    return os.path.join(ROOT, "data", "tenants", f"{slug}.db")


def main():
    suf = uuid.uuid4().hex[:8]
    alice = login("alice@lab.cn", "demo123")
    at = alice["token"]

    # ---------- Bug 1：导出高上限 ----------
    print("\n[Bug1] 审计导出真正按更高上限工作")
    g = sqlite3.connect(tenant_db("biolab"))
    n0 = g.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    BULK = 250
    for i in range(BULK):
        g.execute(
            "INSERT INTO audit_log(created_at,actor_id,actor_name,actor_email,action,"
            "object_type,object_id,document_id,summary,before_json,after_json,ip,tenant_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), alice["user"]["id"], "压测", "x", "grant.add", "grant",
             str(100000 + i), None, f"批量造数 {i}", None, None, None, 1))
    g.commit()
    n1 = g.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    g.close()
    check(f"已造数 {BULK} 条（{n0}->{n1}）", n1 - n0 == BULK)

    st, q_default = call("GET", "/api/audit", token=at)
    check("普通查询默认只回 200", st == 200 and q_default["count"] == 200
          and q_default.get("limit") == 200, str(q_default.get("count")))
    st, q_big = call("GET", "/api/audit?limit=10000", token=at)
    # 总量 261 < 封顶 2000：应返回全部 261（关键是 > 默认 200，证明不再被 200 卡死）
    check("普通查询显式大 limit 可超过默认 200（封顶 2000）",
          st == 200 and 200 < q_big["count"] <= 2000 and q_big["count"] == n1,
          str(q_big.get("count")))

    # 在总量 261（>200、<2000）阶段，导出默认与显式 limit 都应返回全部
    st, csv_early = call("GET", "/api/audit/export", token=at)
    n_early = len(list(csv.reader(io.StringIO(csv_early)))) - 1
    check("导出默认包含全部审计（远超 200）", st == 200 and n_early == n1,
          f"{n_early} vs {n1}")
    st, csv_early2 = call("GET", "/api/audit/export?limit=10000", token=at)
    n_early2 = len(list(csv.reader(io.StringIO(csv_early2)))) - 1
    check("导出 limit=10000 返回全部", n_early2 == n1, str(n_early2))

    # 再造到 >2000 行，验证普通查询封顶 2000、导出默认/上限 10000
    g = sqlite3.connect(tenant_db("biolab"))
    MORE = 2000
    for i in range(MORE):
        g.execute(
            "INSERT INTO audit_log(created_at,actor_id,action,object_type,object_id,"
            "summary,tenant_id) VALUES (?,?,?,?,?,?,?)",
            (time.time(), alice["user"]["id"], "grant.add", "grant", str(200000 + i),
             f"封顶造数 {i}", 1))
    g.commit()
    n2 = g.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    g.close()
    check(f"再造 {MORE} 行后总量 {n2} > 2000", n2 > 2000)
    st, q_cap = call("GET", "/api/audit?limit=100000", token=at)
    check("普通查询封顶 2000", q_cap["count"] == 2000, str(q_cap.get("count")))
    st, csv_all = call("GET", "/api/audit/export", token=at)
    n_exp = len(list(csv.reader(io.StringIO(csv_all)))) - 1
    check("导出默认返回全部（>2000，达 10000 上限内）",
          n_exp == n2, f"{n_exp} vs {n2}")

    # 普通成员仍 403
    bob = login("bob@lab.cn", "demo123")
    st, _ = call("GET", "/api/audit/export", token=bob["token"])
    check("普通成员导出仍 403", st == 403, f"{st}")

    # ---------- Bug 2：seed 审计齐全（在全新 seed 数据上不易隔离，直接查当前 biolab） ----------
    print("\n[Bug2] seed 写入路径的审计一致性")
    g = sqlite3.connect(tenant_db("biolab"))
    g.row_factory = sqlite3.Row
    def actions(pred=""):
        return {r[0]: r[1] for r in g.execute(
            f"SELECT action, COUNT(*) FROM audit_log {pred} GROUP BY action")}
    act = actions()
    # seed 固定产物（可能叠加测试产生的同动作，故断言 >=）
    check("seed 文档上传审计 >=4", act.get("document.upload", 0) >= 4, str(act))
    check("seed 成员加入审计 >=4（alice/bob/carol/dave）", act.get("member.add", 0) >= 4, str(act))
    check("seed 给 Bob 的片段授权 grant.add >=1", act.get("grant.add", 0) >= 1, str(act))
    check("seed 机密片段可见性覆盖 chunk.visibility_update >=1",
          act.get("chunk.visibility_update", 0) >= 1, str(act))
    check("seed 机密片段密级 chunk.classification_update >=1",
          act.get("chunk.classification_update", 0) >= 1, str(act))
    # grant.add 里应能找到 subject_value=Bob id 的初始化记录
    bob_grant = g.execute(
        "SELECT COUNT(*) n FROM audit_log WHERE action='grant.add' "
        "AND summary LIKE '%初始化授权%' AND json_extract(after_json,'$.subject_value')=?",
        (str(bob["user"]["id"]),)).fetchone()["n"]
    check("存在 seed 对 Bob 的初始化授权记录", bob_grant >= 1, str(bob_grant))
    # chemmat 应有 erin 的 member.add 与文档上传
    gc = sqlite3.connect(tenant_db("chemmat")); gc.row_factory = sqlite3.Row
    cact = {r[0]: r[1] for r in gc.execute("SELECT action,COUNT(*) FROM audit_log GROUP BY action")}
    check("chemmat 有 erin member.add", cact.get("member.add", 0) >= 1, str(cact))
    check("chemmat 有文档上传审计", cact.get("document.upload", 0) >= 1, str(cact))
    g.close(); gc.close()

    # 审计只追加护栏仍在（直接连库）
    g = sqlite3.connect(tenant_db("biolab"))
    aid = g.execute("SELECT id FROM audit_log LIMIT 1").fetchone()[0]
    blocked_u = blocked_d = False
    try:
        g.execute("UPDATE audit_log SET summary='x' WHERE id=?", (aid,))
    except sqlite3.Error:
        blocked_u = True
    try:
        g.execute("DELETE FROM audit_log WHERE id=?", (aid,))
    except sqlite3.Error:
        blocked_d = True
    check("审计 UPDATE 被触发器拒绝", blocked_u)
    check("审计 DELETE 被触发器拒绝", blocked_d)
    g.close()

    # ---------- Bug 2：建租户指定 admin_email 落 member.add ----------
    print("\n[Bug2] 平台管理员建租户时 admin 加入有审计")
    root_email = f"root_{suf}@corp.cn"
    ta_email = f"ta_{suf}@corp.cn"
    call("POST", "/api/auth/register",
         data={"email": root_email, "display_name": "平台管理员", "password": PASS})
    call("POST", "/api/auth/register",
         data={"email": ta_email, "display_name": "新管理员", "password": PASS})
    gp = sqlite3.connect(os.path.join(ROOT, "data", "global.db"))
    gp.execute("UPDATE users SET is_platform_admin=1 WHERE email=?", (root_email,))
    gp.commit(); gp.close()
    # 平台管理员需先能登录：由 alice 将其加入 biolab
    call("POST", "/api/admin/members", token=at,
         data={"email": root_email, "role": "member"})
    root = login(root_email)
    new_slug = f"newt{suf}"
    st, r = call("POST", "/api/admin/tenants", token=root["token"],
                 data={"slug": new_slug, "name": "新租户", "admin_email": ta_email})
    check("创建租户成功", st == 200 and r["slug"] == new_slug, f"{st} {r}")
    nt = sqlite3.connect(tenant_db(new_slug)); nt.row_factory = sqlite3.Row
    rows = nt.execute("SELECT action, summary, before_json, after_json FROM audit_log").fetchall()
    check("新租户有且有 member.add", len(rows) >= 1 and rows[0]["action"] == "member.add",
          str([dict(r) for r in rows]))
    check("审计摘要含管理员邮箱", ta_email in rows[0]["summary"], rows[0]["summary"])
    check("after 记录角色 admin",
          json.loads(rows[0]["after_json"])["role"] == "admin")
    nt.close()

    print(f"\n结果：通过 {_passed}，失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
