# Decisions log

Format per entry: **Decision** — Why — Alternatives rejected — Cost if reversed.

Append new entries at the bottom. Mark superseded rather than deleting.

---

## D1. Pinecone over BigQuery VECTOR_SEARCH

**Decision:** Pinecone serverless as the vector store. Index `aim-chunks`, `dim=1536`, `metric=cosine`.

**Why:**
- Aim uses Pinecone in production (per the brief owner's note)
- Zero GCP setup overhead for the critical path — Pinecone auth is one env var vs a service account with 5 IAM roles
- Dead-simple SDK: `pc.Index("aim-chunks").upsert([...])` / `.query(vector=…)`
- Metadata filters are native (`filter={"region": {"$in": […]}}`) — this is how hybrid retrieval works

**Alternatives rejected:**
- **BigQuery VECTOR_SEARCH** — legitimate for warehouse-side analytics on retrieval patterns; kills Phase 0 velocity.
- **ChromaDB local** — no "real vector DB" thinking, brief notes it as the less impressive choice.
- **In-memory numpy cosine** — kept as Phase-0 fallback only if Pinecone breaks.

**Cost if reversed:** swap `pipeline/vector_store.py` (~30 LOC). Low.

**Interview note:** "Time-to-first-green-run. At Aim scale BigQuery VECTOR_SEARCH is a legitimate choice for warehouse-side analytics; for a one-day demo, Pinecone gets us to a working end-to-end faster, and its metadata filter gives us hybrid retrieval cheaper."

---

## D2. OpenAI embeddings over VertexAI

**Decision:** OpenAI `text-embedding-3-small` (1536 dim).

**Why:**
- One provider for embeddings + LLM simplifies auth, errors, rate-limit tracking
- Stable, cheap, well-documented
- No GCP AI Platform setup in the critical path

**Alternatives rejected:**
- **VertexAI `text-embedding-004` (768 dim)** — task-type asymmetry (`RETRIEVAL_DOCUMENT` at index, `RETRIEVAL_QUERY` at retrieve) legitimately improves precision. Deferred to Phase 5c as an explicit swap; not worth the dim-mismatch risk in Phase 0.

**Cost if reversed:** swap `embedding.py` + recreate Pinecone index at `dim=768` + re-embed everything. Medium.

**Interview note:** "Phase 5c swap if time permits. Task-type asymmetry is genuinely better for retrieval because the model learns that a question isn't an answer. I defaulted to OpenAI for Phase 0 velocity."

---

## D3. Local JSON over Firestore (for aims + digests)

**Decision:** `data/aims/{aim_id}.json`, `data/digests/{digest_id}.json`. `storage.py` is the only file that touches IO.

**Why:**
- Zero setup — works day 1.
- Easy to inspect during debugging: `cat data/digests/*.json | jq`.
- Firestore adds `google-cloud-firestore` + service-account creds + region choice + emulator-vs-cloud decision.

**Alternatives rejected:**
- **Firestore from the start** — matches Aim's stack; setup cost is real (service account, IAM `roles/datastore.user`, region).
- **SQLite** — overkill for two collections.

**Cost if reversed:** `storage.py` changes. Listed as Phase 5a. Low.

---

## D4. FastAPI BackgroundTasks over Pub/Sub + Cloud Run

**Decision:** Pipeline runs in an in-process `BackgroundTasks` handler; client polls `GET /digest/{job_id}` for status.

**Why:**
- No Pub/Sub topic/subscription/worker setup.
- Mimics production async semantics (non-blocking POST + polling GET) in a single process.
- Full pipeline runs in 30–120 s — well within FastAPI lifetime.

**Alternatives rejected:**
- **Pub/Sub + Cloud Run worker** — matches Aim's production architecture; setup cost (~1–2 h) trades directly against source coverage or rerank quality.
- **Synchronous pipeline** — blocks HTTP for minutes, unacceptable UX.

**Cost if reversed:** move `run_pipeline` body into a Pub/Sub subscriber handler. Modest refactor.

**Interview talking point:** the prototype's 1-process model maps onto a split production deployment — `cached` mode is structurally the read-only digest service from that split (Cloud Run Job ingest + Cloud Run Service retrieve/rank). See [D12](#d12-three-mode-digest-trigger-is-a-soft-ingestdigest-split) for why.

---

## D5. Start with ~10 region-weighted sources, not 5 and not 50

**Decision:** Phase 0 hardcodes 10 RSS feeds — ~5 US/global + ~3 Czech + ~2 CEE — so both demo Aims have a non-trivial pool from day one.

**Why:**
- A 5-source start confounds rerank-quality measurement — a thin pool forces rerank to *fragment* content across sections (same article, two angles) rather than rank genuinely distinct candidates. Can't cleanly claim "rerank helped" under those conditions.
- 50 sources = 50 flaky-feed risks in Phase 0; one silently-empty feed eats 20 min.
- Weighting toward the Aims' stated regions means every post-Phase-0 improvement is cleanly attributable (pool isn't the bottleneck). See [LESSONS § Rerank's precondition](LESSONS.md#reranks-precondition).

**Alternatives rejected:**
- **5 sources.** Causes rerank-measurement confounding as above.
- **50 sources (brief's implied number).** Risk > reward for the walking skeleton.

---

## D6. Text-only sources; no video/audio in the live prototype

**Decision:** Live connectors are text-only: RSS (news/blogs/long-tail), Congress.gov JSON, SEC EDGAR JSON, (optionally) Reddit text subreddits. Video (YouTube) and audio (podcasts) are **stubbed** — registered in the connector registry with `NotImplementedError("stubbed — see D6")`.

**Why:**
- Video/audio adds yt-dlp + Whisper transcription. 1-hour audio = 1–5 min transcription latency, GPU-or-API decision, ToS risk on YouTube. Zero marginal architectural signal vs text connectors.
- The *heterogeneity* signal the brief grades on comes from connector *types*, not modality. RSS + Congress JSON + SEC JSON are three genuinely different shapes (parser, metadata, rate-limit behaviour). That's the extensibility story.
- Stubs prove the registry pattern in 30 seconds of demo. Implementing is a 3-hour rabbit hole.

**Alternatives rejected:**
- **Implement `youtube-transcript-api` only** (captions-first, skip Whisper fallback). Defensible but still costs 45 min for a side-show connector.
- **Skip video/audio entirely, no stubs.** Loses the "extensible to new source types" signal. Stubs are cheap insurance.

**Cost if reversed:** add 1–3 live video/audio connectors. Each: 30–90 min depending on captions availability. "What's next with a week."

**Interview note:** "Video and audio connectors are registered but stubbed. Implementation needs yt-dlp plus Whisper which is a 2–3-hour rabbit hole for zero marginal architectural signal — every text connector type already exercises the connector registry, the dedup tiers, and the hybrid retrieval filter. Captions-first via `youtube-transcript-api` would be my first add with more time."

---

## D7. Defer GCP infra entirely to Phase 5

**Decision:** No GCS, BigQuery, Firestore, VertexAI, Cloud Scheduler, Pub/Sub, or Cloud Run in Phases 0–4.

**Why:**
- Brief budgets 30 min for GCP setup; empirically 1–2 h on a fresh account. Already set up locally (`aim-challenge-494220`, europe-west3, credentials.json) — reuse.
- Nothing in the core pipeline *requires* GCP once we use Pinecone + OpenAI.
- Phase 5 swaps are one file each (`storage.py` / `vector_store.py` / `embedding.py`); dual-write pattern keeps local fallback intact.

**Alternatives rejected:**
- **GCP setup first.** User explicit: "make the whole thing work simply first."

---

## D8. `response_format={"type": "json_object"}` for every JSON-producing LLM call

**Decision:** Both rerank and digest-generation use OpenAI JSON mode.

**Why:**
- Eliminates markdown fences and preamble that break `json.loads`.
- Much more reliable than prompt-only JSON coercion.

**Gotcha:** JSON mode requires the prompt to contain the word "JSON" (case-insensitive). Phrase like "Respond with a JSON object matching this schema:" covers it.

**Additional defensive layer:** every structured LLM call routes through `pipeline/_util.safe_llm_json(raw, key, expected_len)` which strips fences, handles JSON-stringified arrays, truncates over-length lists, raises on under-length. See [LESSONS § LLM output-shape handling](LESSONS.md#llm-output-shape-handling).

---

## D9. `article_id = md5(url)` as the Tier-1 dedup key

**Decision:** `article_id = md5(url).hexdigest()`. `storage.get_seen_article_ids()` unions `article_id`s across `data/raw/*.json`.

**Why:**
- Stable across runs.
- No separate "seen" DB — raw-article store is the source of truth.

**Alternatives rejected:**
- **Title+domain fingerprint** — brittle under feed republishes with title tweaks.
- **Separate `data/seen.json`** — doubles the source of truth.

---

## D10. Adopt Aim's real product nouns

**Decision:** `Aim` (monitoring config) and `Digest` (output briefing) per [PRODUCT_NOTES.md](PRODUCT_NOTES.md). Dynamic `sections` chosen by the LLM, not a fixed enum.

**Why:**
- Grounded in screenshots of the live product.
- `Aim` structured fields (`regions`, `monitored_entities`, `update_types`) are **Pinecone metadata filters**, not prompt content — this unlocks hybrid retrieval (structured filter ∩ semantic search). See [ARCHITECTURE § Hybrid retrieval](ARCHITECTURE.md#hybrid-retrieval--structured-filter--semantic-search).
- Dynamic sections match how real digests are shaped — one Aim produces "Opinion leaders / Investments / Product Updates", another produces "Hires & exits / Fundraises / Regulatory".
- Using the product's terminology is a free signal: "I looked at the live app and mirrored your data model."

**Alternatives rejected:**
- **Freeform `thesis: str` field.** Loses filter-dimension signal and cannot support dynamic sections coherently.

---

## D11. `gpt-4o-mini` as default; `gpt-4o` as upgrade path

**Decision:** Both rerank and digest-generation use `gpt-4o-mini`. Switch to `gpt-4o` only if a side-by-side benchmark shows synthesis is weak.

**Why:**
- ~15× cheaper than `gpt-4o` at the time of writing (input $0.15 vs $2.50 per 1M; output $0.60 vs $10).
- Running the pipeline 10× a day is painless at mini cost, painful at `gpt-4o`.
- For structured-output tasks (rerank, digest with dynamic sections) the quality delta is usually small.

**Alternatives rejected:**
- **`gpt-4o` as default.** Strictly better synthesis but premature cost on Phase 0 iteration.
- **Local open-weight model** (Qwen 32B, Llama 3.3 70B). Strong talking point but adds deployment surface; not worth it in a 1-day build.

**Cost if reversed:** change one constant. Trivial.

**Interview note:** "I defaulted to mini because Phase 0 is about proving the pipe. At demo time I'd benchmark both on the same Aim and pick based on measured digest quality, not vibes."

---

## D12. Three-mode digest trigger is a soft ingest/digest split

**Decision:** `POST /aim/{aim_id}/digest?mode=incremental|force|cached`.
- `incremental` (default) — dedup on, ingest only new articles.
- `force` — ignore seen-set, re-ingest everything.
- `cached` — skip ingest entirely, retrieve + rerank + generate against current Pinecone state.

**Why:**
- Demo gift: `cached` runs in 5–10 s; iterating on rerank/prompt during the afternoon window is painless.
- Architectural signal: `cached` mode *is* the read-only digest service from the scaled split architecture (Cloud Run Job ingest on a cron + Cloud Run Service retrieve/rank on-demand). Click `force` then `cached` in the demo — narrate the split.
- Binary `force` from a prior draft was a retrofit the moment a UI existed. Three modes avoids the retrofit.

**Alternatives rejected:**
- **Single mode (always re-ingest).** Slow demo iteration, wastes OpenAI + Pinecone calls.
- **Binary `force: bool`.** Wants to be three-mode the moment there's a UI — demo iteration needs a "skip ingest, just re-run retrieval + rerank" knob. Ship three modes from Phase 1 to avoid the retrofit.

**Cost if reversed:** one endpoint param + a `mode` branch in `run_pipeline`. Trivial.

---

## D13. Dedup — Tier 1 + Tier 3 live; Tier 2 MinHash talked-about

**Decision:** Wire exact URL hash (Tier 1) and embedding cosine > 0.93 (Tier 3). Document MinHash LSH (Tier 2) as the expected-at-scale add; don't implement.

**Why:**
- Tier 1 catches "same URL crawled twice" — common from feed overlap.
- Tier 3 reuses embeddings we already compute for retrieval — *free* in dev cost, catches "AP story rewritten by WSJ + Reuters with same facts". Implementation: one `top_k=1` Pinecone query before each upsert; skip if `score > 0.93`.
- Tier 2 (MinHash LSH) catches "press release reprinted by 5 outlets with minor edits" — common in news. Needs `datasketch` + banding params (`num_perm=128`, `b=16`, `r=8` for Jaccard ≈ 0.85). ~30 LOC.
- Skipping Tier 2 in the prototype: Tier 3 covers most of the same ground in a single-process demo, and Tier 2's sharper win appears at >100k new docs/day where embedding every candidate before indexing becomes expensive.

**Alternatives rejected:**
- **Tier 1 only.** Misses the "two distinct articles on the same event" case the brief specifically calls out. Weak.
- **All three tiers.** Extra 30 LOC + a library for marginal coverage over Tier 3; costs Phase 2 time we'd rather spend on retries.

**Cost if reversed (add Tier 2):** ~30 LOC + `datasketch` dep + Phase 2 retrofit. Low.

**Interview note:** "Three tiers, cheap-to-expensive: exact URL hash catches crawl duplicates, MinHash LSH catches republished press releases, embedding cosine catches rewrites. I wired Tier 1 and Tier 3 — Tier 3 is free because we already have the embedding — and documented MinHash as the add I'd wire when daily new-chunk volume makes semantic dedup too expensive per upsert. That's above ~100k docs/day."

**Update (Phase 2 first measured run, see [LESSONS L4](LESSONS.md#l4-tier-3-semantic-dedup-catches-arxiv-more-than-news)):** Tier 1 is the workhorse (77/78 hits on the re-run). Tier 3 fired on **9/623 chunks** on the first force-run — but ~80% of those were arxiv-abstract boilerplate matching other arxiv abstracts, not the cross-outlet rewrite story the original note sold. Don't oversell Tier 3 in the demo without this qualifier; it's *insurance* that the plumbing exists, not a measured dominant signal at the current source mix.

---

## D14. Vanilla HTML/CSS/JS frontend, no framework

**Decision:** Phase 3 frontend is vanilla ES modules, one CSS file, Google Fonts Inter. No React/Vue/Svelte, no build step.

**Why:**
- The frontend is a 60-minute demo wrapper, not a product. Adding tooling trades directly against pipeline depth.
- Design tokens lifted from startaiming.com ([DESIGN_SYSTEM.md](DESIGN_SYSTEM.md)) — the HoE should feel at home in the UI.
- No build step = changes reload instantly; zero surprise at demo time.

**Alternatives rejected:**
- **Next.js / Vite + React.** Impressive CV signal; negative ROI for 60 min of work.
- **Tailwind CDN.** Smaller than the above, but still extra dependency for what three design tokens solve.

**Cost if reversed:** rewrite `static/` under a framework. 1–3 h.

---

## D15. Auth is out of scope; `user_id` is trust-the-client

**Decision:** `user_id` is a required field on `Aim` and is stored with every record, but no endpoint authenticates it. Real auth (OAuth, JWT, per-user Pinecone namespaces, row-level security) is Phase 6.

**Why:**
- Brief is about the *pipeline*. Auth is undifferentiated plumbing — 45 min on JWT middleware trades against source coverage or rerank quality.
- Multi-tenant data *shape* is correct (`user_id` on every Aim, `GET /aims?user_id=…`). Adding auth later is a one-day follow-up with no schema changes.
- Local JSON + shared Pinecone = single-tenant anyway until Phase 5a (Firestore) and Phase 5d (Pinecone namespaces). Bolting auth on top would be theatre.

**Alternatives rejected:**
- **Toy API-key gate.** Protects nothing in a local demo; pure ceremony.
- **Full OAuth/JWT.** 60–90 min minimum for a pipeline-focused interview.

**Cost if reversed:** per-endpoint `user: User = Depends(current_user)` + replace `user_id` fields with `user.id`. Medium-low.

**Interview note:** "Deliberately out of scope. Data shape is multi-tenant; auth is a one-day follow-up once Phase 5a moves aims to Firestore — JWT dependency + per-user Pinecone namespace. Putting auth on top of local-JSON single-tenant storage would be theatre."
