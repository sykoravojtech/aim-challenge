"""Phase 6H capture — retrieve-with-cap → rerank → collapse-to-1-per-article → MMR → generate.

Mirrors main.py's retrieval path exactly, against cached Pinecone state (no
ingest, no upsert). Produces data/compare/phase6h_cap3_<aim>.json for
compare_digests.py diffing vs. phase4_full.

Usage:
  uv run python scripts/capture_phase6h.py --aim cee-founder-media
  uv run python scripts/capture_phase6h.py --aim saas-ai-legislation
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from openai import OpenAI  # noqa: E402

from pipeline import storage  # noqa: E402
from pipeline.embedding import embed_query  # noqa: E402
from pipeline.report import generate_digest  # noqa: E402
from pipeline.retrieval import (  # noqa: E402
    build_query_filter,
    build_query_text,
    collapse_chunks_by_article,
    mmr_diversify,
    rerank_chunks,
)
from pipeline.vector_store import get_index, query as vector_query  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase6h-capture")

COMPARE_DIR = ROOT / "data" / "compare"

# Mirror main.py constants exactly so capture == live pipeline.
RETRIEVE_TOP_K = 40
RETRIEVE_RAW_K = 1000
RERANK_TOP_N = 15
MMR_TOP_K = 10
MMR_LAMBDA = 0.7
PER_ARTICLE_CAP = 2


def capture(aim_id: str) -> dict[str, Any]:
    aim = storage.get_aim(aim_id)
    if aim is None:
        raise SystemExit(f"aim {aim_id!r} not found under data/aims/")
    oai = OpenAI()
    index = get_index()

    q_emb = embed_query(oai, build_query_text(aim))
    raw = vector_query(
        index,
        q_emb,
        top_k=RETRIEVE_RAW_K,
        filter=build_query_filter(aim),
        include_values=True,
    )
    retrieved = collapse_chunks_by_article(
        raw, top_k=RETRIEVE_TOP_K, per_article_cap=PER_ARTICLE_CAP
    )
    unique_articles = len({c["article_id"] for c in retrieved if c.get("article_id")})
    log.info(
        "retrieve: raw=%d → chunks=%d across %d articles",
        len(raw), len(retrieved), unique_articles,
    )

    reranked = rerank_chunks(oai, retrieved, aim, top_n=RERANK_TOP_N)
    reranked_unique = collapse_chunks_by_article(
        reranked, top_k=MMR_TOP_K * 2, per_article_cap=1, sort_key="rerank_score"
    )
    log.info("rerank: %d → %d → unique=%d", len(retrieved), len(reranked), len(reranked_unique))

    diversified = mmr_diversify(
        reranked_unique, q_emb, top_k=MMR_TOP_K, lambda_=MMR_LAMBDA
    )
    log.info("mmr: %d → %d", len(reranked_unique), len(diversified))

    digest = generate_digest(oai, aim, diversified)
    funnel = {
        "label": "phase6h_cap3",
        "aim_id": aim_id,
        "retrieved_raw": len(raw),
        "retrieved": len(retrieved),
        "unique_articles": unique_articles,
        "per_article_cap": PER_ARTICLE_CAP,
        "reranked": len(reranked),
        "reranked_unique": len(reranked_unique),
        "diversified": len(diversified),
        "sections": len(digest.get("sections", [])),
        "items": sum(len(s.get("items", [])) for s in digest.get("sections", [])),
    }
    digest["_funnel"] = funnel
    digest["_captured_at"] = datetime.now(timezone.utc).isoformat()
    log.info("sections=%d items=%d", funnel["sections"], funnel["items"])
    return digest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aim", required=True)
    args = ap.parse_args()

    COMPARE_DIR.mkdir(parents=True, exist_ok=True)
    digest = capture(args.aim)
    out = COMPARE_DIR / f"phase6h_cap3_{args.aim}.json"
    out.write_text(json.dumps(digest, indent=2))
    log.info("wrote %s", out.relative_to(ROOT))


if __name__ == "__main__":
    main()
