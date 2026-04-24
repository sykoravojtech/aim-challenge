"""Retrieval — hybrid filter ∩ ANN, then Phase 4 rerank + MMR diversification."""
from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
from openai import OpenAI

from models.schemas import Aim
from pipeline._util import LLMShapeError, safe_llm_json, strip_markdown_fences
from pipeline.embedding import embed_query
from pipeline.vector_store import query

log = logging.getLogger(__name__)

RETRIEVE_TOP_K = 20
RERANK_MODEL = "gpt-4o-mini"


def build_query_text(aim: Aim) -> str:
    return (
        f"{aim.title} — "
        f"{' '.join(aim.summary)} — "
        f"monitoring: {', '.join(aim.monitored_entities)}"
    )


def build_query_filter(aim: Aim) -> dict[str, Any]:
    # Hybrid retrieval core: regions are Pinecone filter dimensions, not prompt
    # content. "Global" is always OR'd so Global-tagged pieces serve regional
    # Aims (see ARCHITECTURE § Hybrid retrieval).
    return {"region": {"$in": list(aim.regions) + ["Global"]}}


def retrieve_relevant_chunks(
    client: OpenAI, index, aim: Aim, top_k: int = RETRIEVE_TOP_K
) -> list[dict[str, Any]]:
    query_emb = embed_query(client, build_query_text(aim))
    return query(index, query_emb, top_k=top_k, filter=build_query_filter(aim))


def collapse_chunks_by_article(
    chunks: list[dict[str, Any]],
    top_k: int,
    per_article_cap: int = 1,
    sort_key: str = "score",
) -> list[dict[str, Any]]:
    """Collapse chunks to up to `per_article_cap` best-scored chunks per article_id,
    returning the top `top_k` *articles* worth (so max output = top_k * per_article_cap).

    Why: Tier 3 semantic dedup filters `article_id $ne`, so same-URL re-upserts
    across `force` runs accumulate in Pinecone — one article can hold 100+
    near-identical chunks that all crowd the top-k. Without this, a single
    high-similarity article monopolises the candidate pool and legit
    on-Aim sources (regulatory / legislation) get starved at rerank time.
    Surfaced by 6G diagnostic: saas-ai-legislation recall 0/6 traced to
    `OpenAI Workspace Agents` holding 14/30 of the retrieve pool.

    `per_article_cap>1` preserves multi-chunk context for long articles into
    rerank — a 6G regression showed CEE recall dropped from single-chunk
    collapse because rerank saw only a narrow excerpt of long pieces.
    `sort_key="rerank_score"` reuses the same collapse post-rerank to collapse
    to 1-per-article (article-level MMR input).
    """
    by_article: dict[str, list[dict[str, Any]]] = {}
    for c in chunks:
        aid = c.get("article_id")
        if aid is None:
            continue
        by_article.setdefault(aid, []).append(c)

    for aid, cs in by_article.items():
        cs.sort(key=lambda c: -(c.get(sort_key) or 0))
        by_article[aid] = cs[:per_article_cap]

    articles_ordered = sorted(
        by_article.items(),
        key=lambda kv: -(kv[1][0].get(sort_key) or 0),
    )

    out: list[dict[str, Any]] = []
    for _, cs in articles_ordered[:top_k]:
        out.extend(cs)
    return out


_SCORES_INT_RE = __import__("re").compile(r"-?\d+")


def _recover_scores(raw: str, expected_len: int) -> list[int] | None:
    """Best-effort rescue: handle three failure modes the live rerank has hit:
      1. Valid JSON but wrong length — pull the list, pad/trim.
      2. Truncated/runaway JSON (max_tokens hit mid-array) — regex-extract ints.
      3. Markdown fences — strip before parsing.
    Returns None only if we recover <(expected_len - 3) scores; caller falls back."""
    cleaned = strip_markdown_fences(raw)
    arr: list[int] | None = None

    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() == "scores" and isinstance(v, list):
                    arr = [int(x) if isinstance(x, (int, float, str)) and str(x).lstrip("-").isdigit() else 5 for x in v]
                    break
    except Exception:
        pass

    if arr is None:
        # JSON broke — try to regex-extract from the array body. We locate
        # `"scores": [` and pull integers until the first close-bracket or EOF.
        import re
        m = re.search(r'"scores"\s*:\s*\[', cleaned, re.IGNORECASE)
        if m:
            body = cleaned[m.end():]
            end = body.find("]")
            if end != -1:
                body = body[:end]
            arr = [int(x) for x in _SCORES_INT_RE.findall(body)]

    if arr is None or len(arr) < expected_len - 3:
        return None
    if len(arr) > expected_len:
        arr = arr[:expected_len]
    elif len(arr) < expected_len:
        arr = arr + [5] * (expected_len - len(arr))
    return arr


def rerank_chunks(
    client: OpenAI, chunks: list[dict], aim: Aim, top_n: int = 15
) -> list[dict]:
    if len(chunks) <= top_n:
        log.debug("rerank: %d chunks ≤ top_n=%d, skipping LLM call", len(chunks), top_n)
        return chunks

    system = (
        "You are a senior market intelligence analyst. For each retrieved chunk, "
        "assign a relevance score 0-10 for how well it serves the given Aim. "
        "Chunks that obviously do not relate to the Aim's monitored_entities, "
        "regions, or update_types MUST get near-zero scores. Reserve 8-10 for "
        "chunks that directly match the Aim's intent. Respond with a JSON object "
        "exactly as specified in the user message."
    )

    user = {
        "aim": aim.model_dump(mode="json"),
        "chunks": [
            {
                "i": i,
                "title": c["title"],
                "source_type": c["source_type"],
                "region": c["region"],
                "excerpt": (c["text"] or "")[:400],
            }
            for i, c in enumerate(chunks)
        ],
        "instructions": (
            "Return JSON exactly {\"scores\":[int, int, ...]} — one integer 0..10 "
            "per chunk in the same order as provided. Length MUST equal N chunks. "
            "No commentary."
        ),
    }

    resp = client.chat.completions.create(
        model=RERANK_MODEL,
        response_format={"type": "json_object"},
        temperature=0.3,
        # Cap output tightly: ~10 tokens per score + small JSON overhead. This
        # is a hard ceiling that prevents the runaway "line 4096" pathology we
        # saw with larger rerank inputs (6H per_article_cap change).
        max_tokens=min(2000, 200 + 12 * len(chunks)),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
    )
    raw = resp.choices[0].message.content or "{}"

    try:
        scores = safe_llm_json(raw, "scores", expected_len=len(chunks))
    except LLMShapeError as e:
        # Soft recovery: if the LLM dropped a few scores (common with 60+ chunks),
        # pad the tail with neutral=5 rather than discarding the entire rerank.
        # Going to vector order on an under-length-by-1 wipes out the primary
        # quality stage — worse than padding a single unknown.
        scores = _recover_scores(raw, len(chunks))
        if scores is None:
            log.warning("rerank failed, falling back to vector order: %s", e)
            return chunks[:top_n]
        log.warning("rerank recovered with padding: %s", e)

    for i, c in enumerate(chunks):
        try:
            s = int(scores[i])
        except (ValueError, TypeError):
            s = 0
        c["rerank_score"] = max(0, min(10, s))

    # Stable sort by score desc preserves original vector order within ties.
    ordered = sorted(
        enumerate(chunks), key=lambda p: (-p[1]["rerank_score"], p[0])
    )
    kept = [c for _, c in ordered[:top_n]]
    mean_kept = sum(c["rerank_score"] for c in kept) / len(kept) if kept else 0.0
    log.info("rerank: %d → top %d (mean=%.2f)", len(chunks), top_n, mean_kept)
    return kept


def mmr_diversify(
    chunks: list[dict],
    query_embedding: list[float],
    top_k: int = 10,
    lambda_: float = 0.7,
) -> list[dict]:
    if len(chunks) <= top_k:
        return chunks

    if any("embedding" not in c or c["embedding"] is None for c in chunks):
        log.warning("mmr_diversify: missing embeddings on some chunks, returning top_k in input order")
        return chunks[:top_k]

    def _norm(v: list[float]) -> np.ndarray:
        arr = np.asarray(v, dtype=np.float32)
        n = np.linalg.norm(arr)
        # Guard: zero-norm vector would NaN the cosine; fall back to raw.
        return arr / n if n > 0 else arr

    chunk_vecs = [_norm(c["embedding"]) for c in chunks]
    query_vec = _norm(query_embedding)
    sim_q = np.array([float(np.dot(cv, query_vec)) for cv in chunk_vecs])

    selected: list[int] = []
    remaining = set(range(len(chunks)))

    while remaining and len(selected) < top_k:
        best_idx = -1
        best_score = -float("inf")
        for i in sorted(remaining):
            # max over empty selected set = 0 (MMR convention for round 1).
            if selected:
                max_sim_sel = max(
                    float(np.dot(chunk_vecs[i], chunk_vecs[s])) for s in selected
                )
            else:
                max_sim_sel = 0.0
            score = lambda_ * sim_q[i] - (1.0 - lambda_) * max_sim_sel
            if score > best_score:
                best_score = score
                best_idx = i
        selected.append(best_idx)
        remaining.remove(best_idx)

    picked = []
    for rank, idx in enumerate(selected):
        chunks[idx]["mmr_rank"] = rank
        picked.append(chunks[idx])
    return picked
