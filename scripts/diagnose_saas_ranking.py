"""6G diagnostic: trace the 6 golden saas SEC URLs through retrieve → rerank → MMR.

Runs the same query/filter/top_k as main.py::run_pipeline but with instrumentation
at every stage. Does NOT mutate production code. Read-only against live Pinecone.

Usage:
    uv run python scripts/diagnose_saas_ranking.py
    uv run python scripts/diagnose_saas_ranking.py --top-k 100   # widen retrieve to rule in/out "didn't make top 30"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from openai import OpenAI  # noqa: E402

from pipeline import storage  # noqa: E402
from pipeline.embedding import embed_texts  # noqa: E402
from pipeline.retrieval import (  # noqa: E402
    build_query_filter,
    build_query_text,
    mmr_diversify,
    rerank_chunks,
)
from pipeline.vector_store import get_index, query as vector_query  # noqa: E402


AIM_ID = "saas-ai-legislation"

GOLDEN_POSITIVE_URLS = [
    "https://www.sec.gov/newsroom/press-releases/2026-34",
    "https://www.sec.gov/newsroom/press-releases/2026-35-sec-appoints-david-woodcock-director-division-enforcement",
    "https://www.sec.gov/newsroom/press-releases/2026-40-sec-cftc-jointly-propose-amendments-reduce-private-fund-reporting-burdens",
    "https://www.sec.gov/newsroom/press-releases/2026-37-sec-seeks-public-comment-consolidated-audit-trail-other-audit-trails-data-sources",
    "https://www.sec.gov/newsroom/press-releases/2026-36-sec-approves-exemptive-order-proposed-rule-change-permit-customer-cross-margining-us-treasury-market",
    "https://www.sec.gov/newsroom/press-releases/2026-39-chairman-atkins-launches-material-matters-podcast",
]


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=30, help="retrieve top_k")
    ap.add_argument("--rerank-top-n", type=int, default=15)
    ap.add_argument("--mmr-top-k", type=int, default=10)
    ap.add_argument("--mmr-lambda", type=float, default=0.7)
    ap.add_argument(
        "--probe-wide",
        action="store_true",
        help="also run top_k=500 retrieval to locate golden chunks' raw Pinecone rank",
    )
    args = ap.parse_args()

    golden_ids = {md5(u): u for u in GOLDEN_POSITIVE_URLS}
    print("Golden article_ids (md5 of URL):")
    for aid, u in golden_ids.items():
        print(f"  {aid}  {u}")
    print()

    aim = storage.get_aim(AIM_ID)
    if aim is None:
        raise SystemExit(f"aim {AIM_ID!r} not found — check data/aims/ or Firestore")

    print("Aim:")
    print(f"  regions       = {aim.regions}")
    print(f"  update_types  = {aim.update_types}")
    print(f"  query_text    = {build_query_text(aim)[:120]}...")
    filt = build_query_filter(aim)
    print(f"  query_filter  = {filt}")
    print()

    oai = OpenAI()
    index = get_index()

    print(f"Embedding query and retrieving top_k={args.top_k} with filter...")
    q_emb = embed_texts(oai, [build_query_text(aim)])[0]
    retrieved = vector_query(
        index, q_emb, top_k=args.top_k, filter=filt, include_values=True
    )
    print(f"  retrieved {len(retrieved)} chunks")

    # One chunk per article — collapse for readability.
    seen_aids: dict[str, dict] = {}
    for rank, c in enumerate(retrieved):
        aid = c["article_id"]
        if aid not in seen_aids:
            seen_aids[aid] = {"rank": rank, "score": c["score"], "chunk": c}

    print()
    print(f"Stage 1 — retrieve (top_k={args.top_k}):")
    print(f"  unique articles returned: {len(seen_aids)}")
    print()
    print(f"  Golden URL presence:")
    golden_retrieve_rank: dict[str, int | None] = {}
    for aid, url in golden_ids.items():
        info = seen_aids.get(aid)
        if info:
            print(
                f"    ✓ rank={info['rank']:3d} score={info['score']:.4f}  "
                f"source_type={info['chunk']['source_type']:<10} region={info['chunk']['region']:<6} "
                f"{url[:70]}"
            )
            golden_retrieve_rank[aid] = info["rank"]
        else:
            print(f"    ✗ NOT RETRIEVED        {url[:70]}")
            golden_retrieve_rank[aid] = None

    # Show top 15 retrieved — what's crowding the SEC content out?
    print()
    print(f"  Top 15 retrieved (by Pinecone score, chunk-level) — is SEC content crowded out?")
    for i, c in enumerate(retrieved[:15]):
        mark = "★" if c["article_id"] in golden_ids else " "
        print(
            f"    {mark} rank={i:3d} score={c['score']:.4f} "
            f"st={c['source_type']:<10} reg={c['region']:<6} "
            f"title={str(c.get('title', ''))[:60]}"
        )

    if args.probe_wide:
        print()
        print("Probe: widening retrieval to top_k=500 to locate golden chunks' raw rank...")
        retrieved_wide = vector_query(
            index, q_emb, top_k=500, filter=filt, include_values=False
        )
        for aid, url in golden_ids.items():
            wide_rank = None
            wide_score = None
            for r, c in enumerate(retrieved_wide):
                if c["article_id"] == aid:
                    wide_rank = r
                    wide_score = c["score"]
                    break
            print(f"    {aid}  wide_rank={wide_rank}  wide_score={wide_score}  {url[:60]}")

    # STAGE 2 — rerank
    print()
    print(f"Stage 2 — rerank (LLM, top_n={args.rerank_top_n}):")
    reranked = rerank_chunks(oai, retrieved, aim, top_n=args.rerank_top_n)

    rerank_rank_by_aid: dict[str, int | None] = {}
    seen_reranked: set[str] = set()
    for i, c in enumerate(reranked):
        aid = c["article_id"]
        if aid not in seen_reranked:
            rerank_rank_by_aid[aid] = i
            seen_reranked.add(aid)

    # Inspect rerank_score on every retrieved chunk for golden URLs
    print("  Golden URL rerank scores (from the 'retrieved' list — full pool):")
    for aid, url in golden_ids.items():
        matches_in_retrieved = [c for c in retrieved if c["article_id"] == aid]
        if not matches_in_retrieved:
            print(f"    -  NOT IN RETRIEVE POOL    {url[:70]}")
            continue
        scores = [c.get("rerank_score", "n/a") for c in matches_in_retrieved]
        survived = aid in rerank_rank_by_aid
        mark = "✓" if survived else "✗"
        print(
            f"    {mark} survived={survived} rerank_scores={scores}  "
            f"post-rerank rank={rerank_rank_by_aid.get(aid)}  {url[:50]}"
        )

    # Show top-15 reranked titles
    print()
    print("  Top 15 reranked (one-per-chunk order):")
    for i, c in enumerate(reranked):
        mark = "★" if c["article_id"] in golden_ids else " "
        print(
            f"    {mark} rank={i:2d} rerank_score={c.get('rerank_score', '?'):>3} "
            f"st={c['source_type']:<10} title={str(c.get('title', ''))[:60]}"
        )

    # STAGE 3 — MMR
    print()
    print(f"Stage 3 — MMR diversify (top_k={args.mmr_top_k}, λ={args.mmr_lambda}):")
    diversified = mmr_diversify(
        reranked, q_emb, top_k=args.mmr_top_k, lambda_=args.mmr_lambda
    )
    mmr_aids = {c["article_id"] for c in diversified}
    print(f"  post-MMR final: {len(diversified)} chunks, unique articles={len(mmr_aids)}")
    print("  Golden URL final presence:")
    for aid, url in golden_ids.items():
        present = aid in mmr_aids
        print(f"    {'✓' if present else '✗'}  {url[:80]}")

    # Summary verdict
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)
    retrieved_count = sum(1 for aid in golden_ids if golden_retrieve_rank.get(aid) is not None)
    reranked_count = sum(1 for aid in golden_ids if aid in rerank_rank_by_aid)
    final_count = sum(1 for aid in golden_ids if aid in mmr_aids)
    print(f"  Golden URLs retrieved ({args.top_k}): {retrieved_count}/6")
    print(f"  Golden URLs surviving rerank ({args.rerank_top_n}): {reranked_count}/6")
    print(f"  Golden URLs surviving MMR ({args.mmr_top_k}): {final_count}/6")
    print()
    print("Diagnosis hint:")
    if retrieved_count == 0:
        print("  → retrieval is dropping them. Either region filter mismatch or top_k too tight.")
    elif reranked_count < retrieved_count:
        print(f"  → rerank drops {retrieved_count - reranked_count} golden articles.")
        print(f"     Check whether rerank scores <8 on regulatory prose vs news headlines.")
    elif final_count < reranked_count:
        print(f"  → MMR drops {reranked_count - final_count} golden articles.")
        print(f"     5-of-6 golden are SEC press releases — MMR likely over-diversifying the SEC cluster.")
    else:
        print(f"  → all {final_count} survive the ranking stack. Check `generate` stage (prompt budget / top-10 slice).")


if __name__ == "__main__":
    main()
