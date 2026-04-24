"""Retrieval — hybrid filter ∩ ANN, then Phase 4 rerank + MMR diversification."""
from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
from openai import OpenAI

from models.schemas import Aim
from pipeline._util import LLMShapeError, safe_llm_json
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
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
    )
    raw = resp.choices[0].message.content or "{}"

    try:
        scores = safe_llm_json(raw, "scores", expected_len=len(chunks))
    except LLMShapeError as e:
        # Fallback: vector order is a sane prior when the reranker misbehaves.
        log.warning("rerank failed, falling back to vector order: %s", e)
        return chunks[:top_n]

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
