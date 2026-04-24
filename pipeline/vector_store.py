"""Pinecone wrapper — upsert + query. Swap path: BigQuery VECTOR_SEARCH."""
from __future__ import annotations

import os
from typing import Any

from pinecone import Pinecone

PINECONE_INDEX = os.environ.get("PINECONE_INDEX", "aim-chunks")

_pc: Pinecone | None = None
_index = None


def get_index():
    """Lazy-init Pinecone client + index handle. One per process."""
    global _pc, _index
    if _index is None:
        _pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
        _index = _pc.Index(PINECONE_INDEX)
    return _index


def upsert_chunks(index, chunks: list[dict[str, Any]], embeddings: list[list[float]]) -> int:
    vectors = []
    for chunk, emb in zip(chunks, embeddings):
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
    return upserted


def query(index, embedding: list[float], top_k: int, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    res = index.query(
        vector=embedding,
        top_k=top_k,
        filter=filter,
        include_metadata=True,
    )
    out = []
    for match in res.get("matches", []):
        md = match.get("metadata", {}) or {}
        out.append(
            {
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
        )
    return out
