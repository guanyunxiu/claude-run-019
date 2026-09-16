"""检索层：中文友好分词 + BM25 排序 + 权限穿透过滤（需求 4）。

流程（权限永远先于内容展示）：
  1. 每个租户构建覆盖「全部片段」的 BM25 索引，按文档版本指纹缓存，
     文档新增/删除/权限变更使指纹变化、索引自动重建；
  2. 打分阶段用「可访问片段」SQL 谓词得到允许集合，只对允许集合内片段累计分数，
     无权限片段的分数始终为 0、物理上不可能进入结果；
  3. 取回结果时再次带权限谓词查询（纵深防御）。

索引不含任何跨租户数据：缓存键是 tenant_slug，数据来自该租户独立库文件。
"""
from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass

from .. import config
from . import permissions

# 字母数字与下划线视为同一标识符（兼容 DONOR_2026_017、BE4max 等科研编号）；
# 连字符仍作为分隔（LPN-207 -> lpn / 207）。
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")


def tokenize(text: str) -> tuple[list[str], list[str]]:
    """英文/数字按词；中文按单字并叠加双字组（bigram）。

    返回 (strong, weak)：strong=英文词/数字/中文双字组（区分度高），
    weak=中文单字（仅在查询没有 strong 词元时兜底，避免“率”命中“转化率”这类噪声）。
    """
    base = _TOKEN_RE.findall(text.lower())
    cn = [t for t in base if "一" <= t <= "鿿"]
    other = [t for t in base if not ("一" <= t <= "鿿")]
    bigrams = [cn[i] + cn[i + 1] for i in range(len(cn) - 1)]
    strong = list(dict.fromkeys(other + bigrams))
    weak = list(dict.fromkeys(cn))
    return strong, weak


@dataclass
class _Index:
    version_fp: tuple
    n_docs: int
    avgdl: float
    df: dict[str, int]
    postings: dict[str, dict[int, int]]   # term -> {chunk_id: tf}
    lengths: dict[int, int]


class TenantSearchIndex:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache: dict[str, _Index] = {}

    @staticmethod
    def _version_fingerprint(conn) -> tuple:
        row = conn.execute(
            """SELECT
                 (SELECT COALESCE(SUM(index_version),0) FROM documents) AS dv,
                 (SELECT COUNT(*) FROM documents) AS dc,
                 (SELECT COALESCE(SUM(index_version),0) FROM chunks) AS cv,
                 (SELECT COUNT(*) FROM chunk_grants) AS gc"""
        ).fetchone()
        return tuple(row)

    def _get_index(self, tenant_slug: str, conn) -> _Index:
        fp = self._version_fingerprint(conn)
        with self._lock:
            cached = self._cache.get(tenant_slug)
            if cached is not None and cached.version_fp == fp:
                return cached
            rows = conn.execute(
                "SELECT id, content FROM chunks"
            ).fetchall()
            lengths: dict[int, int] = {}
            postings: dict[str, dict[int, int]] = {}
            df: dict[str, int] = {}
            for r in rows:
                cid = r["id"]
                strong, weak = tokenize(r["content"])
                toks = strong + weak
                lengths[cid] = len(toks)
                tf_map: dict[str, int] = {}
                for t in toks:
                    tf_map[t] = tf_map.get(t, 0) + 1
                for term, tf in tf_map.items():
                    postings.setdefault(term, {})[cid] = tf
                    df[term] = df.get(term, 0) + 1
            n_docs = len(rows)
            avgdl = (sum(lengths.values()) / n_docs) if n_docs else 0.0
            idx = _Index(fp, n_docs, avgdl, df, postings, lengths)
            self._cache[tenant_slug] = idx
            return idx

    def search(self, tenant_slug: str, conn, ctx: dict, query: str,
               top_k: int | None = None) -> list[dict]:
        top_k = top_k or config.SEARCH_TOP_K
        q_strong, q_weak = tokenize(query)
        # 优先用强词元（英文词/中文双字组）；查询本身是单字时才退回单字
        q_terms = q_strong if q_strong else q_weak
        if not q_terms:
            return []

        # 第一步：权限——得到当前用户可访问的全部片段 id 白名单
        where, params = permissions.accessible_chunks_where(ctx)
        allowed = {
            r["id"] for r in conn.execute(
                f"""SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id
                    WHERE {where}""",
                params,
            ).fetchall()
        }
        if not allowed:
            return []

        idx = self._get_index(tenant_slug, conn)

        # 第二步：仅在白名单内累计 BM25 分数
        k1, b = config.BM25_K1, config.BM25_B
        scores: dict[int, float] = {}
        matched_terms: dict[int, set] = {}
        # 在「整个租户语料」中存在的查询词（用于覆盖率归一化，不含权限信息泄露）
        corpus_terms = {t for t in q_terms if t in idx.postings}
        for term in q_terms:
            post = idx.postings.get(term)
            if not post:
                continue
            df = idx.df.get(term, 0)
            idf = math.log(1 + (idx.n_docs - df + 0.5) / (df + 0.5))
            for cid, tf in post.items():
                if cid not in allowed:
                    continue  # 越权片段直接跳过
                dl = idx.lengths[cid]
                denom = tf + k1 * (1 - b + b * dl / (idx.avgdl or 1))
                scores[cid] = scores.get(cid, 0.0) + idf * (tf * (k1 + 1)) / denom
                matched_terms.setdefault(cid, set()).add(term)

        if not scores:
            return []

        # 查询词覆盖度软因子：只命中“通用词”、漏掉全部稀有词的片段被降权。
        # 例如查 “DONOR-2026-017” 时，仅含 “2026” 的片段覆盖率低、排名靠后；
        # 该因子只影响排序，不改变权限白名单（安全边界不受影响）。
        if len(corpus_terms) >= 2:
            n_terms = len(corpus_terms)
            for cid in list(scores.keys()):
                coverage = len(matched_terms.get(cid, set()) & corpus_terms) / n_terms
                scores[cid] *= 0.35 + 0.65 * coverage

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        ids = [cid for cid, _ in ranked]
        score_map = dict(ranked)

        # 第三步：取回展示数据，再次叠加权限谓词（纵深防御）
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT c.id, c.document_id, c.chunk_index, c.heading_path, c.content,
                       c.char_start, c.char_end, c.page_start, c.page_end,
                       d.title, d.source_name
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.id IN ({placeholders}) AND {where}""",
            ids + params,
        ).fetchall()
        results = []
        for r in rows:
            results.append({
                "chunk_id": r["id"],
                "document_id": r["document_id"],
                "title": r["title"],
                "source_name": r["source_name"],
                "chunk_index": r["chunk_index"],
                "heading_path": r["heading_path"],
                "content": r["content"],
                "char_start": r["char_start"],
                "char_end": r["char_end"],
                "page_start": r["page_start"],
                "page_end": r["page_end"],
                "score": round(score_map[r["id"]], 4),
            })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results


# 进程内单例
search_index = TenantSearchIndex()
