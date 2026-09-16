#!/usr/bin/env python3
"""第三轮：三维权限（时限授权 + 显式 deny + 密级 clearance）端到端回归。

覆盖验收：
  - secret 片段即使 public/team/grant，clearance 不足时 search/ask/source 均不可得；
    列表不得因该片段暴露整篇机密文档；
  - 1 分钟时限授权过期前后行为可复现；deny 压过 grant 与 public；
  - admin 旁路密级/deny；读/管分离、踢人废会话、跨租户隔离不被破坏。

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


def register_and_join(alice_tok, suf, name, clearance="internal",
                      team="分子生物学团队", dept="研发部"):
    email = f"{name}_{suf}@lab.cn"
    call("POST", "/api/auth/register",
         data={"email": email, "display_name": name, "password": PASS})
    st, r = call("POST", "/api/admin/members", token=alice_tok,
                 data={"email": email, "role": "member", "team": team,
                       "department": dept, "clearance": clearance})
    assert st == 200, (email, st, r)
    return email, login(email)


def upload(at, fields, filename, content):
    body, ct = multipart(fields, {"file": (filename, content.encode())})
    st, r = call("POST", "/api/documents", token=at, raw=body, ctype=ct)
    assert st == 200, (st, r)
    return r


def long_section(marker):
    return ("本节内容用于形成独立片段，包含唯一标记 " + marker + "。") * 35


def main():
    suf = uuid.uuid4().hex[:8]
    alice = login("alice@lab.cn", "demo123")
    at = alice["token"]

    # 成员：Bob 实际演示账号 clearance=sensitive；新建 lo(internal)/hi(secret)
    bob = login("bob@lab.cn", "demo123")
    lo_email, lo = register_and_join(at, suf, "lo", clearance="internal")
    hi_email, hi = register_and_join(at, suf, "hi", clearance="secret")
    carol = login("carol@lab.cn", "demo123")

    # ============ 1. 密级：secret 片段 public 也绕不过 ============
    print("\n[密级] secret 内容对 clearance 不足者四处不可得")
    SEC = f"SECMK{suf}"
    content = f"1 公开章节\n{long_section('NORMAL'+suf)}\n\n2 机密章节\n{long_section(SEC)}\n"
    doc = upload(at, {"visibility": "public", "classification": "secret",
                      "title": f"密级测试_{suf}"}, f"cls_{suf}.txt", content)
    did = doc["document_id"]
    st, chunks = call("GET", f"/api/documents/{did}/chunks", token=at)
    secret_chunk = next(c for c in chunks if SEC in c["content"])
    cid = secret_chunk["chunk_id"]
    # 即使把该片段显式覆盖为 public、密级保持继承 secret
    st, _ = call("PUT", f"/api/chunks/{cid}/visibility", token=at,
                 data={"visibility": "public"})
    assert st == 200

    for label, tok, cl_ok in [("Bob(sensitive)", bob["token"], False),
                               ("lo(internal)", lo["token"], False),
                               ("hi(secret)", hi["token"], True),
                               ("alice(admin/internal? 实际secret)", at, True)]:
        st, r = call("POST", "/api/search", token=tok, data={"query": SEC})
        got = any(SEC in x["content"] for x in r["results"])
        check(f"{label} search 机密片段 {'可见' if cl_ok else '不可见'}", got == cl_ok, str(r)[:150])
        st, a = call("POST", "/api/ask", token=tok, data={"question": SEC})
        leaked = SEC in a.get("answer", "") or any(
            SEC in json.dumps(s, ensure_ascii=False) for s in a.get("sources", []))
        check(f"{label} ask 不泄露机密" if not cl_ok else f"{label} ask 可得",
              leaked == cl_ok, a.get("answer", "")[:80])
        st, src = call("GET", f"/api/documents/{did}/chunks/{cid}/source", token=tok)
        src_ok = st == 200 and SEC in src.get("exact_text", "")
        check(f"{label} source 溯源 {'200' if cl_ok else '404'}", src_ok == cl_ok, f"{st}")

    # 列表一致性：Bob/lo 能看到文档吗？
    # 文档含一个 public+secret 片段和一个 public+secret(继承) 片段——全部 secret，
    # clearance 不足者列表不应出现该文档。
    for label, tok in [("Bob", bob["token"]), ("lo", lo["token"])]:
        st, docs = call("GET", "/api/documents", token=tok)
        check(f"{label} 列表不出现纯机密文档", not any(d["id"] == did for d in docs))
    st, docs = call("GET", "/api/documents", token=hi["token"])
    check("hi 列表可见机密文档", any(d["id"] == did for d in docs))
    st, docs = call("GET", "/api/documents", token=at)
    check("admin 列表可见机密文档", any(d["id"] == did for d in docs))

    # ============ 2. 列表不得因机密片段暴露无权片段内容 ============
    print("\n[密级一致性] 同文档含 internal+secret 片段：低密级只见 internal 片段")
    MIX_I, MIX_S = f"MIXI{suf}", f"MIXS{suf}"
    mix = f"1 普通章节\n{long_section(MIX_I)}\n\n2 机密章节\n{long_section(MIX_S)}\n"
    d2 = upload(at, {"visibility": "public", "classification": "internal",
                     "title": f"混密级_{suf}"}, f"mix_{suf}.txt", mix)
    d2id = d2["document_id"]
    st, ch2 = call("GET", f"/api/documents/{d2id}/chunks", token=at)
    s_chunk = next(c for c in ch2 if MIX_S in c["content"])
    st, _ = call("PUT", f"/api/chunks/{s_chunk['chunk_id']}/classification",
                 token=at, data={"classification": "secret"})
    assert st == 200
    # Bob(sensitive) 对 secret 仍不足（sensitive<secret）
    st, r = call("POST", "/api/search", token=bob["token"], data={"query": MIX_S})
    check("Bob 搜不到混密级文档的 secret 片段", all(MIX_S not in x["content"] for x in r["results"]))
    st, r = call("POST", "/api/search", token=bob["token"], data={"query": MIX_I})
    check("Bob 能搜到 internal 片段", any(MIX_I in x["content"] for x in r["results"]))
    st, visible = call("GET", f"/api/documents/{d2id}/chunks", token=bob["token"])
    check("Bob 片段列表只见 internal、不含 secret 文本",
          all(MIX_S not in c["content"] for c in visible)
          and any(MIX_I in c["content"] for c in visible),
          str([c["content"][:20] for c in visible]))

    # ============ 3. 时限授权：过期前后 ============
    print("\n[时限] 60 秒授权给 Carol：过期前可见、过期后消失（用 2 秒加速复现）")
    TMG = f"TIMED{suf}"
    d3 = upload(at, {"visibility": "private", "classification": "secret",
                     "title": f"时限授权_{suf}"}, f"timed_{suf}.txt",
                f"1 机密\n{long_section(TMG)}\n")
    d3id = d3["document_id"]
    st, ch3 = call("GET", f"/api/documents/{d3id}/chunks", token=at)
    tc = next(c for c in ch3 if TMG in c["content"])["chunk_id"]
    carol_uid = carol["user"]["id"]
    # Carol clearance=sensitive，片段是 secret；授权也受密级闸门约束 -> 先给她 secret clearance
    st, _ = call("PUT", f"/api/admin/members/{carol_uid}/clearance", token=at,
                 data={"clearance": "secret"})
    assert st == 200
    carol = login("carol@lab.cn", "demo123")  # 重新登录取带新 clearance 的会话
    # 授权前不可见
    st, r = call("POST", "/api/search", token=carol["token"], data={"query": TMG})
    check("授权前 Carol 不可见", r["count"] == 0, str(r)[:120])
    # 2 秒时限授权
    st, g = call("POST", f"/api/chunks/{tc}/grants", token=at,
                 data={"subject_type": "user", "subject_value": str(carol_uid),
                       "effect": "allow", "expires_in": 2})
    check("创建 2 秒时限授权", st == 200 and g["expires_at"], str(g))
    st, r = call("POST", "/api/search", token=carol["token"], data={"query": TMG})
    check("授权有效期内 Carol 可见", any(TMG in x["content"] for x in r["results"]), str(r)[:120])
    print("    等待授权过期（3 秒）...")
    time.sleep(3)
    st, r = call("POST", "/api/search", token=carol["token"], data={"query": TMG})
    check("过期后立即从白名单消失", r["count"] == 0, str(r)[:120])
    # 恢复 Carol clearance（避免影响其它场景）
    call("PUT", f"/api/admin/members/{carol_uid}/clearance", token=at,
         data={"clearance": "sensitive"})

    # ============ 4. deny 压过 grant 与 public ============
    print("\n[deny] 显式拒绝优先于 grant/public/team")
    DNM = f"DENYMK{suf}"
    d4 = upload(at, {"visibility": "public", "classification": "internal",
                     "title": f"拒绝测试_{suf}"}, f"deny_{suf}.txt",
                f"1 公开\n{long_section(DNM)}\n")
    d4id = d4["document_id"]
    st, ch4 = call("GET", f"/api/documents/{d4id}/chunks", token=at)
    dc = next(c for c in ch4 if DNM in c["content"])["chunk_id"]
    # lo 原本因 public 可见
    st, r = call("POST", "/api/search", token=lo["token"], data={"query": DNM})
    check("deny 前 public 可见", any(DNM in x["content"] for x in r["results"]))
    # 即便先给一个 allow，再加 deny
    call("POST", f"/api/chunks/{dc}/grants", token=at,
         data={"subject_type": "user", "subject_value": str(lo["user"]["id"]), "effect": "allow"})
    st, _ = call("POST", f"/api/chunks/{dc}/grants", token=at,
                 data={"subject_type": "user", "subject_value": str(lo["user"]["id"]), "effect": "deny"})
    assert st == 200
    st, r = call("POST", "/api/search", token=lo["token"], data={"query": DNM})
    check("deny 压过 allow+public，检索为空", r["count"] == 0, str(r)[:120])
    st, a = call("POST", "/api/ask", token=lo["token"], data={"question": DNM})
    check("deny 后问答不含内容", DNM not in a.get("answer", "") and not a.get("sources"))
    st, _ = call("GET", f"/api/documents/{d4id}/chunks/{dc}/source", token=lo["token"])
    check("deny 后溯源 404", st == 404, f"{st}")
    # admin 旁路 deny
    st, r = call("POST", "/api/search", token=at, data={"query": DNM})
    check("admin 旁路 deny 仍可见", any(DNM in x["content"] for x in r["results"]))

    # 文档级 deny：对 lo 整篇拒绝
    st, _ = call("POST", f"/api/documents/{d4id}/rules", token=at,
                 data={"subject_type": "user", "subject_value": str(hi["user"]["id"]),
                       "effect": "deny"})
    assert st == 200
    st, docs = call("GET", "/api/documents", token=hi["token"])
    check("文档级 deny：hi 列表不再出现该文档", not any(d["id"] == d4id for d in docs))
    st, r = call("POST", "/api/search", token=hi["token"], data={"query": DNM})
    check("文档级 deny：hi 检索为空", r["count"] == 0)

    # ============ 5. 既有安全行为不被破坏 ============
    print("\n[回归] 踢人废会话 / 读管分离 / 跨租户隔离")
    # 踢人
    st, _ = call("DELETE", f"/api/admin/members/{lo['user']['id']}", token=at)
    check("管理员可移除成员", st == 200, f"{st}")
    st, _ = call("GET", "/api/me", token=lo["token"])
    check("被踢者旧令牌立即 401", st == 401, f"{st}")
    # 读管分离：Bob 可读 public 文档但不能删/改密级
    st, _ = call("DELETE", f"/api/documents/{d2id}", token=bob["token"])
    check("有读权限者不能删除文档", st == 403, f"{st}")
    st, _ = call("PUT", f"/api/chunks/{s_chunk['chunk_id']}/classification",
                 token=bob["token"], data={"classification": "internal"})
    check("有读权限者不能改密级", st == 403, f"{st}")
    # 跨租户仍全空
    erin = login("erin@chem.cn", "demo123", "chemmat")
    for q in [SEC, TMG, DNM, MIX_S, "纳米颗粒", "机密载体"]:
        st, r = call("POST", "/api/search", token=erin["token"], data={"query": q})
        check(f"跨租户隔离 chemmat 搜不到 ({q[:10]})", r["count"] == 0, str(r)[:80])

    print(f"\n结果：通过 {_passed}，失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
