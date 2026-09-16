#!/usr/bin/env python3
"""第五轮缺陷回归：
  Bug1 BM25 内存索引指纹碰撞：删除后重传同构文档，索引必须按单调代数重建，
        旧词 OLDIDX 消失、新词 NEWIDX 立即可检；
  Bug2 部分片段可见时，列表/详情不得返回整篇密级、未过滤的 chunk_count/char_count。

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


def upload(token, fields, filename, content):
    body, ct = multipart(fields, {"file": (filename, content.encode())})
    st, r = call("POST", "/api/documents", token=token, raw=body, ctype=ct)
    assert st == 200, (st, r)
    return r


def main():
    suf = uuid.uuid4().hex[:8]
    alice = login("alice@lab.cn", "demo123")
    at = alice["token"]
    bob = login("bob@lab.cn", "demo123")   # sensitive
    bt = bob["token"]

    # ================= Bug 1：指纹碰撞 =================
    print("\n[Bug1] 删除含 OLDIDX 的文档后重传同构 NEWIDX 文档，索引即时重建")
    body = ("实验方法与结果的固定结构段落，用于保证新旧文档结构尽量一致。" * 20)
    d_old = upload(at, {"visibility": "public", "classification": "internal",
                        "title": f"指纹旧_{suf}"}, f"old_{suf}.txt",
                   f"1 方法\n{body}\n\n唯一标记 OLDIDX{suf}，旧版内容。\n")
    old_id = d_old["document_id"]
    # 预热：让进程为当前代数构建倒排
    st, r = call("POST", "/api/search", token=bt, data={"query": f"OLDIDX{suf}"})
    check("预热后 OLDIDX 可检", r["count"] >= 1
          and all(f"OLDIDX{suf}" in x["content"] for x in r["results"]), str(r)[:120])

    # 删除旧文档
    st, r = call("DELETE", f"/api/documents/{old_id}", token=at)
    check("删除旧文档", st == 200, str(r))

    # 立刻重传“同构”文档（段落骨架一致），但标记换成 NEWIDX，且不含 OLDIDX
    d_new = upload(at, {"visibility": "public", "classification": "internal",
                        "title": f"指纹新_{suf}"}, f"new_{suf}.txt",
                   f"1 方法\n{body}\n\n唯一标记 NEWIDX{suf}，新版内容。\n")
    check("重传同构新文档", d_new["chunk_count"] >= 1, str(d_new.get("chunk_count")))

    st, r = call("POST", "/api/search", token=bt, data={"query": f"NEWIDX{suf}"})
    check("新标记 NEWIDX 立即可检（索引已重建）",
          r["count"] >= 1 and all(f"NEWIDX{suf}" in x["content"] for x in r["results"]),
          str(r)[:150])
    st, r = call("POST", "/api/search", token=bt, data={"query": f"OLDIDX{suf}"})
    check("旧标记 OLDIDX 已彻底消失（不返回新文档正文）",
          r["count"] == 0, str(r)[:150])
    # 问答同样不应引用旧词
    st, a = call("POST", "/api/ask", token=bt, data={"question": f"OLDIDX{suf} 是什么"})
    leaked = f"OLDIDX{suf}" in a.get("answer", "") or any(
        f"OLDIDX{suf}" in json.dumps(s, ensure_ascii=False) for s in a.get("sources", []))
    check("问答也不再泄露旧标记", not leaked, a.get("answer", "")[:80])

    # 反复删除/重建多次，单调代数必须每次都触发重建
    last_id = d_new["document_id"]
    for i in range(2):
        call("DELETE", f"/api/documents/{last_id}", token=at)
        mk = f"CYCLE{suf}_{i}"
        d_c = upload(at, {"visibility": "public", "classification": "internal",
                          "title": f"循环_{i}_{suf}"}, f"c{i}_{suf}.txt",
                     f"1 方法\n{body}\n\n循环标记 {mk}。\n")
        last_id = d_c["document_id"]
        st, r = call("POST", "/api/search", token=bt, data={"query": mk})
        check(f"第 {i+1} 轮重建后新词 {mk} 可检", r["count"] >= 1, str(r)[:100])

    # ================= Bug 2：信封信息按可见片段过滤 =================
    print("\n[Bug2] 部分片段可见：列表/详情只暴露可见片段的密级/计数/字数")
    SEC = f"ENVLSEC{suf}"
    INL = f"ENVLINL{suf}"
    sec_body = ("机密片段正文，涉及放行参数与工艺细节。" * 40)
    inl_body = ("内部片段正文，通用培训须知内容。" * 40)
    content = f"1 机密章节\n{sec_body}标记 {SEC}。\n\n2 内部章节\n标记 {INL}。{inl_body}\n"
    d2 = upload(at, {"visibility": "public", "classification": "secret",
                     "title": f"信封_{suf}"}, f"env_{suf}.txt", content)
    d2id = d2["document_id"]
    st, ch = call("GET", f"/api/documents/{d2id}/chunks", token=at)
    sec_c = next(c for c in ch if (c.get("heading_path") or "").startswith("1 机密章节"))
    inl_c = next(c for c in ch if (c.get("heading_path") or "").startswith("2 内部章节"))
    # 将内部章节下调为 internal，机密章节保持 secret
    call("PUT", f"/api/chunks/{inl_c['chunk_id']}/classification", token=at,
         data={"classification": "internal"})

    # 取内部片段的精确长度（char_end-char_start）
    st, src = call("GET",
                   f"/api/documents/{d2id}/chunks/{inl_c['chunk_id']}/source", token=at)
    inl_len = src["char_range"][1] - src["char_range"][0]
    sec_len = call("GET",
                   f"/api/documents/{d2id}/chunks/{sec_c['chunk_id']}/source", token=at)[1]
    sec_len = sec_len["char_range"][1] - sec_len["char_range"][0]

    # Bob(sensitive) 只见 internal 片段
    st, docs = call("GET", "/api/documents", token=bt)
    row = next((d for d in docs if d["id"] == d2id), None)
    check("Bob 列表能见到该文档（因含 internal 片段）", row is not None)
    if row:
        check("列表 classification 只反映可见片段（internal，非 secret）",
              row["classification"] == "internal", str(row.get("classification")))
        check("列表 chunk_count=1（隐藏机密分片数）", row["chunk_count"] == 1,
              str(row.get("chunk_count")))
        check("列表 char_count 仅统计可见片段",
              abs(row["char_count"] - inl_len) <= 2,
              f"{row.get('char_count')} vs {inl_len}")
        check("列表 char_count 不等于整篇字数",
              row["char_count"] < inl_len + sec_len - 10)

    st, detail = call("GET", f"/api/documents/{d2id}", token=bt)
    check("详情 200", st == 200, f"{st}")
    check("详情 classification=internal", detail.get("classification") == "internal",
          str(detail.get("classification")))
    check("详情 chunk_count=1", detail.get("chunk_count") == 1, str(detail.get("chunk_count")))
    check("详情 char_count 仅可见片段",
              abs(detail.get("char_count", 0) - inl_len) <= 2,
              f"{detail.get('char_count')} vs {inl_len}")

    # admin 看到整篇信封：secret 密级、2 个片段、全文字数
    st, adocs = call("GET", "/api/documents", token=at)
    arow = next(d for d in adocs if d["id"] == d2id)
    check("admin 列表 classification=secret", arow["classification"] == "secret",
          str(arow.get("classification")))
    check("admin 列表 chunk_count=2", arow["chunk_count"] == 2, str(arow.get("chunk_count")))
    st, ad = call("GET", f"/api/documents/{d2id}", token=at)
    check("admin 详情 char_count=整篇字数", ad["char_count"] >= inl_len + sec_len - 2,
          f"{ad['char_count']} vs {inl_len}+{sec_len}")

    print(f"\n结果：通过 {_passed}，失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
