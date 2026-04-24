"""Retrieval — hybrid filter ∩ ANN. Rerank + MMR land in Phase 4."""
from __future__ import annotations

from typing import Any

from openai import OpenAI

from models.schemas import Aim
from pipeline.embedding import embed_query
from pipeline.vector_store import query

RETRIEVE_TOP_K = 20


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
