"""Phase 4 ablation capture — runs the retrieve→(rerank?)→(mmr?)→generate
path against cached Pinecone state for a given Aim and writes a Digest-shaped
JSON to data/compare/. No ingest, no upsert. Designed to produce the
before/after exhibits driven by scripts/compare_digests.py.

Usage:
  uv run python scripts/capture_phase4_snapshots.py --aim cee-founder-media
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
    mmr_diversify,
    rerank_chunks,
)
from pipeline.vector_store import get_index, query as vector_query  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase4-capture")

COMPARE_DIR = ROOT / "data" / "compare"


def run_variant(
    aim_id: str,
    label: str,
    *,
    rerank: bool,
    mmr: bool,
    retrieve_k: int = 30,
    rerank_n: int = 15,
    mmr_k: int = 10,
    mmr_lambda: float = 0.7,
) -> dict[str, Any]:
    aim = storage.get_aim(aim_id)
    if aim is None:
        raise SystemExit(f"aim {aim_id!r} not found under data/aims/")
    oai = OpenAI()
    index = get_index()

    q_emb = embed_query(oai, build_query_text(aim))
    chunks = vector_query(
        index,
        q_emb,
        top_k=retrieve_k,
        filter=build_query_filter(aim),
        include_values=True,
    )
    funnel: dict[str, Any] = {
        "label": label,
        "aim_id": aim_id,
        "retrieve_k": retrieve_k,
        "rerank": rerank,
        "mmr": mmr,
        "retrieved": len(chunks),
    }
    log.info("[%s] retrieved %d", label, len(chunks))

    if rerank:
        chunks = rerank_chunks(oai, chunks, aim, top_n=rerank_n)
        funnel["reranked"] = len(chunks)
        log.info("[%s] reranked to %d", label, len(chunks))

    if mmr:
        chunks = mmr_diversify(chunks, q_emb, top_k=mmr_k, lambda_=mmr_lambda)
        funnel["diversified"] = len(chunks)
        log.info("[%s] mmr-diversified to %d", label, len(chunks))
    elif len(chunks) > mmr_k and not rerank:
        # If neither stage trimmed, cap at mmr_k for apples-to-apples generate prompt size.
        chunks = chunks[:mmr_k]
        funnel["diversified"] = len(chunks)

    digest = generate_digest(oai, aim, chunks)
    funnel["sections"] = len(digest.get("sections", []))
    funnel["items"] = sum(len(s.get("items", [])) for s in digest.get("sections", []))
    digest["_funnel"] = funnel
    digest["_captured_at"] = datetime.now(timezone.utc).isoformat()
    log.info(
        "[%s] sections=%d items=%d", label, funnel["sections"], funnel["items"]
    )
    return digest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aim", default="cee-founder-media")
    ap.add_argument(
        "--variants",
        default="rerank_only,full",
        help="comma-separated subset of {rerank_only, mmr_only, full}",
    )
    args = ap.parse_args()

    COMPARE_DIR.mkdir(parents=True, exist_ok=True)
    wanted = {v.strip() for v in args.variants.split(",") if v.strip()}

    specs = {
        "rerank_only": dict(rerank=True, mmr=False),
        "mmr_only": dict(rerank=False, mmr=True),
        "full": dict(rerank=True, mmr=True),
    }
    unknown = wanted - specs.keys()
    if unknown:
        raise SystemExit(f"unknown variants: {sorted(unknown)}")

    for label in ("rerank_only", "mmr_only", "full"):
        if label not in wanted:
            continue
        digest = run_variant(args.aim, label, **specs[label])
        out = COMPARE_DIR / f"phase4_{label}.json"
        out.write_text(json.dumps(digest, indent=2))
        log.info("wrote %s", out.relative_to(ROOT))


if __name__ == "__main__":
    main()
