#!/usr/bin/env python3
"""三个已修复缺陷的专项回归测试。

前置：先 `python3 -m scripts.seed` 并启动服务（默认 http://127.0.0.1:8080）。
  KB_BASE=http://127.0.0.1:8080 python3 tests/test_bugfixes.py
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
        with urllib.request.urlopen(req, timeout=10) as r:
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
    pre = "".join(parts).encode("utf-8")
    return pre + content + f"\r\n--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def main():
    suf = uuid.uuid4().hex[:8]
    # 用演示租户管理员 alice 做成员管理
    st, alice = call("POST", "/api/auth/login",
                     data={"email": "alice@lab.cn", "password": "demo123", "tenant_slug": "biolab"})
    assert st == 200, ("需要先 seed 并启动服务", alice)

    # 注册一个受害者/被踢成员 vince，和一个旁观成员 wendy
    vince_email = f"vince_{suf}@lab.cn"
    wendy_email = f"wendy_{suf}@lab.cn"
    for email, name in [(vince_email, "文斯"), (wendy_email, "温蒂")]:
        st, r = call("POST", "/api/auth/register",
                     data={"email": email, "display_name": name, "password": PASS})
        check(f"注册 {email}", st == 200, r)
    st, r = call("POST", "/api/admin/members", token=alice["token"],
                 data={"email": vince_email, "role": "member",
                       "team": "分子生物学团队", "department": "研发部"})
    check("文斯加入 biolab", st == 200, r)
    st, r = call("POST", "/api/admin/members", token=alice["token"],
                 data={"email": wendy_email, "role": "member",
                       "team": "分子生物学团队", "department": "研发部"})
    check("温蒂加入 biolab", st == 200, r)

    st, vince = call("POST", "/api/auth/login",
                     data={"email": vince_email, "password": PASS, "tenant_slug": "biolab"})
    check("文斯登录拿到令牌", st == 200, r)
    vince_token = vince["token"]
    vince_uid = vince["user"]["id"]
    st, wendy = call("POST", "/api/auth/login",
                     data={"email": wendy_email, "password": PASS, "tenant_slug": "biolab"})
    wendy_token = wendy["token"]

    marker = f"INJECT_{suf}"

    # ========== Bug 1：移出租户后旧令牌立即失效，无法再鉴权/写入 ==========
    print("\n[Bug1] 成员移出租户后旧 Bearer 令牌必须立即失效")
    # 踢人之前令牌可用
    st, r = call("GET", "/api/me", token=vince_token)
    check("被踢前令牌有效", st == 200, f"{st}")

    st, r = call("DELETE", f"/api/admin/members/{vince_uid}", token=alice["token"])
    check("管理员移除成员", st == 200, r)

    # 旧令牌立刻 401
    st, r = call("GET", "/api/me", token=vince_token)
    check("旧令牌访问 /api/me 返回 401", st == 401, f"{st} {r}")

    # 用旧令牌尝试上传 public 文档 —— 必须被拒
    payload, ct = multipart({"visibility": "public"},
                            {"file": (f"evil_{suf}.txt", f"注入内容 {marker} 不应入库".encode())})
    st, r = call("POST", "/api/documents", token=vince_token, raw=payload, ctype=ct)
    check("旧令牌上传被拒绝(401)", st == 401, f"{st} {r}")

    # 同租户其他人检索不到注入标记
    st, r = call("POST", "/api/search", token=wendy_token, data={"query": marker})
    check("被踢者无法注入：同租户成员检索不到标记", st == 200 and r["count"] == 0, str(r)[:200])
    st, r = call("POST", "/api/search", token=alice["token"], data={"query": marker})
    check("管理员同样检索不到注入标记", r["count"] == 0, str(r)[:200])

    # 被踢者重新登录也应被拒（不再是租户成员）
    st, r = call("POST", "/api/auth/login",
                 data={"email": vince_email, "password": PASS, "tenant_slug": "biolab"})
    check("被踢后无法再登录该租户(403)", st == 403, f"{st} {r}")

    # 非管理员不能踢人
    st, r = call("DELETE", f"/api/admin/members/{wendy['user']['id']}", token=wendy_token)
    check("普通成员不能移除他人(403)", st == 403, f"{st}")
    # 非法 user id 不应 500
    st, r = call("DELETE", "/api/admin/members/not-a-number", token=alice["token"])
    check("非法 user_id 返回 400 而非 500", st == 400, f"{st}")

    # ========== Bug 2：溯源链路（document_id 返回 + URL 可解析 + 非法 id 不 500） ==========
    print("\n[Bug2] 片段列表返回 document_id，溯源原文接口可用")
    payload, ct = multipart({"visibility": "team"},
                            {"file": (f"src_{suf}.txt",
                                      f"溯源测试文档 {suf}\n\n1 方法\n采用 DLS 测定粒径，标记 SOURCE_{suf}。\n".encode())})
    st, doc = call("POST", "/api/documents", token=wendy_token, raw=payload, ctype=ct)
    check("温蒂上传团队文档", st == 200, doc)
    st, chunks = call("GET", f"/api/documents/{doc['document_id']}/chunks", token=wendy_token)
    check("片段列表每项包含 document_id",
          st == 200 and all("document_id" in c for c in chunks), str(chunks)[:200])
    target = next((c for c in chunks if f"SOURCE_{suf}" in c["content"]), None)
    check("定位含正文标记的片段", target is not None, str([c["content"] for c in chunks]))
    check("document_id 等于所属文档", target and target.get("document_id") == doc["document_id"], str(target))

    st, src = call("GET", f"/api/documents/{target['document_id']}/chunks/{target['chunk_id']}/source",
                   token=wendy_token)
    check("溯源原文接口 200 且精确定位", st == 200 and f"SOURCE_{suf}" in src["exact_text"], str(src)[:200])

    # 前端历史 bug：URL 里出现 undefined -> 现在返回 400 而不是 500
    st, r = call("GET", f"/api/documents/undefined/chunks/{target['chunk_id']}/source", token=wendy_token)
    check("undefined 文档 id 返回 400（不再 500）", st == 400, f"{st} {r}")
    st, r = call("GET", f"/api/documents/{target['document_id']}/chunks/abc/source", token=wendy_token)
    check("非法 chunk id 返回 400（不再 500）", st == 400, f"{st} {r}")

    # ========== Bug 3：有读权限的成员可查看文档详情 ==========
    print("\n[Bug3] 同团队成员可读文档详情（管理权与读权限分离）")
    # 再注册一个同团队成员 xander，对温蒂的文档只有读权限
    xander_email = f"xander_{suf}@lab.cn"
    st, _ = call("POST", "/api/auth/register",
                 data={"email": xander_email, "display_name": "亚历山大", "password": PASS})
    st, r = call("POST", "/api/admin/members", token=alice["token"],
                 data={"email": xander_email, "role": "member",
                       "team": "分子生物学团队", "department": "研发部"})
    st, xander = call("POST", "/api/auth/login",
                      data={"email": xander_email, "password": PASS, "tenant_slug": "biolab"})
    xt = xander["token"]

    # 列表能看到
    st, docs = call("GET", "/api/documents", token=xt)
    ids = {d["id"] for d in docs}
    check("同团队成员列表可见该文档", doc["document_id"] in ids, f"{st}")
    # 详情此前 403，现在应 200
    st, detail = call("GET", f"/api/documents/{doc['document_id']}", token=xt)
    check("同团队成员 GET 文档详情 200", st == 200, f"{st} {detail}")
    check("详情标题正确", detail.get("title") == doc["title"], str(detail)[:150])
    # 片段列表可读
    st, xchunks = call("GET", f"/api/documents/{doc['document_id']}/chunks", token=xt)
    check("同团队成员可读片段列表", st == 200 and len(xchunks) >= 1, f"{st}")
    # 但不能删除 / 改权限（读≠管理）
    st, r = call("DELETE", f"/api/documents/{doc['document_id']}", token=xt)
    check("同团队成员不能删除他人文档(403)", st == 403, f"{st}")
    st, r = call("PUT", f"/api/chunks/{target['chunk_id']}/visibility", token=xt,
                 data={"visibility": "public"})
    check("同团队成员不能改片段权限(403)", st == 403, f"{st}")

    # 无任何关系的跨团队成员（dave：基因治疗团队/临床部）看不到详情
    st, dave = call("POST", "/api/auth/login",
                    data={"email": "dave@lab.cn", "password": "demo123", "tenant_slug": "biolab"})
    if st == 200:
        st, r = call("GET", f"/api/documents/{doc['document_id']}", token=dave["token"])
        check("无权限成员 GET 详情被拒(404)", st == 404, f"{st}")

    # 所有者仍可删除
    st, r = call("DELETE", f"/api/documents/{doc['document_id']}", token=wendy_token)
    check("所有者删除文档成功", st == 200, f"{st} {r}")

    print(f"\n结果：通过 {_passed}，失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
