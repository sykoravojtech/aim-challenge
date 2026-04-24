"""FastAPI surface: Aim CRUD + three-mode digest trigger + status polling.

Shape follows ARCHITECTURE § "Data flow" and ROADMAP Phase 1. The digest job
runs as a FastAPI BackgroundTask (Pub/Sub + Cloud Run is DECISIONS D4's
scale-up path; at this scale BackgroundTasks is semantically identical).
"""
from __future__ import annotations

import logging
import os
import sys
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
from pipeline.ingestion import SEED_AIMS, ingest_all_sources, mirror_raw_to_bq  # noqa: E402
from pipeline.processing import chunk_articles  # noqa: E402
from pipeline.report import generate_digest  # noqa: E402
from pipeline.retrieval import (  # noqa: E402
    build_query_filter,
    build_query_text,
    mmr_diversify,
    rerank_chunks,
)
from pipeline.vector_store import get_index, query as vector_query, upsert_chunks  # noqa: E402

# Phase 4 funnel shape: retrieve 30 → rerank to 15 → MMR diversify to 10 → generate.
RETRIEVE_TOP_K = 30
RERANK_TOP_N = 15
MMR_TOP_K = 10
MMR_LAMBDA = 0.7

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
    if any(storage.AIMS_DIR.glob("*.json")) if storage.AIMS_DIR.exists() else False:
        return
    now = storage.now_iso()
    for seed in SEED_AIMS:
        aim = Aim(**seed, created_at=now, updated_at=now)
        storage.save_aim(aim)
    log.info("seeded %d demo aims", len(SEED_AIMS))


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


@app.get("/digest/{digest_id}")
def get_digest_status(digest_id: str) -> dict[str, Any] | Digest:
    # If the job is live and not yet complete, surface the stage name.
    tracker = JOB_STATUS.get(digest_id)
    if tracker and tracker["status"] not in ("complete", "failed"):
        return {
            "digest_id": digest_id,
            "status": tracker["status"],
            "aim_id": tracker["aim_id"],
            "mode": tracker["mode"],
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
            _set_stage(digest_id, "ingesting")
            seen_ids = storage.get_seen_article_ids() if mode == "incremental" else None
            docs, source_stats = ingest_all_sources(seen_ids=seen_ids)
            funnel["ingested"] = len(docs)
            funnel["source_stats"] = source_stats
            log.info("[%s] ingested %d docs", digest_id, len(docs))

            if docs:
                raw_payload = [d.to_dict() for d in docs]
                storage.save_raw_articles(raw_payload, digest_id)
                # BQ mirror: gated + never-raise. Local JSON above stays dedup truth.
                mirror_raw_to_bq(raw_payload, digest_id)

                _set_stage(digest_id, "processing")
                chunks = chunk_articles(docs)
                funnel["chunked"] = len(chunks)

                _set_stage(digest_id, "embedding")
                embeddings = embed_texts(oai, [c["text"] for c in chunks])
                funnel["embedded"] = len(embeddings)

                _set_stage(digest_id, "upserting")
                upsert_result = upsert_chunks(index, chunks, embeddings)
                funnel["upserted"] = upsert_result["upserted"]
                funnel["semantic_dups"] = upsert_result["semantic_dups"]
                log.info(
                    "[%s] upserted %d / %d chunks (tier3 semantic_dups=%d)",
                    digest_id,
                    upsert_result["upserted"],
                    upsert_result["candidates"],
                    upsert_result["semantic_dups"],
                )

        _set_stage(digest_id, "retrieving")
        q_emb = embed_texts(oai, [build_query_text(aim)])[0]
        retrieved = vector_query(
            index,
            q_emb,
            top_k=RETRIEVE_TOP_K,
            filter=build_query_filter(aim),
            include_values=True,
        )
        funnel["retrieved"] = len(retrieved)
        if not retrieved:
            log.warning("[%s] retrieval returned 0 chunks; regions=%s", digest_id, aim.regions)

        _set_stage(digest_id, "reranking")
        reranked = rerank_chunks(oai, retrieved, aim, top_n=RERANK_TOP_N)
        funnel["reranked"] = len(reranked)

        _set_stage(digest_id, "diversifying")
        diversified = mmr_diversify(reranked, q_emb, top_k=MMR_TOP_K, lambda_=MMR_LAMBDA)
        funnel["diversified"] = len(diversified)
        log.info(
            "[%s] rerank+mmr funnel: %d → %d → %d",
            digest_id, len(retrieved), len(reranked), len(diversified),
        )

        _set_stage(digest_id, "generating")
        raw_digest = generate_digest(oai, aim, diversified)
        funnel["sections"] = len(raw_digest.get("sections", []))
        funnel["items"] = sum(len(s.get("items", [])) for s in raw_digest.get("sections", []))

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
