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

    def test_single_char_query_falls_back(self):
        strong, weak = tokenize("率")
        self.assertEqual(strong, [])
        self.assertEqual(weak, ["率"])


class TestPermissionPredicate(unittest.TestCase):
    """用内存 SQLite 构造 文档/片段/授权，验证权限谓词的过滤结果。"""

    def setUp(self):
        import sqlite3
        from app.core import permissions
        self.permissions = permissions
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        CREATE TABLE documents(id INTEGER PRIMARY KEY, tenant_id INT, visibility TEXT,
            owner_user_id INT, owner_team TEXT, owner_dept TEXT);
        CREATE TABLE chunks(id INTEGER PRIMARY KEY, document_id INT, visibility TEXT, content TEXT);
        CREATE TABLE chunk_grants(id INTEGER PRIMARY KEY, chunk_id INT,
            subject_type TEXT, subject_value TEXT);
        """)
        # 文档：1 私有 / 2 团队(Alpha) / 3 部门(RD) / 4 公开
        rows = [
            (1, 1, "private", 1, "Alpha", "RD"),
            (2, 1, "team", 1, "Alpha", "RD"),
            (3, 1, "department", 1, "Alpha", "RD"),
            (4, 1, "public", 1, "Alpha", "RD"),
        ]
        self.conn.executemany(
            "INSERT INTO documents VALUES (?,?,?,?,?,?)", rows)
        # 每文档 2 片段；doc1 的 chunk2 单独授权给 Beta 团队
        cid = 1
        for d in range(1, 5):
            self.conn.execute("INSERT INTO chunks VALUES (?,?,?,?)",
                              (cid, d, None, f"doc{d} chunk A"))
            cid += 1
            self.conn.execute("INSERT INTO chunks VALUES (?,?,?,?)",
                              (cid, d, None, f"doc{d} chunk B"))
            cid += 1
        # chunk 2 (doc1 B) -> team Beta
        self.conn.execute("INSERT INTO chunk_grants VALUES (1,2,'team','Beta')")
        # chunk 5 (doc3 A) 覆盖为 private
        self.conn.execute("UPDATE chunks SET visibility='private' WHERE id=5")
        self.conn.commit()

    def _visible_chunk_ids(self, ctx):
        where, params = self.permissions.accessible_chunks_where(ctx)
        return {r[0] for r in self.conn.execute(
            f"SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id WHERE {where}",
            params)}

    def test_owner_sees_all_own(self):
        ctx = {"user_id": 1, "tenant_id": 1, "tenant_role": "member",
               "team": "Alpha", "department": "RD"}
        ids = self._visible_chunk_ids(ctx)
        self.assertEqual(ids, {1, 2, 3, 4, 5, 6, 7, 8})

    def test_same_team(self):
        ctx = {"user_id": 9, "tenant_id": 1, "tenant_role": "member",
               "team": "Alpha", "department": "Other"}
        ids = self._visible_chunk_ids(ctx)
        # doc1 私有不可见(1)，但 chunk2 被 grant 给 Beta——Alpha 仍不可见
        self.assertNotIn(1, ids)
        self.assertNotIn(2, ids)
        self.assertIn(3, ids)   # team Alpha
        self.assertNotIn(5, ids)  # doc3 chunk5 被覆盖为 private -> 不可见
        self.assertIn(7, ids)   # public

    def test_granted_team_sees_single_chunk_only(self):
        ctx = {"user_id": 10, "tenant_id": 1, "tenant_role": "member",
               "team": "Beta", "department": "X"}
        ids = self._visible_chunk_ids(ctx)
        self.assertIn(2, ids)      # 被授权的单个片段
        self.assertNotIn(1, ids)   # 同文档其它片段仍不可见
        self.assertIn(7, ids)      # 公开内容

    def test_department_member(self):
        ctx = {"user_id": 11, "tenant_id": 1, "tenant_role": "member",
               "team": "Other", "department": "RD"}
        ids = self._visible_chunk_ids(ctx)
        self.assertIn(6, ids)    # doc3 chunk B 部门可见
        self.assertNotIn(5, ids)  # doc3 chunk A 被覆盖为 private
        self.assertNotIn(3, ids)  # Alpha 团队文档不可见

    def test_tenant_admin_sees_all(self):
        ctx = {"user_id": 99, "tenant_id": 1, "tenant_role": "admin",
               "team": None, "department": None}
        ids = self._visible_chunk_ids(ctx)
        self.assertEqual(ids, {1, 2, 3, 4, 5, 6, 7, 8})

    def test_other_tenant_nothing(self):
        ctx = {"user_id": 99, "tenant_id": 2, "tenant_role": "member",
               "team": "Alpha", "department": "RD"}
        ids = self._visible_chunk_ids(ctx)
        self.assertEqual(ids, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
