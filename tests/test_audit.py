#!/usr/bin/env python3
"""第六轮：权限变更审计回归。

覆盖：
  - 改密级/加 deny/踢人/删文档/成员密级等写操作都留痕，含谁/何时/对象/改前改后；
  - 普通成员查审计 403；
  - 化学租户看不到生物实验室审计（跨租户隔离）；
  - 审计表只追加（触发器拒 UPDATE/DELETE）。

前置：python3 -m scripts.seed 并启动服务。
"""
import csv
import io
import json
import os
import sys
import urllib.request
import urllib.error
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("KB_BASE", "http://127.0.0.1:8080")
PASS = "test123"
_passed = _failed = 0


def call(method, path, token=None, data=None, raw=None, ctype="application/json"):
    headers = {}
    body = None
    if raw is not None:
        body, headers["Content-Type"] = raw, ctype
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw_body = r.read()
            ctype_resp = r.headers.get("Content-Type", "")
            if "json" in ctype_resp:
                return r.status, json.loads(raw_body.decode("utf-8"))
            return r.status, raw_body.decode("utf-8-sig")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}  {detail}")


def multipart(fields, files):
    b = "----rb" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    for k, (fn, content) in files.items():
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                     f"filename=\"{fn}\"\r\nContent-Type: text/plain\r\n\r\n")
    return "".join(parts).encode() + content + f"\r\n--{b}--\r\n".encode(), \
        f"multipart/form-data; boundary={b}"


def login(email, password=PASS, slug="biolab"):
    st, r = call("POST", "/api/auth/login",
                 data={"email": email, "password": password, "tenant_slug": slug})
    assert st == 200, (email, st, r)
    return r


def upload(token, fields, filename, content):
    body, ct = multipart(fields, {"file": (filename, content.encode())})
    st, r = call("POST", "/api/documents", token=token, raw=body, ctype=ct)
    assert st == 200, (st, r)
    return r


def main():
    suf = uuid.uuid4().hex[:8]
    alice = login("alice@lab.cn", "demo123")
    at = alice["token"]
    bob = login("bob@lab.cn", "demo123")
    bt = bob["token"]

    # 一次性临时成员：承担 403 / 成员密级调整 / 踢人，避免污染演示账号状态
    temp_email = f"tmp_{suf}@lab.cn"
    call("POST", "/api/auth/register",
         data={"email": temp_email, "display_name": "临时", "password": PASS})
    call("POST", "/api/admin/members", token=at,
         data={"email": temp_email, "role": "member", "clearance": "internal",
               "team": "分子生物学团队", "department": "研发部"})
    tmp = login(temp_email)
    tmp_uid = tmp["user"]["id"]
    tt = tmp["token"]

    # ---------- 普通成员审计接口 403 ----------
    print("\n[访问控制] 普通成员不能查审计")
    st, r = call("GET", "/api/audit", token=tt)
    check("普通成员 GET /api/audit -> 403", st == 403, f"{st} {r}")
    st, r = call("GET", "/api/audit/export", token=tt)
    check("普通成员 GET /api/audit/export -> 403", st == 403, f"{st}")
    st, r = call("GET", "/api/audit", token=bt)
    check("Bob GET /api/audit -> 403", st == 403, f"{st} {r}")
    st, _ = call("GET", "/api/audit")
    check("匿名 GET /api/audit -> 401", st == 401, f"{st}")

    # ---------- 触发各类写操作 ----------
    print("\n[留痕] 各类权限写操作")
    d = upload(at, {"visibility": "private", "classification": "internal",
                    "title": f"审计用文档_{suf}"}, f"audit_{suf}.txt",
               "1 引言\n" + "审计测试正文内容，包含唯一标记 AUDITMK。" * 30 + "\n")
    did = d["document_id"]

    st, ch = call("GET", f"/api/documents/{did}/chunks", token=at)
    cid = ch[0]["chunk_id"]

    # 1) 文档密级 internal -> secret
    st, r = call("PUT", f"/api/documents/{did}/classification", token=at,
                 data={"classification": "secret"})
    check("改文档密级 secret", st == 200, str(r))
    # 2) 片段密级 -> sensitive
    st, r = call("PUT", f"/api/chunks/{cid}/classification", token=at,
                 data={"classification": "sensitive"})
    check("改片段密级 sensitive", st == 200, str(r))
    # 3) 片段可见性 -> public
    st, r = call("PUT", f"/api/chunks/{cid}/visibility", token=at,
                 data={"visibility": "public"})
    check("改片段可见性 public", st == 200, str(r))
    # 4) 对临时成员加 deny（不改动 Bob 状态）
    st, g = call("POST", f"/api/chunks/{cid}/grants", token=at,
                 data={"subject_type": "user", "subject_value": str(tmp_uid),
                       "effect": "deny", "expires_in": 3600})
    check("加 deny 规则", st == 200 and g["effect"] == "deny", str(g))
    deny_gid = g["grant_id"]
    # 5) 文档级 deny 规则（team）
    st, dr = call("POST", f"/api/documents/{did}/rules", token=at,
                  data={"subject_type": "team", "subject_value": "基因治疗团队",
                        "effect": "deny"})
    check("加文档级 deny", st == 200, str(dr))
    # 6) 成员密级调整：临时成员 internal -> secret
    st, r = call("PUT", f"/api/admin/members/{tmp_uid}/clearance", token=at,
                 data={"clearance": "secret"})
    check("调整成员密级 secret", st == 200, str(r))
    # 7) 删除授权 + 删除文档规则
    st, r = call("DELETE", f"/api/chunks/{cid}/grants/{deny_gid}", token=at)
    check("删除 grant", st == 200, str(r))
    st, r = call("DELETE", f"/api/documents/{did}/rules/{dr['rule_id']}", token=at)
    check("删除文档规则", st == 200, str(r))
    # 8) 踢人：移除临时成员并验证其令牌立即失效
    st, r = call("DELETE", f"/api/admin/members/{tmp_uid}", token=at)
    check("踢人并吊销会话", st == 200, str(r))
    st, _ = call("GET", "/api/me", token=tt)
    check("被踢令牌立即 401", st == 401, f"{st}")
    # 9) 删除文档
    st, r = call("DELETE", f"/api/documents/{did}", token=at)
    check("删除文档", st == 200, str(r))

    # ---------- 查询审计 ----------
    print("\n[查询] Alice 按文档/人/动作查审计")
    st, audit = call("GET", f"/api/audit?document_id={did}&limit=100", token=at)
    check("按文档查到审计", st == 200 and audit["count"] >= 8, f"{st} count={audit.get('count')}")
    actions = {x["action"] for x in audit["results"]}
    for want in ("document.upload", "document.classification_update",
                 "chunk.classification_update", "chunk.visibility_update",
                 "grant.add", "grant.delete", "document_rule.add",
                 "document_rule.delete", "document.delete"):
        check(f"含动作 {want}", want in actions, str(sorted(actions)))

    # 记录字段完整性
    sample = next(x for x in audit["results"] if x["action"] == "document.classification_update")
    check("记录含谁/何时/对象",
          sample["actor_id"] == alice["user"]["id"] and sample["actor_email"] == "alice@lab.cn"
          and sample["created_at"] and sample["document_id"] == did)
    check("记录含改前改后",
          sample["before"] == {"classification": "internal"}
          and sample["after"] == {"classification": "secret"}, str(sample))
    deny_row = next(x for x in audit["results"] if x["action"] == "grant.add")
    check("deny 记录含 effect=deny 与到期",
          deny_row["after"]["effect"] == "deny" and deny_row["after"]["expires_at"])

    # 成员密级/踢人无 document_id，按 actor + action 查
    st, a2 = call("GET",
                  f"/api/audit?actor_id={alice['user']['id']}&action=member.clearance_update&limit=20",
                  token=at)
    check("按人+动作查到成员密级变更",
          st == 200 and any(x["after"] == {"clearance": "secret"}
                            and x["before"] == {"clearance": "internal"}
                            and x["object_id"] == str(tmp_uid)
                            for x in a2["results"]), str(a2)[:200])
    st, a3 = call("GET", "/api/audit?action=member.remove&limit=20", token=at)
    check("查到踢人记录且对象为被踢成员",
          any(x["object_id"] == str(tmp_uid) and temp_email in x["summary"]
              for x in a3["results"]), str(a3)[:200])

    # 时间窗过滤
    import time as _t
    now = _t.time()
    st, future = call("GET", f"/api/audit?start={now + 1000}", token=at)
    check("未来时间窗为空", future["count"] == 0, str(future.get("count")))
    st, past = call("GET", f"/api/audit?end=0", token=at)
    check("1970 时间窗为空", past["count"] == 0, str(past.get("count")))

    # CSV 导出
    st, csv_text = call("GET", f"/api/audit/export?action=document.delete&limit=50", token=at)
    check("CSV 导出 200", st == 200 and "document.delete" in csv_text, str(csv_text)[:120])
    rows = list(csv.reader(io.StringIO(csv_text)))
    check("CSV 有表头且含删除记录", rows[0][0] == "id" and
          any(len(r) > 5 and r[5] == "document.delete" for r in rows[1:]))

    # ---------- 审计表只追加（直接连租户库验证触发器） ----------
    print("\n[只追加] 触发器拒绝改删")
    import sqlite3
    dbp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "tenants", "biolab.db")
    c = sqlite3.connect(dbp)
    aid = c.execute("SELECT id FROM audit_log ORDER BY id LIMIT 1").fetchone()[0]
    blocked_u = blocked_d = False
    try:
        c.execute("UPDATE audit_log SET summary='x' WHERE id=?", (aid,))
    except sqlite3.Error:
        blocked_u = True
    try:
        c.execute("DELETE FROM audit_log WHERE id=?", (aid,))
    except sqlite3.Error:
        blocked_d = True
    check("UPDATE 被触发器拒绝", blocked_u)
    check("DELETE 被触发器拒绝", blocked_d)
    c.close()

    # ---------- 跨租户：化学租户看不到生物实验室审计 ----------
    print("\n[跨租户隔离] chemmat 审计与 biolab 完全分离")
    erin = login("erin@chem.cn", "demo123", "chemmat")
    et = erin["token"]
    # erin 在自己租户做一个动作
    body, ct = multipart({"visibility": "team", "classification": "internal",
                          "owner_team": "催化团队"},
                         {"file": ("chem_audit.txt", "化学租户审计标记 CHEMAUDIT。".encode())})
    st, chemdoc = call("POST", "/api/documents", token=et, raw=body, ctype=ct)
    check("chemmat 内上传成功", st == 200, str(chemdoc))
    st, ca = call("GET", "/api/audit?limit=200", token=et)
    summaries = " ".join(x["summary"] for x in ca["results"])
    check("chemmat 审计不含 biolab 文档/成员",
          f"审计用文档_{suf}" not in summaries and "鲍勃" not in summaries, summaries[:200])
    check("chemmat 审计只含本租户上传",
          any(x["action"] == "document.upload" and "chem_audit" in x["summary"]
              for x in ca["results"]), summaries[:200])
    # 直接拿 biolab 的文档 id 在 chemmat 查不到
    st, cross = call("GET", f"/api/audit?document_id={did}", token=et)
    check("chemmat 按 biolab 文档 id 查审计为空", st == 200 and cross["count"] == 0, str(cross)[:150])

    print(f"\n结果：通过 {_passed}，失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
