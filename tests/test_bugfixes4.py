#!/usr/bin/env python3
"""第四轮缺陷回归：
  Bug1 溯源 source 的 before/after 窗口必须按相邻片段 ACL/密级裁切，
        不得通过原文窗口泄出无权（secret/deny）正文；
  Bug2 文档级可见但全部片段被 chunk deny 时，不得出现“空壳文档”
        （列表/详情/search/chunks 四处语义一致）。

前置：python3 -m scripts.seed 并启动服务。
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


def upload(at, fields, filename, content):
    body, ct = multipart(fields, {"file": (filename, content.encode())})
    st, r = call("POST", "/api/documents", token=at, raw=body, ctype=ct)
    assert st == 200, (st, r)
    return r


def main():
    suf = uuid.uuid4().hex[:8]
    alice = login("alice@lab.cn", "demo123")
    at = alice["token"]
    bob = login("bob@lab.cn", "demo123")          # clearance=sensitive
    bt = bob["token"]
    bob_uid = bob["user"]["id"]

    # ================= Bug 1：溯源窗口裁切 =================
    print("\n[Bug1] 相邻 secret/internal 片段：internal 的 source 窗口不得带出密文")
    SECRET_TAIL = f"机密窗口标记 SECWIN{suf}，仅限 secret clearance。"
    INTERNAL_HEAD = f"内部窗口标记 INLWIN{suf}，sensitive 可读。"
    # 两节均足够长以各自独立成片段；密文标记放在第一节结尾（与第二节物理相邻 <120 字）
    sec_body = ("机密章节正文描述中试工艺与放行参数，内容较长用于形成独立片段。" * 30)
    inl_body = ("内部章节正文描述通用培训要求，内容同样较长用于形成独立片段。" * 30)
    content = (f"1 机密章节\n{sec_body}{SECRET_TAIL}\n\n"
               f"2 内部章节\n{INTERNAL_HEAD}{inl_body}\n")
    doc = upload(at, {"visibility": "public", "classification": "secret",
                      "title": f"窗口裁切_{suf}"}, f"win_{suf}.txt", content)
    did = doc["document_id"]
    st, chunks = call("GET", f"/api/documents/{did}/chunks", token=at)
    sec_chunk = next(c for c in chunks
                     if (c.get("heading_path") or "").startswith("1 机密章节"))
    inl_chunk = next(c for c in chunks
                     if (c.get("heading_path") or "").startswith("2 内部章节"))
    check("机密/内部片段相互独立", sec_chunk["chunk_id"] != inl_chunk["chunk_id"],
          str([(c["chunk_id"], c["content"][:12]) for c in chunks]))
    # 文档密级 secret，默认两片段都继承 secret；把内部章节下调为 internal
    st, r = call("PUT", f"/api/chunks/{inl_chunk['chunk_id']}/classification",
                 token=at, data={"classification": "internal"})
    check("将相邻片段下调为 internal", st == 200, str(r))

    # Bob 对 secret 片段 source 必须 404
    st, r = call("GET",
                 f"/api/documents/{did}/chunks/{sec_chunk['chunk_id']}/source", token=bt)
    check("Bob 对 secret 片段 source 返回 404", st == 404, f"{st}")

    # Bob 对 internal 片段 source 200，但 before/after 不得含密文标记
    st, src = call("GET",
                   f"/api/documents/{did}/chunks/{inl_chunk['chunk_id']}/source", token=bt)
    check("Bob 对 internal 片段 source 返回 200", st == 200, f"{st} {src}")
    window = src.get("before", "") + src.get("exact_text", "") + src.get("after", "")
    check("internal source 窗口不含相邻密文 SECWIN",
          st == 200 and f"SECWIN{suf}" not in window,
          f"before={src.get('before','')[-60:]!r}")
    check("before 窗口不残留相邻无权片段的标题「机密章节」",
          "机密章节" not in src.get("before", ""), repr(src.get("before", "")[-60:]))
    check("internal source 仍包含自身 INLWIN", f"INLWIN{suf}" in src.get("exact_text", ""))
    check("窗口被裁切标记 context_clipped=true", bool(src.get("context_clipped")),
          str({k: src.get(k) for k in ("context_clipped", "clipped_side")}))

    # 反向场景：internal 在前、secret 在后 -> 密文标记放在 secret 段开头（贴近边界 <120）
    SECRET_TAIL2 = f"首部密文 HEADSEC{suf}，紧随内部片段之后。"
    content2 = (f"1 内部章节\n{inl_body}{INTERNAL_HEAD}\n\n"
                f"2 机密章节\n{SECRET_TAIL2}{sec_body}\n")
    d2 = upload(at, {"visibility": "public", "classification": "secret",
                     "title": f"窗口裁切2_{suf}"}, f"win2_{suf}.txt", content2)
    st, ch2 = call("GET", f"/api/documents/{d2['document_id']}/chunks", token=at)
    i2 = next(c for c in ch2
              if (c.get("heading_path") or "").startswith("1 内部章节"))
    s2 = next(c for c in ch2
              if (c.get("heading_path") or "").startswith("2 机密章节"))
    call("PUT", f"/api/chunks/{i2['chunk_id']}/classification", token=at,
         data={"classification": "internal"})
    st, src2 = call("GET",
                    f"/api/documents/{d2['document_id']}/chunks/{i2['chunk_id']}/source", token=bt)
    window2 = src2.get("before", "") + src2.get("exact_text", "") + src2.get("after", "")
    check("internal(在前) 的 after 不含相邻密文 TAILSEC",
          st == 200 and f"HEADSEC{suf}" not in window2,
          f"after={src2.get('after','')[:60]!r}")
    check("after 窗口不残留相邻无权片段的标题「机密章节」",
          "机密章节" not in src2.get("after", ""), repr(src2.get("after", "")[:60]))
    check("Bob 对后随 secret 片段 source 404",
          call("GET", f"/api/documents/{d2['document_id']}/chunks/{s2['chunk_id']}/source",
               token=bt)[0] == 404)

    # admin 旁路：Alice 看 internal 片段可以拿到完整邻接上下文
    st, asrc = call("GET",
                    f"/api/documents/{did}/chunks/{inl_chunk['chunk_id']}/source", token=at)
    check("admin 溯源不受裁切、可见邻接密文",
          st == 200 and f"SECWIN{suf}" in
          (asrc.get("before", "") + asrc.get("exact_text", "") + asrc.get("after", "")))

    # ================= Bug 2：空壳文档 =================
    print("\n[Bug2] public 文档但全部片段 deny Bob：四处一致不可见")
    m1 = f"SHELL1{suf}"
    m2 = f"SHELL2{suf}"
    content3 = f"1 章节A\n{('公开内容A '+m1+'。')*40}\n\n2 章节B\n{('公开内容B '+m2+'。')*40}\n"
    d3 = upload(at, {"visibility": "public", "classification": "internal",
                     "title": f"空壳文档_{suf}"}, f"shell_{suf}.txt", content3)
    d3id = d3["document_id"]
    st, ch3 = call("GET", f"/api/documents/{d3id}/chunks", token=at)
    check("deny 前 Bob 可见 public 文档",
          any(d["id"] == d3id for d in call("GET", "/api/documents", token=bt)[1]))
    # 对 Bob 在每个片段上加 user deny
    for c in ch3:
        st, r = call("POST", f"/api/chunks/{c['chunk_id']}/grants", token=at,
                     data={"subject_type": "user", "subject_value": str(bob_uid),
                           "effect": "deny"})
        assert st == 200, r
    check("已对 Bob 拒绝全部片段", len(ch3) >= 2)

    # 四处必须一致：列表、详情、检索、片段列表
    st, docs = call("GET", "/api/documents", token=bt)
    check("列表不出现空壳文档", not any(d["id"] == d3id for d in docs),
          str([d["title"] for d in docs if d["id"] == d3id]))
    st, detail = call("GET", f"/api/documents/{d3id}", token=bt)
    check("详情返回 404（不是 200 空壳）", st == 404, f"{st}")
    st, r = call("POST", "/api/search", token=bt, data={"query": f"{m1} {m2}"})
    check("检索命中 0", r["count"] == 0, str(r)[:120])
    for q in (m1, m2):
        st, r = call("POST", "/api/search", token=bt, data={"query": q})
        check(f"检索 {q} 为 0", r["count"] == 0)
    st, visible = call("GET", f"/api/documents/{d3id}/chunks", token=bt)
    check("片段列表整体 404（文档已不可进入）", st == 404, f"{st}")

    # owner/admin 不受影响
    st, docs = call("GET", "/api/documents", token=at)
    check("所有者(admin)列表仍可见", any(d["id"] == d3id for d in docs))
    st, r = call("POST", "/api/search", token=at, data={"query": m1})
    check("所有者检索仍命中", any(m1 in x["content"] for x in r["results"]))

    # 其它无关用户（Dave internal，同文档 public internal）仍可见 -> 证明 deny 只针对 Bob
    dave = login("dave@lab.cn", "demo123")
    st, docs = call("GET", "/api/documents", token=dave["token"])
    check("deny 不影响其他用户（Dave 列表可见）", any(d["id"] == d3id for d in docs))
    st, r = call("POST", "/api/search", token=dave["token"], data={"query": m1})
    check("Dave 检索仍命中", any(m1 in x["content"] for x in r["results"]))

    print(f"\n结果：通过 {_passed}，失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
