"""Phase 2 verification: run the pipeline twice and dump the funnels.

Run 1 (mode=force): clears the seen-set; chunks that already exist in Pinecone
should be caught by Tier 3 (semantic dedup, cosine ≥0.93 on a different
article_id). Hitting the same article twice is prevented by the uuid chunk_ids
being new each run — Tier 3 protects against *semantic* dupes, not replays.

Run 2 (mode=incremental): seen-set now contains every article from run 1, so
Tier 1 (URL md5) should skip them all. ingested=0, upserted=0, but retrieval
still finds chunks in Pinecone and the Digest still emits.

Writes: data/compare/phase2_dedup.json (run 2 digest + funnel).
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase2")

from main import JOB_STATUS, run_pipeline  # noqa: E402
from pipeline import storage  # noqa: E402

AIM_ID = "cee-founder-media"
COMPARE_DIR = ROOT / "data" / "compare"
COMPARE_DIR.mkdir(parents=True, exist_ok=True)


def _run(mode: str, tag: str) -> dict:
    digest_id = f"phase2-{tag}"
    JOB_STATUS[digest_id] = {"status": "queued", "aim_id": AIM_ID, "mode": mode}
    log.info("=== running mode=%s id=%s ===", mode, digest_id)
    run_pipeline(AIM_ID, digest_id, mode)  # synchronous call — no BackgroundTask
    digest = storage.get_digest(digest_id)
    if digest is None:
        raise SystemExit(f"run {mode} did not produce a digest on disk")
    return digest.model_dump(mode="json")


def main():
    # Run 1 — force, re-ingests everything; Tier 3 should catch semantic dupes
    # of anything already in Pinecone from Phase 1.
    first = _run("force", "run1-force")

    # Run 2 — incremental; Tier 1 should skip every article from run 1.
    second = _run("incremental", "run2-incremental")

    artefact = {
        "phase": "phase2_dedup",
        "aim_id": AIM_ID,
        "run1_force": {
            "digest_id": first["digest_id"],
            "funnel": first.get("funnel", {}),
            "sections": len(first.get("sections") or []),
            "items": sum(len(s.get("items") or []) for s in first.get("sections") or []),
        },
        "run2_incremental": {
            "digest_id": second["digest_id"],
            "funnel": second.get("funnel", {}),
            "sections": len(second.get("sections") or []),
            "items": sum(len(s.get("items") or []) for s in second.get("sections") or []),
        },
        "digest_run2": second,
    }
    out = COMPARE_DIR / "phase2_dedup.json"
    out.write_text(json.dumps(artefact, indent=2, ensure_ascii=False))
    log.info("wrote %s", out)
    # Terse success summary on stdout for the shell.
    print(json.dumps(
        {
            "run1_funnel": artefact["run1_force"]["funnel"],
            "run2_funnel": artefact["run2_incremental"]["funnel"],
            "run2_sections": artefact["run2_incremental"]["sections"],
            "run2_items": artefact["run2_incremental"]["items"],
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
