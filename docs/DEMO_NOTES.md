# Demo notes

Live talking-points doc for the **16:45 show & tell**. Curated as the day unfolds, not scrambled together at 16:30.

The 10-minute slot must cover: *how it works, why these choices, what to build next with a week, biggest risks* (per [ASSIGNMENT.md](../ASSIGNMENT.md)). The section order below matches [ROADMAP § Final demo structure](ROADMAP.md#final-demo-structure).

Source-of-truth docs this curates from:
- **Why** → [DECISIONS.md](DECISIONS.md) (every entry's *Interview note* field is a demo-script line; this doc just picks the top 3–5 to lead with).
- **Surprises / fragility** → [LESSONS.md](LESSONS.md).
- **Scope** → [CLAUDE.md § Scope](../CLAUDE.md#scope--what-were-building-and-what-were-not).

Keep every bullet below specific: *name the module, name the function, name the line count.* Weak candidates hand-wave; strong ones point at code.

---

## 1. How it works — the 8-verb spine

`ingest → extract → chunk → embed → store → retrieve → rerank → generate`

One sentence per verb at demo time. Point at the funnel-metrics log line (e.g. `412 raw → 245 clean → 40 reranked → 10 items`).

Update this section as each phase lands — only claim a verb if it's actually wired.

| Verb | Module | Status | One-liner for demo |
|---|---|---|---|
| ingest | `pipeline/ingestion.py` | _tbd_ | _tbd_ |
| extract | `pipeline/processing.py` | _tbd_ | _tbd_ |
| chunk | `pipeline/processing.py` | _tbd_ | _tbd_ |
| embed | `pipeline/embedding.py` | _tbd_ | _tbd_ |
| store | `pipeline/vector_store.py` | _tbd_ | _tbd_ |
| retrieve | `pipeline/retrieval.py` | _tbd_ | _tbd_ |
| rerank | `pipeline/retrieval.py` | _tbd_ | _tbd_ |
| generate | `pipeline/report.py` | _tbd_ | _tbd_ |

---

## 2. Why these choices — top 3–5 decisions to lead with

Pick the highest-signal entries from [DECISIONS.md](DECISIONS.md) to foreground. Rest stay available for Q&A.

- _tbd — append as decisions land with stronger conviction during implementation_

---

## 3. What I chose to go deep on — the 14:30 fork

The 14:30 alarm forces an explicit trade-off call. Name it aloud at demo.

_Filled in at 14:30._

---

## 4. What I cut — deliberate scope calls

Append every scope cut, stub, degraded path, or 30-min-rule pivot here as it happens. Format: **cut** — 1-line *why* — what would make me unstub it.

Seeded from [CLAUDE.md § Scope](../CLAUDE.md#scope--what-were-building-and-what-were-not):

- **Video + audio sources (YouTube, podcasts, Whisper)** — adds 1h transcription latency for zero marginal architectural signal. Would unstub with a week: `PodcastConnector` + `yt-dlp` + Whisper batch job.
- **Pub/Sub fan-out** — identical semantics to `BackgroundTasks` at this scale, costs 2h of infra. Verbalise the pattern; don't run it.
- **Firestore / BigQuery / VertexAI / Cloud Run** — Phase 5 conditional. Local JSON + Pinecone + OpenAI is faster to green. Swap plan is a single file each (`storage.py`, `embedding.py`).
- **Auth** — `user_id` is trust-the-client. See [D15](DECISIONS.md#d15-auth-is-out-of-scope-user_id-is-trust-the-client).

_Append new cuts below as they happen._

---

## 5. What's next with a week — named modules, named line counts

*This is the section weak candidates hand-wave on.* Every bullet must name a file, a function, and a rough line count.

Seed candidates (expand + sharpen as the day goes):

- **Full dedup tier 2+3** — `pipeline/dedup.py` `Deduper.minhash_dedup()` + `.semantic_dedup()`, ~80 LOC. Tier 1 URL-hash is live; tiers 2/3 are scaffolded.
- **PodcastConnector** — `pipeline/sources/podcast.py` via registry, ~120 LOC (yt-dlp + Whisper batch).
- **Real Pub/Sub fan-out** — topic-per-stage + DLQ, ~60 LOC + GCP config.
- **BigQuery VECTOR_SEARCH** as an alternate `VectorStore` implementation — ~50 LOC swap in `pipeline/vector_store.py`.
- _append as items get deferred during the day_

---

## 6. Biggest risks — what's fragile in the current build

What would break under load, at 3am, or under a hostile source. Append as discovered while running the pipe.

Seed risks (upgrade with specifics as they're encountered):

- **Source flakiness** — per-source try/except + tenacity retries mitigates; a single slow feed can still stall `ingest` if timeouts aren't tight. See [LESSONS.md](LESSONS.md) for any concrete incidents logged.
- **LLM output-shape drift** — mitigated by `safe_llm_json()` ([LESSONS § LLM output-shape handling](LESSONS.md)); still the single most likely production-break.
- **Pinecone single-tenancy** — one global index, metadata-filtered per Aim. Fine at demo scale, needs per-namespace partitioning at multi-tenant scale.
- **No observability beyond stdout funnel metrics** — demo-OK, prod-blocker.
- _append as real fragility shows up during runs_

---

## Update discipline

Driven by [CLAUDE.md § Self-maintenance](../CLAUDE.md#self-maintenance). Each phase completion, scope cut, pivot, or fragile discovery writes into the relevant section above — so at 16:30 this doc *is* the demo script.
