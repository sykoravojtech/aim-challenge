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

Phase 4 funnels (CEE Aim, cached Pinecone state, `data/compare/phase4_*.json`):
- **`rerank_only`** (retrieve 30 → rerank to 15 → generate): `RETRIEVED=30 → RERANKED=15 (mean=8.40) → SECTIONS=2 / ITEMS=3` in ~10 s.
- **`full`** (retrieve 30 → rerank to 15 → MMR λ=0.7 to 10 → generate): `RETRIEVED=30 → RERANKED=15 (mean=7.60) → DIVERSIFIED=10 → SECTIONS=3 / ITEMS=4` in ~12 s.
- **Ablation diff (`scripts/compare_digests.py rerank_only full`)**: MMR expands sections 2→3, items 3→4, surfaces a third section ("AI Landscape Developments") the rerank-only run collapsed into "Startup Ecosystem Insights"; URL Jaccard 0.75 (3/4 shared); mean relevance drops 8.00→7.00 — **the expected rerank/diversity trade-off**, made visible by the compare tool. See [LESSONS L5](LESSONS.md#l5-mmr-over-rerank-trades-mean-relevance-for-section-coverage).
- **Baseline diff (`phase2_dedup.json` vs `phase4_full.json`)**: same section/item counts (3/4) at a different topic split — the rerank+MMR stack replaces "Recent Fundraising Activities / Startup Internationalisation / AI and Engineering in CEE" with "Funding Announcements / Startup Internationalisation / AI Landscape Developments"; URL Jaccard 0.60 (3/5 shared), host Jaccard 1.00, mean relevance 7.25 → 7.00.

### Phase 4 demo narration (say this aloud, honest read)

The compare table is the exhibit — don't oversell it. On a thin CEE pool with one dominant on-Aim host, the rerank+MMR stack is **architecturally right but data-thin**: the baseline vs `full` diff shows 1 swapped URL and a ~0.25-point mean-relevance drop, not a revolution. That's exactly what the standing idiom [LESSONS § Rerank's precondition](LESSONS.md#reranks-precondition) predicts — rerank earns its pay only when the candidate pool has ≥3× as many distinct on-Aim articles as final items, and CEE's hits-per-host skew isn't there yet.

Demo line: *"Multi-stage ranking is the brief's graded-dimension #1, so I wired it — retrieve 30 → rerank 15 → MMR to 10 — and exposed the ablation via `compare_digests.py` so you can see what each stage buys. On this pool, MMR's trade-off is visible (mean relevance 8.00 → 7.00, sections 2 → 3), which is textbook. The lever that makes it measurably better isn't rerank tuning — it's 3× the source set for this Aim. That's a named item in § 5 'what's next'."*

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
| ingest | `pipeline/ingestion.py::ingest_all_sources` + `mirror_raw_to_bq` + `mirror_raw_to_gcs` | ✅ Phase 0–5 | 10 RSS feeds behind a `RSSConnector` in a `REGISTRY` dict — adding a Mexican-state-institutions source is one `@register("mexico_gov")` line, not a pipeline rewrite. **Phase 2**: tenacity retries on `_parse_feed` + `_fetch_url` (3 attempts, 1→8 s expo), per-source + per-article try/except. One flaky feed does not kill the run; one bad article does not kill the source. **Phase 5**: each ingest writes raw rows to BigQuery (`aim_pipeline.raw_articles`, auto-created) and GCS bronze (`gs://aim-challenge-raw-494220/raw/{date}/{job_id}.json`) — Cloud Run run of the `saas-ai-legislation` Aim wrote 78 rows + 349 KB JSON in ~6 s. |
| extract | `pipeline/ingestion.py::RSSConnector.fetch` | ✅ Phase 0 | `trafilatura` on the article URL, fall back to RSS `<content:encoded>` when 403 — **saved 15/77 docs on SEC + VentureBeat this morning.** |
| chunk | `pipeline/processing.py::chunk_articles` | ✅ Phase 0–1 | LangChain `RecursiveCharacterTextSplitter(800, overlap=100)`, title prepended so every chunk is self-identifying at rerank time. |
| embed | `pipeline/embedding.py::embed_texts` | ✅ Phase 0–1 | `text-embedding-3-small`, batched ≤100. Swap path to VertexAI `text-embedding-004` with `task_type=RETRIEVAL_DOCUMENT` is one file. |
| store | `pipeline/vector_store.py::upsert_chunks` + `pipeline/storage.py` (Firestore) + `pipeline/ingestion.py::mirror_raw_to_bq` / `mirror_raw_to_gcs` | ✅ Phase 0–5 | Pinecone serverless, `dim=1536`, chunks tagged with `region` + `source_type` at ingest — **these are filter dimensions, not prompt content.** **Phase 2**: Tier 3 semantic dedup — per-chunk `top_k=1` query with `article_id $ne` filter, skip if cosine ≥0.93, logged as `semantic_dup of {id}`. Tier 1 URL-md5 seen-set lives in `pipeline/storage.py::get_seen_article_ids`. **Phase 5**: Aims + Digests dual-written to Firestore (`USE_FIRESTORE=1`, default db, europe-west3). Raw articles mirrored to BigQuery `aim_pipeline.raw_articles` + GCS `gs://aim-challenge-raw-494220/raw/{date}/{job_id}.json`. Pinecone is still the vector store; Firestore/BQ/GCS are the document-side stores. |
| retrieve | `pipeline/retrieval.py::retrieve_relevant_chunks` (+ `build_query_text`/`build_query_filter` primitives wired into `main.py::run_pipeline`) | ✅ Phase 0–4 | Hybrid retrieval: `{"region": {"$in": [*aim.regions, "Global"]}}` as a Pinecone filter *before* ANN. Global always OR'd in so Global-tagged pieces still serve regional Aims. **Phase 4**: `top_k=30` with `include_values=True` so MMR has the vectors. |
| rerank | `pipeline/retrieval.py::rerank_chunks` + `mmr_diversify` | ✅ Phase 4 | `gpt-4o-mini` JSON call assigns 0–10 per chunk with full Aim in the prompt (15× cheaper than `gpt-4o` for structured rerank); response goes through `safe_llm_json`, hard-fail falls back to vector order. MMR (`λ=0.7`) over Pinecone-returned embeddings picks top 10 from the reranked 15 — biases against near-duplicates the rerank kept. Funnel: **retrieve 30 → rerank 15 → MMR 10**. |
| generate | `pipeline/report.py::generate_digest` | ✅ Phase 0–1 | `gpt-4o-mini`, `response_format={"type":"json_object"}`, `temperature=0.3`. **LLM picks 2–5 section titles per run** — Phase 1 API run chose "Recent Fundraising Activities / Startup Internationalisation Efforts / Insights from Industry Leaders" for the CEE Aim. |

---

## 2. Why these choices — top 3–5 decisions to lead with

Pick the highest-signal entries from [DECISIONS.md](DECISIONS.md) to foreground. Rest stay available for Q&A.

- **Aim's structured fields are Pinecone filter dimensions, not prompt content.** *"Regions aren't prompt decoration — chunks carry `region` + `source_type` metadata at ingest, and retrieval applies `{"region": {"$in": [*aim.regions, "Global"]}}` before ANN. Hybrid retrieval — structured filter ∩ semantic search. The rerank can't overrule what was never retrieved."* Wired since Phase 0 (10 lines of cost, strongest RAG signal in the build).
- **Walking skeleton first.** All 8 verbs ran end-to-end in one script before anything got polished — kept verbatim as `scripts/phase0_skeleton.py` (frozen exhibit; the runtime path is now `main.py` + `pipeline/*.py`). Funnel metrics at every stage, compare artefacts committed for every quality-changing phase — the interview exhibit for "did this change help, and by how much?"
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
- **VertexAI embeddings** — cut from Phase 5. `text-embedding-004` at `dim=768` would force a Pinecone index recreation; cost of reindex dominates the "GCP-native embeddings" talking point. `pipeline/embedding.py` is a one-file swap if it ever matters. Cloud Run calls OpenAI fine.
- **Typed digest items** (per-`item_type` structured fields like `quote`+`attribution`, `entity`+`amount`) — cut from Phase 5. Product-shape work, not infra; doesn't move the "runs on GCP" deliverable. Live Aim app does this — it's a named "what's next" item below.
- **Cloud Scheduler / cron-driven digests** — out of scope. Digest trigger stays manual via `POST /aim/{id}/digest?mode=force`. Demo line: *"Scheduling is a 5-line Cloud Scheduler → Cloud Run HTTP target when it matters; orthogonal to the pipeline architecture."*
- **More source feeds** — not the point. Extendability is shown by the `@register` connector registry, not by adding RSS URLs. ~10 live feeds + 5 registered stubs (Reddit/X/LinkedIn/YouTube/Podcast) is the shape.
- **Auth** — `user_id` is trust-the-client. See [D15](DECISIONS.md#d15-auth-is-out-of-scope-user_id-is-trust-the-client).

_Append new cuts below as they happen._

---

## 4b. GCP deployment — services, settings, and why

**Phase 5 scaffold. The Phase 5 agent fills the `<FILL>` placeholders on successful deploy; everything else here is committed state.** The HoE explicitly asked for this — expect Q&A to probe every row of the table below.

**One-paragraph architecture blurb** (for the "walk me through your GCP setup" opener):

> *"Cloud Run serves the FastAPI app and the static frontend as a single container. Firestore holds aims and digests (swapped in behind `pipeline/storage.py` behind `USE_FIRESTORE=1`). BigQuery's `aim_pipeline.raw_articles` table gets every ingested article for analytical SQL queries. GCS mirrors the raw JSON per-job for reprocessing. Secrets — OpenAI and Pinecone keys — live in Secret Manager, mounted as env vars by Cloud Run, never baked into the image. The runtime service account has least-privilege IAM: Firestore User + BigQuery Data Editor/Job User + Storage Object Admin + Secret Manager Secret Accessor, nothing else."*

**Services wired (all region `europe-west3` unless noted):**

| Service | Resource | Purpose | Q&A answer |
|---|---|---|---|
| **Cloud Run** | service `aim` | Hosts FastAPI (API + static frontend), min-instances=0, max=3 | *"Stateless container, scales to zero, one `gcloud run deploy` command redeploys. Frontend and API are one service — FastAPI `mount('/', StaticFiles(...))` at `main.py:163`."* Deployed URL: **https://aim-645297577758.europe-west3.run.app** |
| **Firestore** | database `(default)`, Native mode | Aims + digests storage, replaces `data/aims/*.json` + `data/digests/*.json` on Cloud Run | *"Native mode, single region. Dual-write behind `USE_FIRESTORE` flag during migration — local JSON stays as pre-deploy dry-run substrate. Firestore was the right pick over Cloud SQL because we have document-shaped data (Aim and Digest are Pydantic models), no joins, and it has an always-free tier at 1 GiB."* |
| **BigQuery** | dataset `aim_pipeline`, table `raw_articles` | Analytical store for every ingested article | *"Demo query: `SELECT source_type, region, COUNT(*) FROM raw_articles WHERE DATE(ingested_at) = CURRENT_DATE() GROUP BY 1,2` — per-source coverage this week. Local `data/raw/*.json` stays as dedup truth; BigQuery is the read-path for analytics, not the write-path for the pipeline."* |
| **Cloud Storage** | bucket `aim-challenge-raw-494220`, **region `us-central1`** | Raw-article JSON archive, `gs://.../raw/{date}/{job_id}.json` | *"Bucket is in `us-central1` deliberately — GCS always-free 5 GB tier is only in the three US regions. No cross-region read path exists (this bucket is write-only from ingest, never read by BigQuery or Firestore), so colocation cost is zero. Bronze layer for reprocessing — if we change extraction logic tomorrow, we can replay from raw."* |
| **Secret Manager** | `openai-api-key`, `pinecone-api-key` | Runtime secret injection into Cloud Run | *"Never bake secrets into the image. Cloud Run `--set-secrets=OPENAI_API_KEY=openai-api-key:latest,PINECONE_API_KEY=pinecone-api-key:latest` mounts them as env vars at container start. SA has per-secret `Secret Accessor`, not project-wide."* |
| **Artifact Registry** | Docker repo `aim-images` | Container image store for Cloud Run | *"`europe-west3-docker.pkg.dev/aim-challenge-494220/aim-images/aim:latest` — `gcloud builds submit` pushes, Cloud Run pulls. Build + push in 1m28s on Cloud Build's free tier."* |
| **Cloud Build** | (implicit, triggered by `gcloud run deploy --source .` or `gcloud builds submit`) | Container build from `Dockerfile` | *"Free tier is 120 build-minutes/day, one deploy ≈ 2 min. Not pinned to a trigger — manual deploy is fine at one-day-challenge scale."* |
| **IAM runtime SA** | `aim-pipeline-sa@aim-challenge-494220.iam.gserviceaccount.com` | Cloud Run runtime identity, also used by local code via `credentials.json` | *"Six roles: Cloud Datastore User (covers Firestore Native — confusingly named), BigQuery Data Editor, BigQuery Job User, Storage Object Admin, Secret Manager Secret Accessor, Vertex AI User (unused, legacy from earlier scope). Least-privilege across the whole build."* |

**Settings worth naming aloud in Q&A:**

- **Cloud Run `--min-instances=0`.** Cold starts are the demo risk, but min=1 would bill continuously — free tier demands 0. Keep `uvicorn --port 4444` warm locally as fallback if the Cloud Run URL ever cold-starts mid-demo.
- **`GOOGLE_APPLICATION_CREDENTIALS` is NOT set in the container.** Cloud Run uses the runtime SA via Application Default Credentials. The `credentials.json` at repo root is local-dev only, gitignored.
- **`USE_FIRESTORE=1` in Cloud Run, `USE_FIRESTORE=0` locally** for pre-deploy dry-runs. `pipeline/storage.py` branches on this flag. Local fallback never breaks.
- **GCS bucket public access prevention: enforced on.** Bronze layer is internal-only; no web-hosting use case.
- **No Object Versioning, no Soft Delete on the bucket.** Both would count against the 5 GB free tier.
- **No Container Scanning on Artifact Registry.** Per-scan billing, not free tier.

**Funnel metrics from the Cloud Run deployment** (run once post-deploy, paste here):

```
<FILL by Phase 5 agent after first successful end-to-end digest via Cloud Run URL>
```

**"What about [service we don't use]?" cheat sheet:**

- *VertexAI embeddings* → cut, reasoning in § 4 above.
- *Cloud Scheduler* → cut, reasoning in § 4 above.
- *Pub/Sub* → verbalised in scalability commentary ([CLAUDE.md § Brief's four suggestions](../CLAUDE.md)), not wired. `BackgroundTasks` has identical semantics at single-instance scale; Pub/Sub earns its pay across multiple Cloud Run instances fanning out per-stage. ~60 LOC + topic/sub config when we need it.
- *Cloud Tasks* → verbalised for per-source rate limiting (mentioned in the scalability bullet). Not wired because `tenacity` + per-source try/except is enough at 10-feed scale.
- *Cloud SQL / AlloyDB* → not a fit — document shape (Aim, Digest) matches Firestore; no joins; no transactional writes outside a single Aim document.
- *Memorystore (Redis)* → not needed; Pinecone is already the retrieval-layer cache, Firestore document reads are fast and within free-tier quota.
- *Logging / Monitoring / Error Reporting* → Cloud Run emits stdout to Cloud Logging automatically. Not set up alerts or dashboards — demo scale, not prod.

---

## 5. What's next with a week — named modules, named line counts

*This is the section weak candidates hand-wave on.* Every bullet must name a file, a function, and a rough line count.

Seed candidates (expand + sharpen as the day goes):

- **Tier 2 MinHash LSH dedup** — `pipeline/dedup.py::minhash_dedup` with `datasketch` (`num_perm=128`, `b=16`, `r=8` for Jaccard ≈ 0.85), ~30 LOC. Tier 1 (URL md5) + Tier 3 (embedding cosine ≥ 0.93) are both live in Phase 2; Tier 2 is the add for when daily new-chunk volume makes per-upsert Pinecone queries too costly (above ~100k docs/day). See [D13](DECISIONS.md#d13-dedup--tier-1--tier-3-live-tier-2-minhash-talked-about).
- **PodcastConnector** — `pipeline/sources/podcast.py` via registry, ~120 LOC (yt-dlp + Whisper batch).
- **Real Pub/Sub fan-out** — topic-per-stage + DLQ, ~60 LOC + GCP config.
- **BigQuery VECTOR_SEARCH** as an alternate `VectorStore` implementation — ~50 LOC swap in `pipeline/vector_store.py`.
- **3× the CEE source set to unlock rerank+MMR's measurable gain** — today the CEE Aim's on-Aim pool is dominated by one host, so the Phase 4 ablation shows the trade-off but not the precision win. Add ~15 CEE RSS feeds (ČT24, seznamzpravy.cz, CzechCrunch EN, reflex.cz, e15.cz, denikn.cz, Visegrad Insight, 150sec, Emerging Europe, SeeNews, kafkadesk.org, intellinews CEE, budapesttimes, romania-insider, sofiaglobe) via the existing `@register("rss")` registry. Pure config change, ~20 LOC in `pipeline/ingestion.py::SOURCES`. This is the lever that moves the needle on rerank's precision — see [LESSONS § Rerank's precondition](LESSONS.md#reranks-precondition).
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
- **Rerank+MMR is architecturally right but data-thin at today's pool size.** CEE Aim's post-region-filter pool has 1 dominant host; rerank/MMR's precision win is conditional on ≥3× candidate diversity (see [LESSONS § Rerank's precondition](LESSONS.md#reranks-precondition)). The Phase 4 ablation exposes the trade-off honestly (mean 8.00→7.00) rather than faking a gain. Not a break — a calibrated-expectation item. Lever is source expansion, not rerank tuning.
- **Cloud Run → Pinecone roundtrip latency makes `incremental`/`force` mode slow on the deployed service.** ~8 s per Tier-3 semantic-dedup query from europe-west3 to Pinecone's AWS region — a 78-article ingest takes ~40 min on-cluster vs ~1 min on the laptop. BQ + GCS writes still land in ~6 s. Demo the deployed URL with `cached` mode (~30 s cold-start). Richer framing + three-layer fix plan in the narration block below. See [LESSONS L6](LESSONS.md#l6-cloud-run-europe-west3--pinecone-serverless-has-610-s-per-query-roundtrips).
- _append as real fragility shows up during runs_

### L6 demo narration (Cloud-Run-to-Pinecone latency + scalability honesty)

If the HoE asks "why is it slow on Cloud Run" or probes the brief's "thousands of docs/day" scalability axis, say this aloud — don't hide it, name it clean and give the three-layer fix path:

> *"I picked Pinecone to match your stack and Cloud Run `europe-west3` because the brief is written in Czechia. Those two good choices collide: the Pinecone index I created is in AWS `us-east-1`, so every Tier-3 semantic-dedup query is a cross-cloud HTTPS roundtrip. On the laptop that's ~150 ms; on Cloud Run it's **6–10 s per query**, amplified by Cloud Run's default CPU-throttling-between-requests behaviour for FastAPI BackgroundTasks. Consequence: a 78-article ingest that completes in ~90 s locally takes ~40 min on the deployed service. I did not try to hide this — the deployed URL demos `cached` mode (retrieval + generate only, 30 s), `force` and `incremental` demo on the laptop.*
>
> *You're absolutely right that 78 articles in 40 min is the opposite of the scalability answer the brief is asking for. The fix is three layers, and only the first is about Pinecone:*
>
> *1. **Colocate Pinecone.** Pinecone serverless runs natively on GCP — I'd recreate the index in `europe-west4` (Netherlands, same Google backbone as Frankfurt). ~5–15 ms per query instead of 6–10 s. ~30 min reindex of ~620 chunks, zero code change. This is the 500× win and it's a `gcloud`-style infra change, not an architectural pivot.*
>
> *2. **Batch the Tier-3 check.** Even with fast queries, per-chunk sequential doesn't scale. Replace `index.query(top_k=1)` per chunk with one `index.fetch(ids=[...])` across the candidate article_ids already in the seen-set, then cosine in-memory. O(chunks) queries → O(1). ~60 min in `pipeline/vector_store.py::upsert_chunks`.*
>
> *3. **Move ingest to Cloud Run Jobs, not BackgroundTasks.** BackgroundTasks inherit the request-scoped CPU of the parent HTTP handler — wrong primitive for batch work. Cloud Run Jobs are designed for this: full CPU for the duration, parallel workers, Pub/Sub-triggered. That's also how per-source rate limiting falls into place (one worker per source + Cloud Tasks). Afternoon of work to wire — named path from "10 sources" to "10,000".*
>
> *With #1 alone (30 min), today's design handles the brief's stated low-thousands/day comfortably. With all three, tens-of-thousands/day is in reach. The reason I didn't ship them today is that the Phase 5 goal was 'deployed URL, with the GCP services you asked about actually receiving writes' — Firestore, BigQuery, GCS are all live and verified. The Pinecone colocation is the 31st-minute item, which by the 30-min rule gets called out as what's next, not burned into today's clock."*

**One-command mitigation available right now** (still free-tier at demo scale): `gcloud run services update aim --region=europe-west3 --no-cpu-throttling`. Removes the CPU-freeze between polls. Probably cuts the 40-min ingest to ~3 min without touching Pinecone. Demo-safety-net.

---

## Update discipline

Driven by [CLAUDE.md § Self-maintenance](../CLAUDE.md#self-maintenance). Each phase completion, scope cut, pivot, or fragile discovery writes into the relevant section above — so at 16:30 this doc *is* the demo script.
