#!/usr/bin/env python3
"""第七轮缺陷回归：
  Bug1 问答 TOCTOU：检索拿到片段后、返回前若被 deny/降密，本次响应不得再带出内容
        （含模拟慢 LLM 期间被 deny 的场景）；
  Bug2 POST /api/admin/members 更新已有成员时正确留痕（member.update + 密级走
        member.clearance_update，含改前改后），不再伪装成 member.add。

前置：python3 -m scripts.seed 并启动服务。
"""
import json
import os
import sys
import time
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
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"error": "non-json"}


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

    # ---------- Bug 1：进程内直接驱动 answer_question，精确制造 TOCTOU ----------
    print("\n[Bug1] 问答返回前权限复核（抽取式 + 模拟慢 LLM）")
    from app.core import db_global, db_tenant
    from app.core import qa as qa_mod
    from app import config

    g = db_global.connect()
    trow = db_global.get_tenant_by_slug(g, "biolab")
    arow = db_global.get_user_by_email(g, "alice@lab.cn")
    brow = db_global.get_user_by_email(g, "bob@lab.cn")
    g.close()

    def ctx_for(uid, clearance="sensitive", role="member", team="分子生物学团队"):
        return {"user_id": uid, "tenant_id": trow["id"], "tenant_role": role,
                "team": team, "department": "研发部", "clearance": clearance}

    tconn = db_tenant.connect("biolab", trow["id"])

    # 直接构造一个 public internal 文档与片段（绕过 HTTP，便于精确控制时序）
    mk = f"TOCTOU{suf}"
    full = f"1 数据章节\n关键结论标记 {mk}，这是实验得到的唯一数值结果。"
    did = db_tenant.create_document(
        tconn, tenant_id=trow["id"], title=f"TOCTOU文档_{suf}",
        source_name=f"t_{suf}.txt", file_type="txt", blob_path="x",
        full_text=full, visibility="public",
        owner_user_id=arow["id"], owner_team=None, owner_dept=None,
        classification="internal")
    cid = db_tenant.insert_chunk(
        tconn, document_id=did, chunk_index=0, heading_path="1 数据章节",
        content=full[full.index("\n")+1:], char_start=full.index("\n")+1,
        char_end=len(full), page_start=1, page_end=1, visibility=None)
    db_tenant.bump_index_generation(tconn)
    tconn.commit()

    bob_ctx = ctx_for(brow["id"])

    # 正常情况：Bob 能问到
    r0 = qa_mod.answer_question("biolab", tconn, bob_ctx, mk)
    check("无 deny 时答案含标记", mk in r0["answer"] and r0["sources"], r0["answer"][:60])

    # 场景 A：抽取式——在 search 之后、最终复核之前 deny
    orig_search = qa_mod.search_index.search

    def search_then_deny(slug, conn, ctx, question, top_k=None):
        hits = orig_search(slug, conn, ctx, question, top_k=top_k)
        # 模拟“检索之后管理员立刻加 deny”
        db_tenant.add_grant(conn, cid, "user", str(brow["id"]), "deny")
        db_tenant.bump_index_generation(conn)
        conn.commit()
        return hits

    qa_mod.search_index.search = search_then_deny
    try:
        r1 = qa_mod.answer_question("biolab", tconn, bob_ctx, mk)
        leaked = mk in r1["answer"] or any(
            mk in json.dumps(s, ensure_ascii=False) for s in r1["sources"])
        check("检索后被 deny：本次抽取式答案不含标记", not leaked, r1["answer"][:80])
        check("来源为空", r1["sources"] == [], str(r1["sources"])[:120])
    finally:
        qa_mod.search_index.search = orig_search
    # 撤销 deny，恢复
    tconn.execute("DELETE FROM chunk_grants WHERE chunk_id=? AND subject_value=?",
                  (cid, str(brow["id"])))
    db_tenant.bump_index_generation(tconn); tconn.commit()

    # 场景 B：模拟慢 LLM——LLM 调用期间被 deny，返回内容必须被丢弃并降级
    calls = {"n": 0}

    def fake_slow_llm(question, hits):
        calls["n"] += 1
        # 模拟 LLM 处理耗时，期间管理员加 deny
        db_tenant.add_grant(tconn, cid, "user", str(brow["id"]), "deny")
        db_tenant.bump_index_generation(tconn); tconn.commit()
        time.sleep(0.2)
        return f"根据资料，{mk} 的结果非常关键（[来源1]）。"

    old_llm = qa_mod._llm_answer
    old_cfg = (config.LLM_BASE_URL, config.LLM_API_KEY)
    config.LLM_BASE_URL, config.LLM_API_KEY = "http://configured/v1", "k"
    qa_mod._llm_answer = fake_slow_llm
    try:
        r2 = qa_mod.answer_question("biolab", tconn, bob_ctx, mk)
        leaked = mk in r2["answer"] or any(
            mk in json.dumps(s, ensure_ascii=False) for s in r2["sources"])
        check("慢 LLM 期间被 deny：丢弃生成答案，不含标记", not leaked, r2["answer"][:80])
        check("mode 回退为 extractive", r2["mode"] == "extractive", r2["mode"])
        check("确实调用过 LLM", calls["n"] == 1, str(calls))
        check("来源为空", r2["sources"] == [], str(r2["sources"])[:100])
    finally:
        qa_mod._llm_answer = old_llm
        config.LLM_BASE_URL, config.LLM_API_KEY = old_cfg
    tconn.execute("DELETE FROM chunk_grants WHERE chunk_id=? AND subject_value=?",
                  (cid, str(brow["id"])))
    db_tenant.bump_index_generation(tconn); tconn.commit()

    # 对照：LLM 慢但期间没有权限变化，应正常采用 LLM 答案
    def fake_slow_llm_ok(question, hits):
        time.sleep(0.1)
        return f"结论 {mk} 正常返回（[来源1]）。"
    config.LLM_BASE_URL, config.LLM_API_KEY = "http://configured/v1", "k"
    qa_mod._llm_answer = fake_slow_llm_ok
    try:
        r3 = qa_mod.answer_question("biolab", tconn, bob_ctx, mk)
        check("LLM 慢但权限未变：正常采用 LLM 答案",
              r3["mode"] == "llm" and mk in r3["answer"], r3["mode"])
    finally:
        qa_mod._llm_answer = old_llm
        config.LLM_BASE_URL, config.LLM_API_KEY = old_cfg

    # 清理直接构造的文档
    db_tenant.delete_document(tconn, did, trow["id"])
    tconn.commit()
    tconn.close()

    # ---------- Bug 2：HTTP 层成员更新留痕 ----------
    print("\n[Bug2] 重复 POST 成员：更新留痕、密级走专门动作")
    email = f"up_{suf}@lab.cn"
    call("POST", "/api/auth/register",
         data={"email": email, "display_name": "升级", "password": PASS})
    st, r1 = call("POST", "/api/admin/members", token=at,
                  data={"email": email, "role": "member", "clearance": "internal",
                        "team": "分子生物学团队", "department": "研发部"})
    check("首次加入 is_new=true", st == 200 and r1.get("is_new") is True, str(r1))
    uid = r1.get("role") and \
        db_global.connect().execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]

    st, r2 = call("POST", "/api/admin/members", token=at,
                  data={"email": email, "role": "admin", "clearance": "secret",
                        "team": "分子生物学团队", "department": "研发部"})
    check("再次提交 is_new=false 且描述变更", st == 200 and r2.get("is_new") is False
          and "secret" in r2["message"], str(r2))

    st, aud = call("GET",
                   f"/api/audit?action=member.clearance_update&limit=50", token=at)
    rows = [x for x in aud["results"] if x["object_id"] == str(uid)]
    check("密级变更走 member.clearance_update 且 before/after 正确",
          len(rows) == 1 and rows[0]["before"] == {"clearance": "internal"}
          and rows[0]["after"] == {"clearance": "secret"}, str(rows))
    st, adds = call("GET", "/api/audit?action=member.add&limit=50", token=at)
    check("member.add 仅首次一条",
          sum(1 for x in adds["results"] if email in x["summary"]) == 1)
    st, ups = call("GET", "/api/audit?action=member.update&limit=50", token=at)
    u_rows = [x for x in ups["results"] if x["object_id"] == str(uid)]
    check("member.update 一条且含完整改前快照（role/team/department/clearance）",
          len(u_rows) == 1 and u_rows[0]["before"]["role"] == "member"
          and u_rows[0]["after"]["role"] == "admin", str(u_rows)[:200])

    # 无实质变化的重复提交：记 member.update 但不应产生新的 clearance_update
    before_n = len(rows)
    st, r3 = call("POST", "/api/admin/members", token=at,
                  data={"email": email, "role": "admin", "clearance": "secret",
                        "team": "分子生物学团队", "department": "研发部"})
    st, aud2 = call("GET",
                    f"/api/audit?action=member.clearance_update&limit=50", token=at)
    rows2 = [x for x in aud2["results"] if x["object_id"] == str(uid)]
    check("无字段变化时不新增 clearance_update", len(rows2) == before_n, str(len(rows2)))

    print(f"\n结果：通过 {_passed}，失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
