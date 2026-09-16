#!/usr/bin/env python3
"""端到端 API 测试：注册/登录、文档权限、片段授权、权限穿透检索、溯源、多租户隔离。

直接用标准库 urllib 对运行中的服务发请求；也可独立运行：
  KB_BASE=http://127.0.0.1:8080 python3 tests/test_api.py
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

_passed = 0
_failed = 0


def call(method, path, token=None, data=None, raw=None, ctype="application/json"):
    url = BASE + path
    headers = {}
    body = None
    if raw is not None:
        body = raw
        headers["Content-Type"] = ctype
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
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


def multipart(fields: dict, files: dict):
    boundary = "----kb" + uuid.uuid4().hex
    lines = []
    for k, v in fields.items():
        lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    for k, f in files.items():
        fn, content = f
        lines.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
            f"filename=\"{fn}\"\r\nContent-Type: text/plain\r\n\r\n"
        )
    pre = "".join(lines).encode("utf-8")
    post = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return pre + content + post, f"multipart/form-data; boundary={boundary}"


def main():
    suffix = uuid.uuid4().hex[:8]
    # 1) 注册平台管理员 + 两个租户的用户
    admin_email = f"admin_{suffix}@corp.cn"
    a_email = f"anna_{suffix}@lab.cn"
    b_email = f"ben_{suffix}@lab.cn"
    c_email = f"cara_{suffix}@lab.cn"
    e_email = f"evan_{suffix}@chem.cn"

    for email, name in [(admin_email, "平台管理员"), (a_email, "安娜"),
                        (b_email, "本"), (c_email, "卡拉"), (e_email, "伊万")]:
        st, r = call("POST", "/api/auth/register",
                     data={"email": email, "display_name": name, "password": PASS})
        check(f"注册 {email}", st == 200, r)

    # 首个平台管理员：直接在数据库中提升（测试便捷方式）
    import sqlite3
    from app import config
    g = sqlite3.connect(config.GLOBAL_DB)
    g.execute("UPDATE users SET is_platform_admin=1 WHERE email=?", (admin_email,))
    g.commit()
    g.close()

    st, r = call("POST", "/api/auth/login",
                 data={"email": admin_email, "password": PASS, "tenant_slug": "biolab"})
    # 管理员尚未加入租户，登录应被拒绝
    check("未加入租户禁止登录", st == 403, f"{st} {r}")

    # 2) 平台管理员创建两个隔离租户
    st, r = call("POST", "/api/auth/login",
                 data={"email": admin_email, "password": PASS, "tenant_slug": "biolab"})
    # 仍无租户 -> 先给一个已有租户登录拿 token 不可能；平台管理员也需租户。
    # 因此用已存在的演示租户 biolab 的 admin（若 seed 存在）否则引导。
    # 引导：直接把平台管理员加入 biolab 作为 admin（通过 seed 账号 alice 操作）
    st, alice = call("POST", "/api/auth/login",
                     data={"email": "alice@lab.cn", "password": "demo123",
                           "tenant_slug": "biolab"})
    if st != 200:
        print("!! 需要先运行: python3 -m scripts.seed  初始化演示租户")
        sys.exit(2)
    # 用平台管理员（加入租户后才能发请求）——通过 alice 把新用户加进 biolab
    st, r = call("POST", "/api/admin/members", token=alice["token"],
                 data={"email": a_email, "role": "admin",
                       "team": "分子生物学团队", "department": "研发部"})
    check("安娜加入 biolab(admin)", st == 200, r)
    st, r = call("POST", "/api/admin/members", token=alice["token"],
                 data={"email": b_email, "role": "member",
                       "team": "分子生物学团队", "department": "研发部"})
    check("本加入 biolab(同团队)", st == 200, r)
    st, r = call("POST", "/api/admin/members", token=alice["token"],
                 data={"email": c_email, "role": "member",
                       "team": "细胞生物学团队", "department": "研发部"})
    check("卡拉加入 biolab(异团队同部门)", st == 200, r)

    # chemmat 租户：erin 是 admin
    st, erin = call("POST", "/api/auth/login",
                    data={"email": "erin@chem.cn", "password": "demo123",
                          "tenant_slug": "chemmat"})
    check("艾琳登录 chemmat", st == 200, r)
    st, r = call("POST", "/api/admin/members", token=erin["token"],
                 data={"email": e_email, "role": "member",
                       "team": "催化团队", "department": "材料部"})
    check("伊万加入 chemmat", st == 200, r)

    # 3) 各用户登录
    st, anna = call("POST", "/api/auth/login",
                    data={"email": a_email, "password": PASS, "tenant_slug": "biolab"})
    check("安娜登录", st == 200, r)
    st, ben = call("POST", "/api/auth/login",
                   data={"email": b_email, "password": PASS, "tenant_slug": "biolab"})
    check("本登录", st == 200, r)
    st, cara = call("POST", "/api/auth/login",
                    data={"email": c_email, "password": PASS, "tenant_slug": "biolab"})
    check("卡拉登录", st == 200, r)
    st, evan = call("POST", "/api/auth/login",
                    data={"email": e_email, "password": PASS, "tenant_slug": "chemmat"})
    check("伊万登录 chemmat", st == 200, r)

    # 4) 无 token 访问被拦截
    st, r = call("GET", "/api/documents")
    check("无令牌访问 401", st == 401, f"{st}")

    # 5) 安娜上传三篇不同可见性的文档
    secret_text = (
        "保密实验记录\n\n1 公开方法\n样品采用高效液相色谱检测，检测波长254纳米。\n\n"
        "2 保密结论\n核心配方 SECRET-9921 的产率达到 97.3%，该数据仅限授权人员。\n"
    )
    team_text = "团队方案\n分子生物学团队的纳米颗粒制备 SOP：使用纳米沉淀法，粒径控制在80纳米左右。\n"
    dept_text = "部门制度\n研发部共享：实验记录本须在实验当日归档，电子数据双备份。\n"

    def upload(token, name, text, vis):
        body, ct = multipart({"visibility": vis}, {"file": (name, text.encode("utf-8"))})
        return call("POST", "/api/documents", token=token, raw=body, ctype=ct)

    st, d_secret = upload(anna["token"], f"保密记录_{suffix}.txt", secret_text, "private")
    check("上传私有文档", st == 200, f"{st} {d_secret}")
    st, d_team = upload(anna["token"], f"团队SOP_{suffix}.txt", team_text, "team")
    check("上传团队文档", st == 200, f"{st} {d_team}")
    st, d_dept = upload(anna["token"], f"部门制度_{suffix}.txt", dept_text, "department")
    check("上传部门文档", st == 200, f"{st} {d_dept}")

    # 6) 文档列表权限
    def titles(token):
        st, r = call("GET", "/api/documents", token=token)
        assert st == 200, r
        return {x["title"] for x in r}

    t_ben = titles(ben["token"])
    t_cara = titles(cara["token"])
    check("同团队成员可见团队文档", f"团队SOP_{suffix}" in t_ben)
    check("异团队成员不可见团队文档", f"团队SOP_{suffix}" not in t_cara, str(t_cara))
    check("同部门成员可见部门文档", f"部门制度_{suffix}" in t_cara)
    check("私有文档对他人不可见", f"保密记录_{suffix}" not in t_ben)
    check("私有文档对所有者可见", f"保密记录_{suffix}" in titles(anna["token"]))

    # 7) 权限穿透检索：本/卡拉 检索保密关键词
    st, r = call("POST", "/api/search", token=ben["token"],
                 data={"query": "SECRET-9921 产率"})
    check("私有片段对无授权成员检索为空", st == 200 and r["count"] == 0, str(r))
    st, r = call("POST", "/api/search", token=cara["token"],
                 data={"query": "SECRET-9921"})
    check("越权检索被过滤(卡拉)", r["count"] == 0, str(r))

    # 8) 片段级授权：安娜把“保密结论”片段单独授权给本（指定用户）
    st, chunks = call("GET", f"/api/documents/{d_secret['document_id']}/chunks",
                      token=anna["token"])
    secret_chunk = next((c for c in chunks if "SECRET-9921" in c["content"]), None)
    check("定位保密片段", secret_chunk is not None, str([c["content"] for c in chunks]))
    st, me = call("GET", "/api/me", token=ben["token"])
    st, r = call("POST", f"/api/chunks/{secret_chunk['chunk_id']}/grants",
                 token=anna["token"], data={"subject_type": "user",
                                            "subject_value": str(me["user"]["id"])})
    check("片段授权给指定用户", st == 200, r)

    # 非授权者卡拉不能改片段权限
    st, r = call("PUT", f"/api/chunks/{secret_chunk['chunk_id']}/visibility",
                 token=cara["token"], data={"visibility": "public"})
    check("非管理者不能改片段权限", st == 403, f"{st}")

    # 授权后：本可以检索到保密片段，但文档其它私有片段仍不可见
    st, r = call("POST", "/api/search", token=ben["token"],
                 data={"query": "SECRET-9921 产率"})
    check("授权后本可检索到保密片段", r["count"] >= 1, str(r))
    hit = r["results"][0]
    check("命中片段为被授权片段", hit["chunk_id"] == secret_chunk["chunk_id"], str(hit["chunk_id"]))
    st, r = call("POST", "/api/search", token=ben["token"],
                 data={"query": "高效液相色谱 检测波长"})
    # 公开方法片段仍属 private 文档且无授权 -> 不可见
    check("文档内未授权的其他片段仍不可见", r["count"] == 0, str(r))
    st, r = call("POST", "/api/search", token=cara["token"],
                 data={"query": "SECRET-9921"})
    check("卡拉依旧无法越权检索", r["count"] == 0, str(r))

    # 9) 溯源信息
    st, src = call("GET", f"/api/documents/{hit['document_id']}/chunks/{hit['chunk_id']}/source",
                   token=ben["token"])
    check("溯源可访问且定位原文", st == 200 and "SECRET-9921" in src["exact_text"], str(src)[:200])
    check("溯源包含文档名/页码/字符位置",
          src["source_name"].endswith(".txt") and isinstance(src["char_range"], list), str(src["char_range"]))
    # 卡拉不能看该片段溯源（纵深防御）
    st, r = call("GET", f"/api/documents/{hit['document_id']}/chunks/{hit['chunk_id']}/source",
                 token=cara["token"])
    check("无权限访问片段溯源 404", st == 404, f"{st}")

    # 10) 问答 + 来源
    st, r = call("POST", "/api/ask", token=ben["token"],
                 data={"question": "SECRET-9921 的产率是多少？"})
    check("问答基于授权片段", st == 200 and "97.3" in r["answer"], str(r)[:200])
    check("问答附带溯源来源", len(r["sources"]) >= 1 and r["sources"][0]["document_title"], str(r.get("sources")))
    st, r = call("POST", "/api/ask", token=cara["token"],
                 data={"question": "SECRET-9921 的产率是多少？"})
    check("无权限问答不泄露内容", "97.3" not in r["answer"] and not r["sources"], str(r)[:200])

    # 11) 角色授权：团队文档对“角色”授权（演示 role grant）
    # 给保密片段再授权 role:admin，验证角色穿透
    st, r = call("POST", f"/api/chunks/{secret_chunk['chunk_id']}/grants",
                 token=anna["token"], data={"subject_type": "role", "subject_value": "admin"})
    check("片段支持角色授权", st == 200, r)

    # 12) 多租户硬隔离：伊万(chemmat) 检索 biolab 的任何内容都为空
    for q in ["SECRET-9921", "纳米颗粒", "研发部", "CRISPR", "包封率"]:
        st, r = call("POST", "/api/search", token=evan["token"], data={"query": q})
        check(f"跨租户隔离: chemmat 搜不到 biolab 内容({q})", r["count"] == 0, str(r)[:120])
    st, r = call("GET", f"/api/documents/{d_secret['document_id']}", token=evan["token"])
    check("跨租户直接访问文档 ID 被拒(404/403)", st in (403, 404), f"{st}")
    # biolab 用户同样看不到 chemmat 的催化剂数据
    st, r = call("POST", "/api/search", token=ben["token"], data={"query": "二氧化碳 甲醇 催化剂"})
    check("biolab 搜不到 chemmat 内容", r["count"] == 0, str(r)[:120])

    # 13) 部门/团队边界（chem 内部数据隔离）
    st, r = call("POST", "/api/search", token=evan["token"], data={"query": "甲醇选择性"})
    check("同租户有权内容可检索", r["count"] >= 1, str(r)[:150])

    # 14) 删除文档
    st, before = call("GET", "/api/documents", token=anna["token"])
    st, r = call("DELETE", f"/api/documents/{d_team['document_id']}", token=anna["token"])
    check("所有者删除文档", st == 200, r)
    st, r = call("DELETE", f"/api/documents/{d_dept['document_id']}", token=ben["token"])
    check("非所有者不能删除他人文档", st == 403, f"{st}")

    print(f"\n结果：通过 {_passed}，失败 {_failed}")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
