"""Embeddings — OpenAI text-embedding-3-small, batched. Swap path: VertexAI text-embedding-004."""
from __future__ import annotations

from openai import OpenAI

EMBED_MODEL = "text-embedding-3-small"
EMBED_BATCH = 100


def embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i : i + EMBED_BATCH]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        out.extend(d.embedding for d in resp.data)
    return out


def embed_query(client: OpenAI, text: str) -> list[float]:
    [emb] = embed_texts(client, [text])
    return emb
