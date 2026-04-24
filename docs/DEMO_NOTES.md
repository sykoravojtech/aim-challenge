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

Phase 0 baseline funnel (CEE Aim): `INGESTED=77 → CHUNKED=582 → EMBEDDED=582 → UPSERTED=582 → RETRIEVED=20 → SECTIONS=3 / ITEMS=7` end-to-end in ~90 s. Saved to `data/compare/phase0_walking_skeleton.json`.

| Verb | Module | Status | One-liner for demo |
|---|---|---|---|
| ingest | `scripts/run_pipeline.py::ingest_all_sources` (Phase 1: `pipeline/ingestion.py`) | ✅ Phase 0 | 10 RSS feeds behind a `RSSConnector` in a `REGISTRY` dict — adding a Mexican-state-institutions source is one `@register("mexico_gov")` line, not a pipeline rewrite. |
| extract | `RSSConnector.fetch` | ✅ Phase 0 | `trafilatura` on the article URL, fall back to RSS `<content:encoded>` when 403 — **saved 15/77 docs on SEC + VentureBeat this morning.** |
| chunk | `chunk_articles` | ✅ Phase 0 | LangChain `RecursiveCharacterTextSplitter(800, overlap=100)`, title prepended so every chunk is self-identifying at rerank time. |
| embed | `embed_texts` | ✅ Phase 0 | `text-embedding-3-small`, batched ≤100. Swap path to VertexAI `text-embedding-004` with `task_type=RETRIEVAL_DOCUMENT` is one file. |
| store | `upsert_chunks` (Phase 1: `pipeline/vector_store.py`) | ✅ Phase 0 | Pinecone serverless, `dim=1536`, chunks tagged with `region` + `source_type` at ingest — **these are filter dimensions, not prompt content.** |
| retrieve | `retrieve_relevant_chunks` | ✅ Phase 0 | Hybrid retrieval: `{"region": {"$in": [*aim.regions, "Global"]}}` as a Pinecone filter *before* ANN. Global always OR'd in so Global-tagged pieces still serve regional Aims. |
| rerank | `pipeline/retrieval.rerank_chunks` | ⏳ Phase 4 | `gpt-4o-mini` JSON call with full Aim in the prompt — 15× cheaper than `gpt-4o` for structured rerank. |
| generate | `generate_digest` (Phase 1: `pipeline/report.py`) | ✅ Phase 0 | `gpt-4o-mini`, `response_format={"type":"json_object"}`, `temperature=0.3`. **LLM picks 2–5 section titles per run** — today it chose "Funding Announcements / Startup Internationalisation Efforts / Technological Developments" for the CEE Aim. |

---

## 2. Why these choices — top 3–5 decisions to lead with

Pick the highest-signal entries from [DECISIONS.md](DECISIONS.md) to foreground. Rest stay available for Q&A.

- **Aim's structured fields are Pinecone filter dimensions, not prompt content.** *"Regions aren't prompt decoration — chunks carry `region` + `source_type` metadata at ingest, and retrieval applies `{"region": {"$in": [*aim.regions, "Global"]}}` before ANN. Hybrid retrieval — structured filter ∩ semantic search. The rerank can't overrule what was never retrieved."* Wired since Phase 0 (10 lines of cost, strongest RAG signal in the build).
- **Walking skeleton first.** All 8 verbs ran end-to-end in one script before anything got polished (see `scripts/run_pipeline.py`). Funnel metrics at every stage, compare artefacts committed for every quality-changing phase — the interview exhibit for "did this change help, and by how much?"
- _append as Phases 1+ land with stronger conviction_

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
- **Silent-empty feeds dominate the failure mode, not exceptions.** 3 of 10 ROADMAP-hardcoded feeds were dead on arrival this morning (federalregister SEC, hn.cz, euvc.com — all `bozo=1`, `entries=[]`, no raised exception). Mitigation: pre-flight smoke test + `RSSConnector.list_new_items` re-raises `bozo_exception` when entries is empty. See [LESSONS L1](LESSONS.md#l1-three-of-the-ten-roadmap-rss-feeds-were-silently-empty-at-0830).
- **15/77 docs in the Phase 0 baseline depend on the RSS-summary fallback, not full extraction.** SEC + VentureBeat 403 trafilatura; RSS `<content:encoded>` saves them *today* but chunks carry truncated signal. A `fetched_via={trafilatura|rss_summary}` counter goes into Phase 2. See [LESSONS L2](LESSONS.md#l2-trafilatura-403s-on-sec--venturebeat-are-invisible-unless-rss-summary-fallback-is-wired).
- **LLM output-shape drift** — mitigated by `safe_llm_json()` ([LESSONS § LLM output-shape handling](LESSONS.md)); still the single most likely production-break.
- **Pinecone single-tenancy** — one global index, metadata-filtered per Aim. Fine at demo scale, needs per-namespace partitioning at multi-tenant scale.
- **No observability beyond stdout funnel metrics** — demo-OK, prod-blocker.
- _append as real fragility shows up during runs_

---

## Update discipline

Driven by [CLAUDE.md § Self-maintenance](../CLAUDE.md#self-maintenance). Each phase completion, scope cut, pivot, or fragile discovery writes into the relevant section above — so at 16:30 this doc *is* the demo script.
