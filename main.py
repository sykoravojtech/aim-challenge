"""FastAPI surface: Aim CRUD + three-mode digest trigger + status polling.

Shape follows ARCHITECTURE § "Data flow" and ROADMAP Phase 1. The digest job
runs as a FastAPI BackgroundTask (Pub/Sub + Cloud Run is DECISIONS D4's
scale-up path; at this scale BackgroundTasks is semantically identical).
"""
from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles

# Make first-party imports work regardless of cwd.
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from models.schemas import (  # noqa: E402
    Aim,
    AimCreate,
    AimUpdate,
    Digest,
    DigestItem,
    DigestSection,
)
from pipeline import storage  # noqa: E402
from pipeline.embedding import embed_texts  # noqa: E402
from pipeline.ingestion import (  # noqa: E402
    SEED_AIMS,
    ingest_all_sources,
    mirror_raw_to_bq,
    mirror_raw_to_gcs,
)
from pipeline.processing import chunk_articles  # noqa: E402
from pipeline.report import generate_digest  # noqa: E402
from pipeline.retrieval import (  # noqa: E402
    build_query_filter,
    build_query_text,
    collapse_chunks_by_article,
    mmr_diversify,
    rerank_chunks,
)
from pipeline.vector_store import get_index, query as vector_query, upsert_chunks  # noqa: E402

# Phase 4 funnel shape: retrieve → rerank → MMR diversify → generate.
# Phase 6G: Tier-3 dedup filters `article_id $ne` — it catches cross-article
# near-dupes but NOT same-article re-upserts across `force` runs, so Pinecone
# accumulates chunk-duplicates (diagnostic showed one article with 126 chunks).
# Fix: retrieve a wide raw-chunk pool, collapse to best-scored chunk per
# article_id, keep top RETRIEVE_TOP_K unique articles. Moved saas-ai recall
# 0.00 → >0 without touching rerank or MMR.
RETRIEVE_TOP_K = 40
# 300 covers the region-filtered corpus comfortably (total index ~811 post-6G
# cleanup). The collapse needs top 40 articles by score; anything below rank
# ~200 wasn't winning. 6H: cuts retrieve latency ~3× vs top_k=1000 by shrinking
# the `include_values=True` payload.
RETRIEVE_RAW_K = 300
RERANK_TOP_N = 15
MMR_TOP_K = 10
MMR_LAMBDA = 0.7
# How many chunks per article to keep into rerank — 2 preserves multi-chunk
# context for long pieces (CEE fix) without blowing out the rerank JSON size.
# cap=3 produced 119 chunks which made gpt-4o-mini runaway-generate and fall
# back to vector order; cap=2 keeps ~80 chunks, comfortable under the limit.
PER_ARTICLE_CAP = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aim")

# Mode = how much of the pipeline runs. See ARCHITECTURE § "Three modes".
Mode = Literal["incremental", "force", "cached"]

# In-memory job tracker. Cleared on process restart (fine — completed digests
# are on disk). Holds the *live* stage name for GET /digest/{id} polling.
JOB_STATUS: dict[str, dict[str, Any]] = {}

app = FastAPI(title="Aim challenge pipeline")


# ---------------------------------------------------------------------------
# Startup — seed demo Aims so a fresh clone has something to hit with curl.
# ---------------------------------------------------------------------------


@app.on_event("startup")
def _seed_demo_aims() -> None:
    # Check per-aim via storage.get_aim so a Cloud Run cold start doesn't
    # overwrite Firestore edits the user made against a previous container
    # (ephemeral local disk would always look empty on boot).
    now = storage.now_iso()
    seeded = 0
    for seed in SEED_AIMS:
        if storage.get_aim(seed["aim_id"]) is not None:
            continue
        aim = Aim(**seed, created_at=now, updated_at=now)
        storage.save_aim(aim)
        seeded += 1
    if seeded:
        log.info("seeded %d demo aims", seeded)


# ---------------------------------------------------------------------------
# Aim CRUD
# ---------------------------------------------------------------------------


@app.post("/aim")
def create_aim(body: AimCreate) -> dict[str, str]:
    aim_id = uuid.uuid4().hex[:12]
    now = storage.now_iso()
    aim = Aim(**body.model_dump(), aim_id=aim_id, created_at=now, updated_at=now)
    storage.save_aim(aim)
    return {"aim_id": aim_id}


@app.get("/aim/{aim_id}")
def get_aim(aim_id: str) -> Aim:
    aim = storage.get_aim(aim_id)
    if aim is None:
        raise HTTPException(status_code=404, detail=f"aim {aim_id!r} not found")
    return aim


@app.put("/aim/{aim_id}")
def put_aim(aim_id: str, patch: AimUpdate) -> Aim:
    updated = storage.update_aim(aim_id, patch)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"aim {aim_id!r} not found")
    return updated


@app.delete("/aim/{aim_id}", status_code=204)
def delete_aim(aim_id: str) -> Response:
    if not storage.delete_aim(aim_id):
        raise HTTPException(status_code=404, detail=f"aim {aim_id!r} not found")
    return Response(status_code=204)


@app.get("/aims")
def list_aims(user_id: str = Query(...)) -> list[Aim]:
    return storage.list_aims_for_user(user_id)


# ---------------------------------------------------------------------------
# Digest trigger + status polling
# ---------------------------------------------------------------------------


@app.post("/aim/{aim_id}/digest")
def trigger_digest(
    aim_id: str,
    background_tasks: BackgroundTasks,
    mode: Mode = Query("incremental"),
) -> dict[str, str]:
    if storage.get_aim(aim_id) is None:
        raise HTTPException(status_code=404, detail=f"aim {aim_id!r} not found")
    digest_id = uuid.uuid4().hex[:12]
    JOB_STATUS[digest_id] = {"status": "queued", "aim_id": aim_id, "mode": mode}
    background_tasks.add_task(run_pipeline, aim_id, digest_id, mode)
    return {"job_id": digest_id, "digest_id": digest_id, "status": "queued"}


@app.get("/aim/{aim_id}/digests")
def list_aim_digests(aim_id: str) -> list[dict[str, Any]]:
    """Lightweight history of completed digests for an Aim, newest first."""
    if storage.get_aim(aim_id) is None:
        raise HTTPException(status_code=404, detail=f"aim {aim_id!r} not found")
    return storage.list_digests_for_aim(aim_id)


@app.get("/digest/{digest_id}")
def get_digest_status(digest_id: str) -> dict[str, Any] | Digest:
    # If the job is live and not yet complete, surface the stage name.
    tracker = JOB_STATUS.get(digest_id)
    if tracker and tracker["status"] not in ("complete", "failed"):
        t0 = tracker.get("_stage_started_at")
        elapsed_s = round(time.perf_counter() - t0, 1) if t0 else None
        return {
            "digest_id": digest_id,
            "status": tracker["status"],
            "aim_id": tracker["aim_id"],
            "mode": tracker["mode"],
            "elapsed_s": elapsed_s,
            "funnel": tracker.get("funnel", {}),
        }
    # Completed (or never seen by this process) — try disk.
    digest = storage.get_digest(digest_id)
    if digest is None:
        if tracker and tracker["status"] == "failed":
            return {"digest_id": digest_id, "status": "failed", **tracker}
        raise HTTPException(status_code=404, detail=f"digest {digest_id!r} not found")
    return digest


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "pinecone_index": os.environ.get("PINECONE_INDEX", "aim-chunks")}


# Static frontend — mounted LAST so API routes win on path conflicts.
# `html=True` makes `/` serve `static/index.html`.
app.mount("/", StaticFiles(directory=str(ROOT / "static"), html=True), name="static")


# ---------------------------------------------------------------------------
# The BackgroundTask — one pipeline run per digest.
# ---------------------------------------------------------------------------


def _set_stage(digest_id: str, stage: str) -> None:
    entry = JOB_STATUS.setdefault(digest_id, {})
    entry["status"] = stage
    entry["_stage_started_at"] = time.perf_counter()
    log.info("[%s] → %s", digest_id, stage)


def _to_digest_model(
    digest_id: str, aim_id: str, mode: Mode, raw: dict[str, Any],
    status: str, funnel: dict[str, Any],
) -> Digest:
    sections = [
        DigestSection(
            title=s.get("title", ""),
            items=[DigestItem(**_clean_item(i)) for i in s.get("items", [])],
        )
        for s in raw.get("sections", [])
    ]
    return Digest(
        digest_id=digest_id,
        aim_id=aim_id,
        headline=raw.get("headline", ""),
        date_range=raw.get("date_range", ""),
        sections=sections,
        generated_at=datetime.now(timezone.utc).isoformat(),
        status=status,
        mode=mode,
        funnel=funnel,
    )


def _clean_item(item: dict[str, Any]) -> dict[str, Any]:
    """Defensive coercion — LLM occasionally omits or mistypes fields."""
    return {
        "title": str(item.get("title", "")),
        "body": str(item.get("body", "")),
        "source_urls": [str(u) for u in (item.get("source_urls") or [])],
        "source_count": int(item.get("source_count") or len(item.get("source_urls") or [])),
        "item_type": str(item.get("item_type", "news")),
        "relevance_score": int(item.get("relevance_score") or 0),
    }


def run_pipeline(aim_id: str, digest_id: str, mode: Mode) -> None:
    """BackgroundTask entry point. Never raise — any failure becomes a
    `status=failed` Digest on disk so the caller gets a useful 200 back."""
    funnel: dict[str, Any] = {"aim_id": aim_id, "digest_id": digest_id, "mode": mode}
    timing: dict[str, int] = {}
    funnel["timing_ms"] = timing
    # Store live reference so GET /digest can surface partial funnel while running.
    JOB_STATUS.setdefault(digest_id, {})["funnel"] = funnel
    try:
        aim = storage.get_aim(aim_id)
        if aim is None:
            raise RuntimeError(f"aim {aim_id!r} vanished before pipeline started")

        from openai import OpenAI
        oai = OpenAI()
        index = get_index()

        if mode == "cached":
            log.info("[%s] cached mode — skipping ingest, retrieving against current Pinecone state", digest_id)
        else:
            t0 = time.perf_counter()
            _set_stage(digest_id, "ingesting")
            seen_ids = storage.get_seen_article_ids() if mode == "incremental" else None
            docs, source_stats = ingest_all_sources(seen_ids=seen_ids)
            funnel["ingested"] = len(docs)
            funnel["source_stats"] = source_stats
            timing["ingesting"] = round((time.perf_counter() - t0) * 1000)
            log.info("[%s] ingested %d docs in %.1fs", digest_id, len(docs), timing["ingesting"] / 1000)

            if docs:
                raw_payload = [d.to_dict() for d in docs]
                storage.save_raw_articles(raw_payload, digest_id)
                # BQ + GCS mirrors: gated + never-raise. Local JSON stays dedup truth.
                mirror_raw_to_bq(raw_payload, digest_id)
                mirror_raw_to_gcs(raw_payload, digest_id)

                t0 = time.perf_counter()
                _set_stage(digest_id, "processing")
                chunks = chunk_articles(docs)
                funnel["chunked"] = len(chunks)
                timing["processing"] = round((time.perf_counter() - t0) * 1000)

                t0 = time.perf_counter()
                _set_stage(digest_id, "embedding")
                embeddings = embed_texts(oai, [c["text"] for c in chunks])
                funnel["embedded"] = len(embeddings)
                timing["embedding"] = round((time.perf_counter() - t0) * 1000)

                t0 = time.perf_counter()
                _set_stage(digest_id, "upserting")
                upsert_result = upsert_chunks(index, chunks, embeddings)
                funnel["upserted"] = upsert_result["upserted"]
                funnel["semantic_dups"] = upsert_result["semantic_dups"]
                timing["upserting"] = round((time.perf_counter() - t0) * 1000)
                log.info(
                    "[%s] upserted %d / %d chunks (tier3 semantic_dups=%d) in %.1fs",
                    digest_id,
                    upsert_result["upserted"],
                    upsert_result["candidates"],
                    upsert_result["semantic_dups"],
                    timing["upserting"] / 1000,
                )

        t0 = time.perf_counter()
        _set_stage(digest_id, "retrieving")
        q_emb = embed_texts(oai, [build_query_text(aim)])[0]
        raw_chunks = vector_query(
            index,
            q_emb,
            top_k=RETRIEVE_RAW_K,
            filter=build_query_filter(aim),
            include_values=True,
        )
        # Phase 6G: collapse to unique articles, preserving up to PER_ARTICLE_CAP
        # chunks per article so rerank sees multi-chunk context for long pieces
        # (CEE regression fix — single-chunk collapse starved long articles at rerank).
        retrieved = collapse_chunks_by_article(
            raw_chunks, top_k=RETRIEVE_TOP_K, per_article_cap=PER_ARTICLE_CAP
        )
        funnel["retrieved_raw"] = len(raw_chunks)
        funnel["retrieved"] = len(retrieved)
        funnel["unique_articles"] = len({c["article_id"] for c in retrieved if c.get("article_id")})
        timing["retrieving"] = round((time.perf_counter() - t0) * 1000)
        log.info(
            "[%s] retrieve funnel: raw=%d → chunks=%d across %d articles (top_k=%d, cap=%d) in %.1fs",
            digest_id, len(raw_chunks), len(retrieved),
            funnel["unique_articles"], RETRIEVE_TOP_K, PER_ARTICLE_CAP,
            timing["retrieving"] / 1000,
        )
        if not retrieved:
            log.warning("[%s] retrieval returned 0 chunks; regions=%s", digest_id, aim.regions)

        t0 = time.perf_counter()
        _set_stage(digest_id, "reranking")
        reranked = rerank_chunks(oai, retrieved, aim, top_n=RERANK_TOP_N)
        funnel["reranked"] = len(reranked)
        timing["reranking"] = round((time.perf_counter() - t0) * 1000)

        # Post-rerank: collapse back to 1 chunk per article (best rerank_score)
        # so the user-facing digest has one item per story, not multiple
        # chunks from the same piece.
        reranked_unique = collapse_chunks_by_article(
            reranked, top_k=MMR_TOP_K * 2, per_article_cap=1, sort_key="rerank_score"
        )
        funnel["reranked_unique"] = len(reranked_unique)

        t0 = time.perf_counter()
        _set_stage(digest_id, "diversifying")
        diversified = mmr_diversify(reranked_unique, q_emb, top_k=MMR_TOP_K, lambda_=MMR_LAMBDA)
        funnel["diversified"] = len(diversified)
        timing["diversifying"] = round((time.perf_counter() - t0) * 1000)
        log.info(
            "[%s] rerank+mmr funnel: %d → %d → unique=%d → %d in %.1fs+%.1fs",
            digest_id, len(retrieved), len(reranked), len(reranked_unique), len(diversified),
            timing["reranking"] / 1000, timing["diversifying"] / 1000,
        )

        t0 = time.perf_counter()
        _set_stage(digest_id, "generating")
        raw_digest = generate_digest(oai, aim, diversified)
        funnel["sections"] = len(raw_digest.get("sections", []))
        funnel["items"] = sum(len(s.get("items", [])) for s in raw_digest.get("sections", []))
        timing["generating"] = round((time.perf_counter() - t0) * 1000)

        digest = _to_digest_model(digest_id, aim_id, mode, raw_digest, status="complete", funnel=funnel)
        storage.save_digest(digest)
        _set_stage(digest_id, "complete")
        log.info("[%s] complete — sections=%d items=%d", digest_id, funnel["sections"], funnel["items"])
    except Exception as e:
        log.exception("[%s] pipeline failed: %s", digest_id, e)
        funnel["error"] = f"{type(e).__name__}: {e}"
        try:
            failed = _to_digest_model(
                digest_id, aim_id, mode,
                {"headline": "Digest generation failed", "date_range": "", "sections": []},
                status="failed", funnel=funnel,
            )
            storage.save_digest(failed)
        except Exception:
            log.exception("[%s] also failed to persist failure digest", digest_id)
        JOB_STATUS.setdefault(digest_id, {})["status"] = "failed"
