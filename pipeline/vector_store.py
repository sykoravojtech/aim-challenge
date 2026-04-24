"""Pinecone wrapper — upsert + query. Swap path: BigQuery VECTOR_SEARCH."""
from __future__ import annotations

import logging
import os
from typing import Any

from pinecone import Pinecone

log = logging.getLogger(__name__)

PINECONE_INDEX = os.environ.get("PINECONE_INDEX", "aim-chunks")

# Tier 3 semantic-dedup threshold (see DECISIONS D13). Cosine ≥0.93 on a
# different-article chunk means the content is a near-duplicate rewrite —
# catches "AP story rewritten by Reuters + WSJ with same facts".
SEMANTIC_DUP_THRESHOLD = 0.93

_pc: Pinecone | None = None
_index = None


def get_index():
    """Lazy-init Pinecone client + index handle. One per process."""
    global _pc, _index
    if _index is None:
        _pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        _index = _pc.Index(PINECONE_INDEX)
    return _index


def _is_semantic_dup(index, embedding: list[float], article_id: str) -> tuple[bool, dict[str, Any] | None]:
    """Tier 3: query top_k=1 with article_id $ne — hit above threshold on a
    different article = near-duplicate rewrite. Eventual-consistency caveat:
    same-batch chunks may not be visible yet, which is fine because
    within-article dedup is guaranteed by chunk_index uniqueness anyway."""
    try:
        res = index.query(
            vector=embedding,
            top_k=1,
            filter={"article_id": {"$ne": article_id}},
            include_metadata=True,
        )
    except Exception as e:
        # Never let a dedup query failure block an upsert — fail-open on this path.
        log.warning("semantic-dedup query failed for article %s: %s", article_id, e)
        return False, None
    matches = res.get("matches", []) or []
    if not matches:
        return False, None
    top = matches[0]
    if (top.get("score") or 0) >= SEMANTIC_DUP_THRESHOLD:
        return True, top
    return False, None


def upsert_chunks(
    index,
    chunks: list[dict[str, Any]],
    embeddings: list[list[float]],
) -> dict[str, int]:
    """Upsert chunks to Pinecone with Tier 3 semantic dedup per chunk.

    Returns `{"upserted": N, "semantic_dups": M, "candidates": len(chunks)}`
    so the funnel line can surface dedup effectiveness."""
    vectors: list[dict[str, Any]] = []
    semantic_dups = 0
    for chunk, emb in zip(chunks, embeddings):
        dup, match = _is_semantic_dup(index, emb, chunk["article_id"])
        if dup:
            semantic_dups += 1
            log.info(
                "semantic_dup of %s (score=%.3f, existing_url=%s, new_url=%s)",
                (match or {}).get("id"),
                (match or {}).get("score") or 0,
                ((match or {}).get("metadata") or {}).get("source_url"),
                chunk.get("source_url"),
            )
            continue
        vectors.append(
            {
                "id": chunk["chunk_id"],
                "values": emb,
                "metadata": {
                    "article_id": chunk["article_id"],
                    "source_url": chunk["source_url"],
                    "source_feed": chunk.get("source_feed", ""),
                    "title": chunk["title"],
                    "text": chunk["text"][:1000],
                    "source_type": chunk["source_type"],
                    "region": chunk["region"],
                    "published_ts": int(chunk.get("published_ts") or 0),
                    "chunk_index": chunk["chunk_index"],
                },
            }
        )
    upserted = 0
    for i in range(0, len(vectors), 100):
        batch = vectors[i : i + 100]
        index.upsert(vectors=batch)
        upserted += len(batch)
    return {
        "upserted": upserted,
        "semantic_dups": semantic_dups,
        "candidates": len(chunks),
    }


def query(
    index,
    embedding: list[float],
    top_k: int,
    filter: dict[str, Any] | None = None,
    include_values: bool = False,
) -> list[dict[str, Any]]:
    res = index.query(
        vector=embedding,
        top_k=top_k,
        filter=filter,
        include_metadata=True,
        include_values=include_values,
    )
    out = []
    for match in res.get("matches", []):
        md = match.get("metadata", {}) or {}
        item = {
            "chunk_id": match["id"],
            "score": match["score"],
            "article_id": md.get("article_id"),
            "source_url": md.get("source_url"),
            "source_feed": md.get("source_feed", ""),
            "title": md.get("title"),
            "text": md.get("text"),
            "source_type": md.get("source_type"),
            "region": md.get("region"),
            "published_ts": md.get("published_ts"),
        }
        if include_values:
            item["embedding"] = match.get("values") or []
        out.append(item)
    return out
