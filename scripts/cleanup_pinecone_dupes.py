"""Pinecone chunk-level dedup cleanup — the fix 6G surfaced but deferred.

Why: Tier 3 semantic dedup filters `article_id $ne`, which catches cross-article
near-duplicates but lets same-URL re-upserts across `force` runs accumulate.
Diagnostic showed one article holding 126 chunks; index has ~4,122 vectors for
~60 unique articles (≈ 7x duplication).

Strategy: for each `article_id` ever seen in `data/raw/*.json`, query Pinecone
for all its chunks, group by `chunk_index`, keep the first chunk_id per
(article_id, chunk_index) tuple, delete the rest. chunk_index is deterministic
from the RecursiveCharacterTextSplitter output order, so two chunks with the
same (article_id, chunk_index) hold identical (or near-identical) text.

Usage:
    uv run python scripts/cleanup_pinecone_dupes.py            # dry-run
    uv run python scripts/cleanup_pinecone_dupes.py --apply    # execute deletes
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from pipeline.vector_store import get_index  # noqa: E402


def collect_article_ids(raw_dir: Path) -> set[str]:
    """All article_ids ever written to data/raw/*.json (superset of live ones)."""
    aids: set[str] = set()
    for f in raw_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for article in items:
            aid = article.get("article_id")
            if aid:
                aids.add(aid)
    return aids


def fetch_all_chunks_for_article(index, article_id: str) -> list[dict]:
    """Pull every chunk vector for a given article_id via metadata filter.
    Uses a dummy query vector since we only care about the filter match;
    top_k=1000 is far above any sane per-article chunk count even with dupes."""
    dummy = [0.0] * 1536
    res = index.query(
        vector=dummy,
        top_k=1000,
        filter={"article_id": {"$eq": article_id}},
        include_metadata=True,
    )
    return [
        {
            "chunk_id": m["id"],
            "score": m.get("score"),
            "chunk_index": (m.get("metadata") or {}).get("chunk_index"),
            "source_url": (m.get("metadata") or {}).get("source_url"),
        }
        for m in res.get("matches", [])
    ]


def plan_deletions(chunks: list[dict]) -> list[str]:
    """Group by chunk_index, keep first chunk_id per index, return the rest."""
    seen_by_index: dict[int, str] = {}
    to_delete: list[str] = []
    for c in chunks:
        ci = c.get("chunk_index")
        if ci is None:
            continue
        if ci in seen_by_index:
            to_delete.append(c["chunk_id"])
        else:
            seen_by_index[ci] = c["chunk_id"]
    return to_delete


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="execute the deletes (otherwise dry-run)")
    args = ap.parse_args()

    index = get_index()
    stats_before = index.describe_index_stats()
    total_before = stats_before.get("total_vector_count", "?")
    print(f"Pinecone state BEFORE: total_vectors={total_before}")

    aids = collect_article_ids(ROOT / "data" / "raw")
    print(f"Checking {len(aids)} article_ids from data/raw/*.json")

    total_dupes = 0
    per_article_summary: list[tuple[str, int, int]] = []  # (aid, n_total, n_delete)
    all_to_delete: list[str] = []

    for i, aid in enumerate(sorted(aids)):
        chunks = fetch_all_chunks_for_article(index, aid)
        to_delete = plan_deletions(chunks)
        if chunks or to_delete:
            per_article_summary.append((aid, len(chunks), len(to_delete)))
        total_dupes += len(to_delete)
        all_to_delete.extend(to_delete)
        if (i + 1) % 20 == 0:
            print(f"  scanned {i+1}/{len(aids)}... running dupes={total_dupes}")

    # Sort by dupes desc
    per_article_summary.sort(key=lambda p: -p[2])
    print()
    print(f"Top 15 duplication offenders (article_id, n_chunks_in_pinecone, n_dupes_to_delete):")
    for aid, n_total, n_delete in per_article_summary[:15]:
        print(f"  {aid[:16]}  total={n_total:4d}  delete={n_delete:4d}  keep={n_total-n_delete:3d}")

    print()
    print(f"PLAN: delete {total_dupes} duplicate chunks across {sum(1 for _, _, d in per_article_summary if d > 0)} articles.")
    print(f"Would keep: {sum(n_total - n_delete for _, n_total, n_delete in per_article_summary)} chunks.")
    print(f"Expected post-cleanup total_vectors ≈ {total_before - total_dupes}")

    if not args.apply:
        print()
        print("DRY RUN — re-run with --apply to execute.")
        return

    print()
    print("Applying deletes in batches of 1000...")
    BATCH = 1000
    for i in range(0, len(all_to_delete), BATCH):
        batch = all_to_delete[i : i + BATCH]
        index.delete(ids=batch)
        print(f"  deleted {min(i + BATCH, len(all_to_delete))}/{len(all_to_delete)}")

    # Allow Pinecone stats to catch up
    import time

    time.sleep(3)
    stats_after = index.describe_index_stats()
    total_after = stats_after.get("total_vector_count", "?")
    print()
    print(f"Pinecone state AFTER:  total_vectors={total_after}  (Δ={total_before - total_after})")


if __name__ == "__main__":
    main()
