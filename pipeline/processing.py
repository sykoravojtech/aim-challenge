"""Chunking — LangChain RecursiveCharacterTextSplitter, title prepended."""
from __future__ import annotations

import uuid
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter

from pipeline.ingestion import RawDoc

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


def chunk_articles(docs: list[RawDoc]) -> list[dict[str, Any]]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )
    chunks: list[dict[str, Any]] = []
    for doc in docs:
        # Prepend title so each chunk is self-identifying at rerank time.
        body = f"{doc.title}\n\n{doc.text}"
        parts = splitter.split_text(body)
        for i, part in enumerate(parts):
            chunks.append(
                {
                    "chunk_id": uuid.uuid4().hex,
                    "article_id": doc.article_id,
                    "source_url": doc.source_url,
                    "source_feed": doc.source_feed,
                    "title": doc.title,
                    "text": part,
                    "source_type": doc.source_type,
                    "region": doc.region,
                    "published_ts": doc.published_ts,
                    "chunk_index": i,
                    "total_chunks": len(parts),
                }
            )
    return chunks
