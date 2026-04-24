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

Phase 1 funnels (via FastAPI + BackgroundTask, CEE Aim, `data/compare/phase1_api_version.json`):
- **`mode=force`** (full pipe): `INGESTED=78 → CHUNKED=623 → EMBEDDED=623 → UPSERTED=623 → RETRIEVED=20 → SECTIONS=3 / ITEMS=4` in ~83 s.
- **`mode=incremental`** (after force; Tier 1 seen-set now wired via `save_raw_articles`): `INGESTED=0 (skipped_seen=78) → RETRIEVED=20 → SECTIONS=3 / ITEMS=4` in ~20 s.
- **`mode=cached`** (skip ingest entirely, retrieve against live Pinecone): `RETRIEVED=20 → SECTIONS=2 / ITEMS=4` in ~17 s. The 5× delta vs `force` is the demo story — see [LESSONS L3](LESSONS.md#l3-modecached-latency-is-llm-generate-dominated-not-pipeline-dominated).

Phase 2 funnels (`data/compare/phase2_dedup.json`, CEE Aim, two back-to-back runs):
- **Run 1, `mode=force`** (Tier 3 semantic dedup now gates upsert): `INGESTED=78 → CHUNKED=623 → EMBEDDED=623 → UPSERTED=614 (SEMANTIC_DUPS=9) → RETRIEVED=20 → SECTIONS=3 / ITEMS=4` in ~124 s. The 9 Tier-3 catches were mostly arxiv-abstract boilerplate, not cross-outlet rewrites — see [LESSONS L4](LESSONS.md#l4-tier-3-semantic-dedup-catches-arxiv-more-than-news).
- **Run 2, `mode=incremental`** (Tier 1 URL md5 on top of run 1's seen-set): `INGESTED=1 (skipped_seen=77) → CHUNKED=4 → UPSERTED=4 → RETRIEVED=20 → SECTIONS=3 / ITEMS=4` in ~13 s. **The demo line: 77/78 articles skipped by Tier 1, Digest still emits in 13 s.** The one new ingest was a Forbes.cz entry published between the two runs — proof that `incremental` tracks live feeds, not a failure.

### How the two dedup tiers actually flow (demo narration)

Say this out loud when pointing at the two `data/compare/phase2_dedup.json` funnels:

**Tier 1 — URL dedup, before scraping.** In `pipeline/ingestion.py::ingest_all_sources`, after listing RSS entries we hash each URL (`md5(url)`) and check it against `seen_ids` — which `main.py` builds from `storage.get_seen_article_ids()`, a union of all `article_id`s across every past `data/raw/*.json`. If the hash is already there → **skip the article entirely, don't even call trafilatura.** Only URLs we've never seen get scraped, extracted, and chunked. That's where the "77/78 skipped_seen" number comes from in run 2.

**Tier 3 — semantic dedup, before storing.** New URLs get chunked + embedded as normal. Then in `pipeline/vector_store.py::upsert_chunks`, before upserting each chunk to Pinecone, we run a `top_k=1` query with that chunk's embedding, **filtered to `article_id $ne` this chunk's article_id**. If the top match (from a *different* article) has cosine ≥ 0.93 → skip the upsert, log `semantic_dup of {id}`, bump the `semantic_dups` counter. Chunks that are too similar to something already in Pinecone never get stored. That's the "9 semantic_dups / 623" in run 1.

**Full flow end-to-end:** new URL → Tier 1 hash check → scrape → chunk → embed → Tier 3 Pinecone near-neighbour check → only upsert if it's genuinely new content. Tier 1 is the cheap precheck that saves the trafilatura+embed cost; Tier 3 is the expensive-but-free-because-we-already-have-the-embedding precheck that protects against semantic near-duplicates Tier 1 can't see.

| Verb | Module | Status | One-liner for demo |
|---|---|---|---|
| ingest | `pipeline/ingestion.py::ingest_all_sources` | ✅ Phase 0–2 | 10 RSS feeds behind a `RSSConnector` in a `REGISTRY` dict — adding a Mexican-state-institutions source is one `@register("mexico_gov")` line, not a pipeline rewrite. **Phase 2**: tenacity retries on `_parse_feed` + `_fetch_url` (3 attempts, 1→8 s expo), per-source + per-article try/except. One flaky feed does not kill the run; one bad article does not kill the source. |
| extract | `pipeline/ingestion.py::RSSConnector.fetch` | ✅ Phase 0 | `trafilatura` on the article URL, fall back to RSS `<content:encoded>` when 403 — **saved 15/77 docs on SEC + VentureBeat this morning.** |
| chunk | `pipeline/processing.py::chunk_articles` | ✅ Phase 0–1 | LangChain `RecursiveCharacterTextSplitter(800, overlap=100)`, title prepended so every chunk is self-identifying at rerank time. |
| embed | `pipeline/embedding.py::embed_texts` | ✅ Phase 0–1 | `text-embedding-3-small`, batched ≤100. Swap path to VertexAI `text-embedding-004` with `task_type=RETRIEVAL_DOCUMENT` is one file. |
| store | `pipeline/vector_store.py::upsert_chunks` | ✅ Phase 0–2 | Pinecone serverless, `dim=1536`, chunks tagged with `region` + `source_type` at ingest — **these are filter dimensions, not prompt content.** **Phase 2**: Tier 3 semantic dedup — per-chunk `top_k=1` query with `article_id $ne` filter, skip if cosine ≥0.93, logged as `semantic_dup of {id}`. Tier 1 URL-md5 seen-set lives in `pipeline/storage.py::get_seen_article_ids`. |
| retrieve | `pipeline/retrieval.py::retrieve_relevant_chunks` | ✅ Phase 0–1 | Hybrid retrieval: `{"region": {"$in": [*aim.regions, "Global"]}}` as a Pinecone filter *before* ANN. Global always OR'd in so Global-tagged pieces still serve regional Aims. |
| rerank | `pipeline/retrieval.py::rerank_chunks` | ⏳ Phase 4 | `gpt-4o-mini` JSON call with full Aim in the prompt — 15× cheaper than `gpt-4o` for structured rerank. |
| generate | `pipeline/report.py::generate_digest` | ✅ Phase 0–1 | `gpt-4o-mini`, `response_format={"type":"json_object"}`, `temperature=0.3`. **LLM picks 2–5 section titles per run** — Phase 1 API run chose "Recent Fundraising Activities / Startup Internationalisation Efforts / Insights from Industry Leaders" for the CEE Aim. |

---

## 2. Why these choices — top 3–5 decisions to lead with

Pick the highest-signal entries from [DECISIONS.md](DECISIONS.md) to foreground. Rest stay available for Q&A.

- **Aim's structured fields are Pinecone filter dimensions, not prompt content.** *"Regions aren't prompt decoration — chunks carry `region` + `source_type` metadata at ingest, and retrieval applies `{"region": {"$in": [*aim.regions, "Global"]}}` before ANN. Hybrid retrieval — structured filter ∩ semantic search. The rerank can't overrule what was never retrieved."* Wired since Phase 0 (10 lines of cost, strongest RAG signal in the build).
- **Walking skeleton first.** All 8 verbs ran end-to-end in one script before anything got polished (see `scripts/run_pipeline.py`). Funnel metrics at every stage, compare artefacts committed for every quality-changing phase — the interview exhibit for "did this change help, and by how much?"
- **Three-tier dedup, cheap-to-expensive; implemented the two whose plumbing is free.** *"Tier 1 is exact URL md5 — catches crawl duplicates, the workhorse (77/78 skips on the re-run). Tier 3 is embedding cosine ≥ 0.93 on a different `article_id` — free because we already have the embedding, one extra Pinecone query per upsert. Tier 2 (MinHash LSH) stays talked-about — it's the add I'd wire when daily new-chunk volume makes per-upsert semantic dedup too expensive, somewhere above 100k docs/day."* See [D13](DECISIONS.md#d13-dedup--tier-1--tier-3-live-tier-2-minhash-talked-about).
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

- **Tier 2 MinHash LSH dedup** — `pipeline/dedup.py::minhash_dedup` with `datasketch` (`num_perm=128`, `b=16`, `r=8` for Jaccard ≈ 0.85), ~30 LOC. Tier 1 (URL md5) + Tier 3 (embedding cosine ≥ 0.93) are both live in Phase 2; Tier 2 is the add for when daily new-chunk volume makes per-upsert Pinecone queries too costly (above ~100k docs/day). See [D13](DECISIONS.md#d13-dedup--tier-1--tier-3-live-tier-2-minhash-talked-about).
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
- **Tier 3 semantic dedup's false-positive surface is bigger than the note implies.** 7/9 Phase-2 catches were arxiv-abstract boilerplate matching other arxiv abstracts at cosine 1.000 — legit dupes of the *content we have*, but not the cross-outlet rewrite story. On a denser wire-service source mix the signal would shift; today it's mostly insurance against boilerplate. See [LESSONS L4](LESSONS.md#l4-tier-3-semantic-dedup-catches-arxiv-more-than-news).
- _append as real fragility shows up during runs_

---

## Update discipline

Driven by [CLAUDE.md § Self-maintenance](../CLAUDE.md#self-maintenance). Each phase completion, scope cut, pivot, or fragile discovery writes into the relevant section above — so at 16:30 this doc *is* the demo script.
