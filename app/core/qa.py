"""问答层（需求 6）。

两种模式：
  - 内置抽取式（默认，离线、无密钥）：对检索命中片段做查询覆盖度评分，
    抽取最相关的 1~3 句组成答案，并强制返回原文溯源；
  - LLM 生成式（可选）：配置 LLM_BASE_URL / LLM_API_KEY 后，将检索到的
    「用户有权访问」片段作为唯一上下文，要求模型只能依据上下文作答并引用来源；
    上下文在进入模型前同样经过权限过滤，杜绝越权内容泄露给模型。

无论哪种模式，返回值都携带 sources（文档名、章节、片段序号、页码、字符位置）。
"""
from __future__ import annotations

import json
import re

from .. import config
from . import permissions
from .retrieval import search_index, tokenize


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _extractive_answer(query: str, hits: list[dict]) -> tuple[str, list[dict]]:
    q_strong, q_weak = tokenize(query)
    q_terms = set(q_strong if q_strong else q_weak)
    scored: list[tuple[float, int, str, dict]] = []
    used_hits: set[int] = set()
    for hit in hits:
        for sent in _split_sentences(hit["content"]):
            s_strong, s_weak = tokenize(sent)
            s_terms = set(s_strong if s_strong else s_weak)
            if not s_terms:
                continue
            overlap = len(q_terms & s_terms)
            if overlap == 0:
                continue
            # 词覆盖比例 + BM25 文档分，归一化句长
            density = overlap / (len(s_terms) ** 0.5)
            score = density * (1 + hit["score"])
            scored.append((score, len(scored), sent, hit))
    scored.sort(key=lambda x: x[0], reverse=True)

    chosen = scored[:3]
    chosen.sort(key=lambda x: x[1])  # 尽量保持原文顺序，便于阅读
    if not chosen:
        # 没有句子命中查询词：退化为最高分片段摘要
        top = hits[:1]
        sents = _split_sentences(top[0]["content"])[:2] if top else []
        answer = " ".join(sents)
        used = top
    else:
        answer = " ".join(s for _, _, s, _ in chosen)
        used = []
        seen = set()
        for _, _, _, hit in chosen:
            if hit["chunk_id"] not in seen:
                used.append(hit)
                seen.add(hit["chunk_id"])

    if not answer:
        answer = "根据您当前可访问的知识库内容，未检索到与问题直接相关的信息。"
    return answer, used


def _sources(hits: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for h in hits:
        if h["chunk_id"] in seen:
            continue
        seen.add(h["chunk_id"])
        page = h.get("page_start")
        page_desc = f"第 {page} 页" if page else "—"
        if h.get("page_end") and h.get("page_end") != page:
            page_desc = f"第 {page}-{h['page_end']} 页"
        out.append({
            "document_title": h["title"],
            "source_file": h["source_name"],
            "section": h.get("heading_path"),
            "chunk_index": h["chunk_index"] + 1,
            "location": page_desc,
            "char_range": [h["char_start"], h["char_end"]],
            "score": h["score"],
        })
    return out


# ---------------- 可选 LLM ----------------

def _llm_answer(query: str, hits: list[dict]) -> str | None:
    if not (config.LLM_BASE_URL and config.LLM_API_KEY):
        return None
    try:
        import urllib.request
        import urllib.error

        context_blocks = []
        for i, h in enumerate(hits, start=1):
            page = f"（{h.get('page_start')}页）" if h.get("page_start") else ""
            sec = f"[{h.get('heading_path')}]" if h.get("heading_path") else ""
            context_blocks.append(
                f"[来源{i}] 《{h['title']}》{sec}{page}\n{h['content']}"
            )
        context = "\n\n".join(context_blocks)
        prompt = (
            "你是严谨的科研知识库助手。只能依据下面提供的私有资料原文回答问题，"
            "不得编造资料中不存在的结论；资料不足时明确说明。"
            "在回答中用 [来源n] 标注依据。\n\n"
            f"私有资料：\n{context}\n\n问题：{query}\n\n回答："
        )
        payload = {
            "model": config.LLM_MODEL,
            "messages": [
                {"role": "system", "content": "你是严谨的科研知识库助手。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        req = urllib.request.Request(
            config.LLM_BASE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {config.LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # LLM 调用失败：返回 None，由上层降级到抽取式
        print(f"[qa] LLM 调用失败，降级为抽取式问答: {exc}")
        return None


def answer_question(tenant_slug: str, conn, ctx: dict, question: str,
                    top_k: int | None = None) -> dict:
    # 第一次实时过滤：检索命中后立即按“当前”权限复核（检索与本调用之间可能已被 deny）
    raw_hits = search_index.search(tenant_slug, conn, ctx, question, top_k=top_k)
    hits = [h for h in raw_hits
            if h["chunk_id"] in permissions.filter_accessible_chunk_ids(
                conn, ctx, [h["chunk_id"] for h in raw_hits])]

    answer = None
    used_hits = hits
    used_llm = False
    llm_configured = bool(hits and config.LLM_BASE_URL and config.LLM_API_KEY)

    if llm_configured:
        llm_text = _llm_answer(question, hits)
        if llm_text and llm_text.strip():
            # 第二次实时复核：LLM 较慢，返回期间片段可能已被 deny/降密/过期。
            # 若喂给模型的任一来源已不可访问，则本次生成结果不可信，丢弃并降级为抽取式。
            allowed_now = permissions.filter_accessible_chunk_ids(
                conn, ctx, [h["chunk_id"] for h in hits])
            still_ok = [h for h in hits if h["chunk_id"] in allowed_now]
            if len(still_ok) == len(hits):
                answer = llm_text.strip()
                used_llm = True
                used_hits = still_ok
            else:
                # 有权限变化：只用仍可访问的片段重做抽取式答案，避免泄露已 deny 内容
                hits = still_ok

    if answer is None:
        if hits:
            answer, used_hits = _extractive_answer(question, hits)
        else:
            answer = "根据您当前可访问的知识库内容，未检索到与问题相关的文档片段。"
            used_hits = []

    # 返回前最终复核：抽取式也可能在执行间隙被 deny；来源只保留此刻仍可访问的片段
    final_candidates = used_hits if used_hits else hits
    final_allowed = permissions.filter_accessible_chunk_ids(
        conn, ctx, [h["chunk_id"] for h in final_candidates])
    final_hits = [h for h in final_candidates if h["chunk_id"] in final_allowed]

    # 若答案引用的句子来自已失效片段，无法逐句裁剪时，整体退化为“无相关信息”
    if not used_llm and not final_hits and raw_hits:
        answer = "根据您当前可访问的知识库内容，未检索到与问题相关的文档片段。"

    return {
        "question": question,
        "answer": answer,
        # 配置了 LLM 但实际降级时，明确标注为 extractive（前端不再误显示“LLM 生成”）
        "mode": "llm" if used_llm else "extractive",
        "llm_configured": bool(raw_hits and config.LLM_BASE_URL and config.LLM_API_KEY),
        "degraded": bool(raw_hits and config.LLM_BASE_URL and config.LLM_API_KEY) and not used_llm,
        "sources": _sources(final_hits),
    }
