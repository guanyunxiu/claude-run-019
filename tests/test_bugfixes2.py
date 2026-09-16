#!/usr/bin/env python3
"""第二轮缺陷回归：
  Bug1 团队公开文档归属团队为空导致同团队成员不可见；
  Bug2 private 文档片段覆盖为 public 后文档不出现在列表；
  Bug3 LLM 调用失败降级后 mode 仍报 llm。

前置：python3 -m scripts.seed 并启动服务。
  KB_BASE=http://127.0.0.1:8080 python3 tests/test_bugfixes2.py
"""
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


def main():
    suf = uuid.uuid4().hex[:8]
    alice = login("alice@lab.cn", "demo123")
    bob = login("bob@lab.cn", "demo123")
    at, bt = alice["token"], bob["token"]

    # ================= Bug 1 =================
    print("\n[Bug1] 团队公开文档：管理员有团队归属，缺省继承；无团队用户必填")
    st, me = call("GET", "/api/me", token=at)
    check("Alice 具备团队归属（seed 已修复）", me.get("team") == "分子生物学团队", str(me))

    body, ct = multipart({"visibility": "team"},
                         {"file": (f"teamdefault_{suf}.txt",
                                   f"团队缺省归属测试，关键词 TEAMDEFAULT_{suf}。\n".encode())})
    st, doc = call("POST", "/api/documents", token=at, raw=body, ctype=ct)
    check("Alice 不填归属团队上传团队文档成功", st == 200, f"{st} {doc}")

    st, docs = call("GET", "/api/documents", token=bt)
    check("同团队 Bob 列表可见", any(d["id"] == doc["document_id"] for d in docs), str(st))
    st, r = call("POST", "/api/search", token=bt, data={"query": f"TEAMDEFAULT_{suf}"})
    check("同团队 Bob 检索可命中", st == 200 and r["count"] >= 1, str(r)[:160])

    # 注册一个无团队/部门的成员 nora
    nora_email = f"nora_{suf}@lab.cn"
    call("POST", "/api/auth/register",
         data={"email": nora_email, "display_name": "诺拉", "password": PASS})
    st, _ = call("POST", "/api/admin/members", token=at,
                 data={"email": nora_email, "role": "member"})
    check("无团队成员加入租户", st == 200, str(st))
    nora = login(nora_email)
    nt = nora["token"]

    body, ct = multipart({"visibility": "team"},
                         {"file": ("bad.txt", "x".encode())})
    st, r = call("POST", "/api/documents", token=nt, raw=body, ctype=ct)
    check("无团队用户选团队公开且不填归属 -> 400", st == 400, f"{st} {r}")
    body, ct = multipart({"visibility": "department"},
                         {"file": ("bad2.txt", "x".encode())})
    st, r = call("POST", "/api/documents", token=nt, raw=body, ctype=ct)
    check("无部门用户选部门公开且不填归属 -> 400", st == 400, f"{st} {r}")
    body, ct = multipart({"visibility": "team", "owner_team": "分子生物学团队"},
                         {"file": ("good.txt", f"显式团队 GRANTED_{suf}".encode())})
    st, r = call("POST", "/api/documents", token=nt, raw=body, ctype=ct)
    check("无团队用户显式填写归属团队 -> 200", st == 200, f"{st} {r}")

    # ================= Bug 2 =================
    print("\n[Bug2] private 文档中单个片段覆盖为 public：列表/检索一致可见")
    # 构造两个 >900 字的章节，确保分成独立片段
    priv_para = ("私有段落内容，标记 PRIVMK_" + suf + "，仅所有者可见。" * 40)
    pub_para = ("公开段落内容，标记 PUBMK_" + suf + "，覆盖为租户公开。" * 40)
    content = f"1 私有章节\n{priv_para}\n\n2 公开章节\n{pub_para}\n"
    body, ct = multipart({"visibility": "private", "title": f"私含公片段_{suf}"},
                         {"file": (f"privpub_{suf}.txt", content.encode())})
    st, doc2 = call("POST", "/api/documents", token=at, raw=body, ctype=ct)
    check("上传 private 多片段文档", st == 200 and doc2["chunk_count"] >= 2,
          f"{st} chunk={doc2.get('chunk_count')}")
    did = doc2["document_id"]

    st, docs = call("GET", "/api/documents", token=bt)
    check("覆盖前 Bob 列表不可见", not any(d["id"] == did for d in docs))

    st, chunks = call("GET", f"/api/documents/{did}/chunks", token=at)
    pub_chunk = next((c for c in chunks if f"PUBMK_{suf}" in c["content"]), None)
    priv_chunk = next((c for c in chunks if f"PRIVMK_{suf}" in c["content"]), None)
    check("公私片段各自独立", pub_chunk and priv_chunk and pub_chunk["chunk_id"] != priv_chunk["chunk_id"],
          str([(c["chunk_id"], c["content"][:20]) for c in chunks]))

    st, r = call("PUT", f"/api/chunks/{pub_chunk['chunk_id']}/visibility",
                 token=at, data={"visibility": "public"})
    check("将公开片段覆盖为 public", st == 200, str(r))

    st, docs = call("GET", "/api/documents", token=bt)
    check("覆盖后 Bob 列表出现该文档", any(d["id"] == did for d in docs), "列表缺失")
    st, r = call("POST", "/api/search", token=bt, data={"query": f"PUBMK_{suf}"})
    check("Bob 可检索到公开片段", st == 200 and r["count"] >= 1
          and all(f"PRIVMK_{suf}" not in x["content"] for x in r["results"]), str(r)[:200])
    st, r = call("POST", "/api/search", token=bt, data={"query": f"PRIVMK_{suf}"})
    check("Bob 检索不到仍私有的片段", r["count"] == 0, str(r)[:200])
    st, detail = call("GET", f"/api/documents/{did}", token=bt)
    check("Bob 可打开文档详情", st == 200, f"{st}")
    st, visible = call("GET", f"/api/documents/{did}/chunks", token=bt)
    check("片段列表只含公开片段、不含私有片段",
          st == 200 and len(visible) >= 1
          and all(f"PRIVMK_{suf}" not in c["content"] for c in visible)
          and any(f"PUBMK_{suf}" in c["content"] for c in visible),
          str([c["content"][:20] for c in visible]))

    # ================= Bug 3（进程内直接验证 qa 降级标记） =================
    print("\n[Bug3] LLM 已配置但调用失败：mode 必须为 extractive 且 degraded=true")
    from app import config
    from app.core import db_global, db_tenant
    from app.core.qa import answer_question

    g = db_global.connect()
    arow = db_global.get_user_by_email(g, "alice@lab.cn")
    trow = db_global.get_tenant_by_slug(g, "biolab")
    mem = db_global.get_membership(g, trow["id"], arow["id"])
    g.close()
    ctx = {"user_id": arow["id"], "tenant_id": trow["id"],
           "tenant_role": mem["role"], "team": mem["team"], "department": mem["department"]}
    tconn = db_tenant.connect("biolab", trow["id"])

    old = (config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_TIMEOUT)
    # 指向一个立即拒绝连接的地址，快速触发失败降级
    config.LLM_BASE_URL = "http://127.0.0.1:9/v1"
    config.LLM_API_KEY = "test-key"
    config.LLM_TIMEOUT = 1.0
    try:
        res = answer_question("biolab", tconn, ctx, "包封率 LPN-207")
    finally:
        config.LLM_BASE_URL, config.LLM_API_KEY, config.LLM_TIMEOUT = old
    tconn.close()
    check("有命中时仍给出答案", bool(res.get("answer")), str(res)[:200])
    check("降级后 mode == extractive", res.get("mode") == "extractive", str(res.get("mode")))
    check("degraded 标记为 true", res.get("degraded") is True, str(res.get("degraded")))
    check("llm_configured 为 true（确实尝试过 LLM）", res.get("llm_configured") is True)
    check("仍附带来源溯源", len(res.get("sources", [])) >= 1)

    print(f"\n结果：通过 {_passed}，失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
