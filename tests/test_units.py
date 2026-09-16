"""单元测试：清洗、语义分段偏移、BM25 检索、权限谓词。运行：python3 -m unittest -v"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.parsing.text_extract import extract as txt_extract
from app.parsing.cleaner import clean_pages, join_pages
from app.parsing.chunker import build_chunks
from app.core.retrieval import tokenize


class TestCleaner(unittest.TestCase):
    def test_header_footer_removed_body_kept(self):
        bodies = ["第一页正文讨论包封率与粒径。", "第二页正文讨论缓释行为。",
                  "第三页正文讨论动物实验。", "第四页正文讨论中试计划。"]
        raw = "\f".join(
            f"研究报告\n页眉 XYZ\n第{i}页 {b}\nXYZ 公司 第 {i} 页"
            for i, b in enumerate(bodies, 1)
        )
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(raw)
            path = f.name
        cleaned = clean_pages(txt_extract(path))
        full, _ = join_pages(cleaned.pages)
        self.assertNotIn("页眉 XYZ", full)
        self.assertNotIn("XYZ 公司", full)
        for b in bodies:
            self.assertIn(b, full)
        os.unlink(path)


class TestChunker(unittest.TestCase):
    def _chunks(self, text, target=900, limit=1400):
        # 单页
        spans = [(0, len(text), 1, 1)]
        return build_chunks(text, spans, target=target, limit=limit)

    def test_offsets_exact_and_length_bounded(self):
        text = "研究报告\n\n" + "\n\n".join(
            f"{i} 章节{i}\n" + "这是关于纳米颗粒包封率与缓释行为的正文内容。" * 20
            for i in range(1, 6)
        )
        chunks = self._chunks(text)
        self.assertTrue(chunks)
        for c in chunks:
            self.assertEqual(text[c.char_start:c.char_end], c.content)
            self.assertLessEqual(len(c.content), 1400)

    def test_heading_hierarchy(self):
        text = ("1 引言\n引言内容。\n2 方法\n2.1 材料\n材料内容。\n"
                "2.2 制备\n制备内容。\n3 结论\n结论内容。")
        chunks = self._chunks(text, target=50, limit=120)
        paths = {c.heading_path for c in chunks}
        self.assertIn("1 引言", paths)
        self.assertIn("2 方法 / 2.1 材料", paths)
        self.assertIn("2 方法 / 2.2 制备", paths)
        self.assertIn("3 结论", paths)

    def test_oversize_hard_split(self):
        text = "1 大章\n" + "无标点超长内容" * 500
        chunks = self._chunks(text, target=300, limit=500)
        for c in chunks:
            self.assertEqual(text[c.char_start:c.char_end], c.content)
            self.assertLessEqual(len(c.content), 500)


class TestTokenizer(unittest.TestCase):
    def test_chinese_bigrams_and_english(self):
        strong, weak = tokenize("包封率 LPN-207")
        self.assertIn("lpn", strong)
        self.assertIn("207", strong)
        self.assertIn("包封", strong)
        self.assertIn("封率", strong)
        self.assertIn("包", weak)  # 单字在 weak

    def test_underscore_identifier(self):
        # 下划线属于标识符内部，兼容 DONOR_2026_017 这类科研编号
        strong, _ = tokenize("DONOR_2026_017")
        self.assertIn("donor_2026_017", strong)

    def test_single_char_query_falls_back(self):
        strong, weak = tokenize("率")
        self.assertEqual(strong, [])
        self.assertEqual(weak, ["率"])


class TestPermissionPredicate(unittest.TestCase):
    """内存 SQLite 验证三维权限谓词：ACL ∧ clearance ∧ ¬deny ∧ ¬过期。"""

    def setUp(self):
        import sqlite3
        from app.core import permissions
        self.permissions = permissions
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE documents(id INTEGER PRIMARY KEY, tenant_id INT, visibility TEXT,
            classification TEXT NOT NULL DEFAULT 'internal',
            owner_user_id INT, owner_team TEXT, owner_dept TEXT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, document_id INT,
            visibility TEXT, classification TEXT, content TEXT);
        CREATE TABLE chunk_grants(id INTEGER PRIMARY KEY, chunk_id INT,
            subject_type TEXT, subject_value TEXT, effect TEXT DEFAULT 'allow',
            expires_at REAL);
        CREATE TABLE document_rules(id INTEGER PRIMARY KEY, document_id INT,
            subject_type TEXT, subject_value TEXT, effect TEXT DEFAULT 'allow',
            expires_at REAL);
        """)
        # 文档：1 私有 / 2 团队(Alpha) / 3 部门(RD) / 4 公开 / 5 机密公开
        rows = [
            (1, 1, "private", "internal", 1, "Alpha", "RD"),
            (2, 1, "team", "internal", 1, "Alpha", "RD"),
            (3, 1, "department", "internal", 1, "Alpha", "RD"),
            (4, 1, "public", "internal", 1, "Alpha", "RD"),
            (5, 1, "public", "secret", 1, "Alpha", "RD"),
        ]
        self.conn.executemany(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?)", rows)
        cid = 1
        for d in range(1, 5):
            self.conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?)",
                              (cid, d, None, None, f"doc{d} A")); cid += 1
            self.conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?)",
                              (cid, d, None, None, f"doc{d} B")); cid += 1
        # doc5：两个片段，一个继承 secret，一个片段覆盖为 internal
        self.conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?)",
                          (9, 5, None, "secret", "secret chunk"))
        self.conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?)",
                          (10, 5, None, "internal", "downgraded chunk"))
        # chunk 2 (doc1 B) -> team Beta 长期 allow
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (1,2,'team','Beta','allow',NULL)")
        # doc3 chunk5(A) 覆盖为 private
        self.conn.execute("UPDATE chunks SET visibility='private' WHERE id=5")
        self.conn.commit()
        self.now = 1_000_000.0

    def _ids(self, ctx, sql_fn, doc=False):
        where, params = sql_fn(ctx, self.now)
        if doc:
            return {r[0] for r in self.conn.execute(
                f"SELECT d.id FROM documents d WHERE {where}", params)}
        return {r[0] for r in self.conn.execute(
            f"SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id WHERE {where}",
            params)}

    def _chunks(self, ctx):
        return self._ids(ctx, self.permissions.accessible_chunks_where)

    def _docs(self, ctx):
        return self._ids(ctx, self.permissions.accessible_document_where, doc=True)

    def test_owner_sees_all_own(self):
        ctx = {"user_id": 1, "tenant_id": 1, "tenant_role": "member",
               "team": "Alpha", "department": "RD", "clearance": "secret"}
        self.assertEqual(self._chunks(ctx), {1, 2, 3, 4, 5, 6, 7, 8, 9, 10})

    def test_same_team(self):
        ctx = {"user_id": 9, "tenant_id": 1, "tenant_role": "member",
               "team": "Alpha", "department": "Other", "clearance": "internal"}
        ids = self._chunks(ctx)
        self.assertNotIn(1, ids)
        self.assertNotIn(2, ids)
        self.assertIn(3, ids)
        self.assertNotIn(5, ids)
        self.assertIn(7, ids)

    def test_granted_team_sees_single_chunk_only(self):
        ctx = {"user_id": 10, "tenant_id": 1, "tenant_role": "member",
               "team": "Beta", "department": "X", "clearance": "internal"}
        ids = self._chunks(ctx)
        self.assertIn(2, ids)
        self.assertNotIn(1, ids)
        self.assertIn(7, ids)

    def test_department_member(self):
        ctx = {"user_id": 11, "tenant_id": 1, "tenant_role": "member",
               "team": "Other", "department": "RD", "clearance": "internal"}
        ids = self._chunks(ctx)
        self.assertIn(6, ids)
        self.assertNotIn(5, ids)
        self.assertNotIn(3, ids)

    def test_tenant_admin_sees_all_bypass_3d(self):
        # admin 即使 clearance=internal，也旁路密级/deny/时限
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (2,9,'user','99','deny',NULL)")
        self.conn.commit()
        ctx = {"user_id": 99, "tenant_id": 1, "tenant_role": "admin",
               "team": None, "department": None, "clearance": "internal"}
        self.assertEqual(self._chunks(ctx), {1, 2, 3, 4, 5, 6, 7, 8, 9, 10})

    def test_other_tenant_nothing(self):
        ctx = {"user_id": 99, "tenant_id": 2, "tenant_role": "member",
               "team": "Alpha", "department": "RD", "clearance": "secret"}
        self.assertEqual(self._chunks(ctx), set())

    # ---------- 三维新增 ----------
    def test_clearance_blocks_public_secret(self):
        # public + secret：internal/sensitive clearance 都看不到，即使完全公开
        for cl in ("internal", "sensitive"):
            ctx = {"user_id": 20, "tenant_id": 1, "tenant_role": "member",
                   "team": "Z", "department": "Z", "clearance": cl}
            ids = self._chunks(ctx)
            self.assertNotIn(9, ids, f"{cl} 不应看到 secret 片段")
            # 同文档被下调为 internal 的片段可读
            self.assertIn(10, ids, f"{cl} 应能看到 internal 片段")
        ctx_secret = {"user_id": 21, "tenant_id": 1, "tenant_role": "member",
                      "team": "Z", "department": "Z", "clearance": "secret"}
        self.assertIn(9, self._chunks(ctx_secret))

    def test_deny_overrides_grant_and_visibility(self):
        # 给用户 30 授 team Alpha allow，再对 chunk3 显式 deny 该用户
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (10,3,'user','30','allow',NULL)")
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (11,3,'user','30','deny',NULL)")
        self.conn.commit()
        ctx = {"user_id": 30, "tenant_id": 1, "tenant_role": "member",
               "team": "Alpha", "department": "X", "clearance": "secret"}
        ids = self._chunks(ctx)
        self.assertNotIn(3, ids)   # deny 压过 team 可见性 + allow
        self.assertIn(4, ids)     # 同文档另一片段仍可见

    def test_document_level_deny_blocks_all_chunks(self):
        # 文档级 deny team Alpha -> doc2 全部片段对 Alpha 成员不可见
        self.conn.execute(
            "INSERT INTO document_rules VALUES (1,2,'team','Alpha','deny',NULL)")
        self.conn.commit()
        ctx = {"user_id": 31, "tenant_id": 1, "tenant_role": "member",
               "team": "Alpha", "department": "RD", "clearance": "secret"}
        ids = self._chunks(ctx)
        self.assertNotIn(3, ids)
        self.assertNotIn(4, ids)
        self.assertNotIn(2, self._docs(ctx))  # 列表也不出现

    def test_expired_grant_disappears(self):
        # 已过期 allow 不再放行；未过期 allow 放行
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (20,1,'user','40','allow',?)",
            (self.now - 10,))   # 过期
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (21,7,'user','40','allow',?)",
            (self.now + 100,))  # 有效
        self.conn.commit()
        ctx = {"user_id": 40, "tenant_id": 1, "tenant_role": "member",
               "team": "Z", "department": "Z", "clearance": "internal"}
        ids = self._chunks(ctx)
        self.assertNotIn(1, ids)   # 私有片段的授权已过期
        self.assertIn(7, ids)      # 公开片段本就可见（且授权有效）

    def test_expired_deny_no_longer_blocks(self):
        # deny 过期后不再拦截
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (30,7,'user','41','deny',?)",
            (self.now - 10,))
        self.conn.commit()
        ctx = {"user_id": 41, "tenant_id": 1, "tenant_role": "member",
               "team": "Z", "department": "Z", "clearance": "internal"}
        self.assertIn(7, self._chunks(ctx))

    def test_doc_list_consistency_secret_not_exposed(self):
        # internal 用户不应因 doc5 含被降级片段而看到 secret 片段，
        # 但可因 internal 片段(chunk10) 在列表看到文档（点进去只看到该片段）
        ctx = {"user_id": 50, "tenant_id": 1, "tenant_role": "member",
               "team": "Z", "department": "Z", "clearance": "internal"}
        docs = self._docs(ctx)
        self.assertIn(5, docs)
        chunks = self._chunks(ctx)
        self.assertIn(10, chunks)
        self.assertNotIn(9, chunks)

    def test_private_doc_with_only_secret_chunk_not_listed(self):
        # 私有 + secret 文档，internal 被 grant 一个 secret 片段：密级不足，列表不出现
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (40,1,'user','60','allow',NULL)")
        self.conn.execute("UPDATE documents SET classification='secret' WHERE id=1")
        self.conn.commit()
        ctx = {"user_id": 60, "tenant_id": 1, "tenant_role": "member",
               "team": "Z", "department": "Z", "clearance": "internal"}
        self.assertNotIn(1, self._chunks(ctx))
        self.assertNotIn(1, self._docs(ctx))

    def test_all_chunks_denied_no_shell_document(self):
        # public 文档两个片段都对用户 deny：列表/详情谓词不返回（无空壳文档）
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (50,7,'user','70','deny',NULL)")
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (51,8,'user','70','deny',NULL)")
        self.conn.commit()
        ctx = {"user_id": 70, "tenant_id": 1, "tenant_role": "member",
               "team": "Z", "department": "Z", "clearance": "internal"}
        self.assertNotIn(7, self._chunks(ctx))
        self.assertNotIn(8, self._chunks(ctx))
        self.assertNotIn(4, self._docs(ctx))   # doc4 全部片段被 deny -> 不进列表

    def test_one_chunk_denied_other_visible_doc_still_listed(self):
        # public 文档只 deny 其中一个片段：文档仍在列表，且仅可见另一被授权片段
        self.conn.execute(
            "INSERT INTO chunk_grants VALUES (52,7,'user','71','deny',NULL)")
        self.conn.commit()
        ctx = {"user_id": 71, "tenant_id": 1, "tenant_role": "member",
               "team": "Z", "department": "Z", "clearance": "internal"}
        chunks = self._chunks(ctx)
        self.assertNotIn(7, chunks)
        self.assertIn(8, chunks)
        self.assertIn(4, self._docs(ctx))


if __name__ == "__main__":
    unittest.main(verbosity=2)
