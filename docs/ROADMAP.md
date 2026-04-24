# Roadmap — the phased build plan

**The most important doc in the repo.** Follow phase order. Do not start a phase until the previous one runs end-to-end. If a layer blocks >30 min, stub/degrade and keep the skeleton running.

The clock is firm (see [§ Clock-based checkpoints](#clock-based-checkpoints)); phase scope flexes within it. A boring working pipeline by noon beats any deep feature half-built at 17:00.

Terminology (**Aim** = monitoring config, **Digest** = output) — see [PRODUCT_NOTES.md](PRODUCT_NOTES.md).

---

## Pre-Phase-0 checklist (08:00–08:15)

Before any code runs:

- [ ] `.env` in repo root with `OPENAI_API_KEY`, `PINECONE_API_KEY`, `PINECONE_INDEX=aim-chunks` (+ GCP vars if Phase 5 is in scope)
- [ ] `credentials.json` (GCP service account with Firestore / BigQuery / GCS / Secret Manager / Artifact Registry / Cloud Run Admin roles) in repo root — local dev only; deployed Cloud Run service uses its own runtime SA via ADC
- [ ] `git check-ignore -v .env credentials.json` — both must print a match before the first commit
- [ ] Smoke-test keys: `uv run python -c "from openai import OpenAI; OpenAI().models.list().data[0].id"` and `uv run python -c "from pinecone import Pinecone; import os; print(Pinecone(api_key=os.environ['PINECONE_API_KEY']).list_indexes().names())"`
- [ ] Pinecone serverless index `aim-chunks` must exist (`dim=1536`, `metric=cosine`). Create via the Pinecone UI if missing — takes 30 s.

If any smoke-test fails, raise with the HoE before 08:30 — don't debug keys alone.

---

## The two demo Aims

The pipeline must produce **two contrasting digests** from the same pipe, demoed side-by-side. Picked to hit the brief's exact examples (legislation-tracker-for-SaaS-AI, CEE founder monitoring).

```python
AIMS = [
  {
    "aim_id": "saas-ai-legislation",
    "title": "SaaS-AI US Legislative Watch",
    "summary": [
      "Track US bills, hearings, and regulatory filings that could affect SaaS and AI companies",
      "Monitor SEC enforcement activity against AI product claims",
      "Follow Congressional committee commentary on AI model regulation",
    ],
    "monitored_entities": ["SaaS companies", "AI companies", "SEC", "US Congress"],
    "regions": ["US", "Global"],
    "update_types": ["Legislation", "Regulatory", "Enforcement", "Hearings"],
  },
  {
    "aim_id": "cee-founder-media",
    "title": "Czech & CEE Founder Media Monitoring",
    "summary": [
      "Track every mention of Czech and CEE founders in regional media",
      "Monitor fundraising and product announcements from CEE AI startups",
      "Follow VC fund commentary on the CEE early-stage market",
    ],
    "monitored_entities": ["Founders", "Companies", "VC funds"],
    "regions": ["Czechia", "Slovakia", "CEE"],
    "update_types": ["News", "Announcements", "Reports", "Media Mentions"],
  },
]
```

Why two: same pipe, two personas, completely different outputs — proves personalisation isn't a prompt trick.

---

## Source plan — text-only, heterogeneous, 3+ connector types live

**Heterogeneity (the graded axis) comes from connector *types*, not feed count.** Three connector types live with good metadata beats ten RSS feeds.

### Live connectors (build these)

| Connector | Sources | Why | Priority |
|---|---|---|---|
| `RSSConnector` | ~10 RSS feeds tagged by region + source_type | Covers news, blogs, long-tail company sites — "long-tail" in the brief maps to "anywhere that publishes RSS" | Phase 0 |
| `CongressConnector` | `api.congress.gov` bills + hearings (requires free API key) | Hits the brief's #1 example ("US Congress filings") — legislation text + summaries | Phase 0 if key available, else Phase 2 |
| `SECConnector` | `data.sec.gov/submissions/CIK{n}.json` + full-text-search endpoint | Hits "SEC filings" from the brief; JSON API, User-Agent required | Phase 2 |

### Stubbed connectors (register, don't build)

Each is a `class XConnector(BaseConnector): pass` with a `list_new_items()` that raises `NotImplementedError("stubbed — see DECISIONS D-scope-sources")`. The class registration is the signal; implementing is the rabbit hole.

- `RedditConnector` — praw/JSON API. Low effort if time; a clear afternoon add.
- `XConnector` — v2 API with paid bearer. "Stub; would wire if given a key."
- `LinkedInConnector` — no clean public API. "Stub; flagged as legal/ToS risk."
- `YouTubeConnector` — yt-dlp + `youtube-transcript-api`. "Stub; deferred with podcasts as the video/audio track."
- `PodcastConnector` — RSS + Whisper. "Stub; video/audio is what's next with a week."

### RSS feed list (Phase 0 hardcode)

Weighted to serve both Aims — if one Aim's pool is thin, the other should still be rich:

```python
SOURCES = [
  # US tech + VC + SaaS/AI coverage (serves SaaS-AI legislation Aim)
  {"url": "https://techcrunch.com/feed/", "source_type": "news", "region": "US"},
  {"url": "https://news.ycombinator.com/rss", "source_type": "news", "region": "Global"},
  {"url": "https://arxiv.org/rss/cs.AI", "source_type": "research", "region": "Global"},
  {"url": "https://www.theverge.com/rss/index.xml", "source_type": "news", "region": "US"},
  {"url": "https://venturebeat.com/feed/", "source_type": "news", "region": "US"},
  # US policy + legislation (serves SaaS-AI legislation Aim)
  {"url": "https://www.federalregister.gov/documents/search.rss?conditions%5Bagencies%5D%5B%5D=securities-and-exchange-commission", "source_type": "regulatory", "region": "US"},
  # CEE founder + VC media (serves CEE Aim)
  {"url": "https://cc.cz/feed/", "source_type": "news", "region": "Czechia"},
  {"url": "https://www.forbes.cz/feed/", "source_type": "news", "region": "Czechia"},
  {"url": "https://hn.cz/feed", "source_type": "news", "region": "Czechia"},
  {"url": "https://www.euvc.com/feed/", "source_type": "news", "region": "CEE"},
]
```

Smoke-test each feed with `feedparser.parse(url).entries` before committing — silent-empty feeds (HTTP 404 served as HTML, `bozo=1` but no exception raised) are a known trap. See [LESSONS § feedparser silent failures](LESSONS.md#feedparser-silent-failures).

---

## Subagent parallelization policy

Single Claude session throughout. No git worktrees — merge thrash beats time savings at this scope. Parallelism comes from **fanning out subagents in one message** whenever the work touches different files.

- **Fan out when:** independent files, <5 min scaffolding each, or pure standalone tools (e.g. `scripts/compare_digests.py`). Spawn N subagents in one message, wait for all, main session integrates.
- **Stay solo when:** wiring the integration (Phase 0), debugging a live pipeline, eyeballing compare artefacts. The "what surprised me" loop feeds LESSONS.md — don't delegate it.
- **How to brief a subagent:** file path, exact spec from this roadmap / ARCHITECTURE.md, acceptance criterion, "return the file contents, don't commit." Main session reviews + commits.

Per-phase guidance is inline under **Subagent fan-out:** in each phase below.

---

## Phase 0 — Walking skeleton (60–90 min) ⭐ highest priority

**Goal:** One Python script that runs the entire pipeline end-to-end for **one** hardcoded Aim (pick the CEE one — more concrete, faster smoke test) and prints a Digest JSON.

**Non-negotiable: hybrid retrieval is wired from day one.** Chunks carry `region` + `source_type` metadata; retrieval passes `filter={"region": {"$in": aim.regions + ["Global"]}}`. Cost: 10 lines. Payoff: the single strongest RAG signal in the whole build — Aim's structured fields become filter dimensions rather than prompt content. See [DECISIONS D10 + D11](DECISIONS.md#d10-adopt-aims-real-product-nouns).

No FastAPI. No dedup. No rerank. No dynamic sections yet (flat digest OK if the LLM struggles on section titles first time).

**Deliverable:** `scripts/phase0_skeleton.py` (single file, frozen as interview exhibit after Phase 1 modularises) and a `safe_llm_json(raw, key, expected_len)` helper living in `pipeline/_util.py`.

**Checklist:**
- [x] Create Pinecone index `aim-chunks` (`dim=1536`, `cosine`) if not already live
- [x] Hardcode both Aims from the § above (even though Phase 0 only runs one, having both defined early means Phase 1 can demo the contrast without retrofitting)
- [x] Hardcode the 10 RSS sources from the § above, smoke-tested
- [x] `RSSConnector` with `list_new_items()` + `fetch()` — registered in a `REGISTRY` dict even though it's the only live one, so the extensibility pattern is present from day one
- [x] For each article URL: `trafilatura.fetch_url` + `.extract`; skip if <200 chars; fall back to RSS `entry.summary` on empty
- [x] Chunk with `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)`, title prepended
- [x] Embed with `text-embedding-3-small` (batch ≤100)
- [x] Upsert to Pinecone: `id = uuid4()`, `metadata = {article_id, source_url, source_feed, title, text[:1000], source_type, region, published_ts, chunk_index}` — `region` + `source_type` mandatory on every chunk; `published_ts` is epoch-seconds (fallback to ingest-time) so Phase 4's recency filter has a number on the vector
- [x] Retrieve: `build_query_text(aim)` → embed → `index.query(top_k=20, filter={"region": {"$in": [*aim.regions, "Global"]}})`
- [x] Generate: one `gpt-4o-mini` call with `response_format={"type":"json_object"}`, `temperature=0.3`, system "senior market intelligence analyst producing a personalised digest", user prompt embeds the full structured Aim + top-20 chunks, returns `{headline, date_range, sections:[{title, items:[{title, body, source_urls, source_count, item_type, relevance_score}]}]}`
- [x] Log funnel metrics at every stage: `INGESTED=N, EXTRACTED=N, CHUNKED=N, EMBEDDED=N, UPSERTED=N, RETRIEVED=N`
- [x] `print(json.dumps(digest, indent=2))`

**Done when:** `uv run python scripts/phase0_skeleton.py` prints a valid Digest JSON whose `sections[*].items[*].source_urls[*]` are real, ingested article URLs.

**Subagent fan-out:** one early only — single message, parallel `WebFetch` (or one Explore agent) to smoke-test all 10 RSS URLs and report which have non-empty `.entries` with body content. Saves ~10 min vs serial validation and catches silent-empty feeds (see LESSONS). Everything else in Phase 0 is solo — integration debugging is where *you* learn the shape, and the HoE will ask "what surprised you."

**Save baseline:** `data/compare/phase0_walking_skeleton.json`. Commit.

**Stop-signs (skip and come back):**
- Pinecone auth broken → in-memory numpy cosine; fix Pinecone at phase end
- Trafilatura 403s on a site → RSS `entry.summary` fallback; one source dropped is fine
- LLM returns `sections: []` → accept flat digest for now, debug prompt in Phase 1
- Congress API key not yet provisioned → skip `CongressConnector`, ship RSS only

**Do NOT yet:** FastAPI, multiple aims, dedup, LLM rerank, complex logging, per-source retries beyond a bare try/except.

---

## Phase 1 — FastAPI + Aim CRUD + three-mode trigger (60 min)

**Goal:** Expose the pipeline via HTTP with full Aim CRUD and the three-mode trigger. No retrofits later.

Binary `force` flags want to be three-mode the moment there's a UI (demo iteration needs a "skip ingest, just re-run retrieval + rerank" knob). Ship three modes from Phase 1 — retrofit cost is ~15 min once a UI exists. CRUD-complete backend means Phase 3 frontend stays pure presentation.

**Deliverable:** `main.py` + split `pipeline/` module + `models/schemas.py` + `pipeline/storage.py`.

**Checklist:**
- [ ] Split `scripts/phase0_skeleton.py` into `pipeline/{ingestion,processing,embedding,vector_store,retrieval,report,storage}.py` (leave the Phase 0 script in place as a frozen exhibit)
- [ ] `models/schemas.py`: `Aim`, `AimCreate`, `DigestItem`, `DigestSection`, `Digest` (Pydantic v2). Schema per [PRODUCT_NOTES](PRODUCT_NOTES.md).
- [ ] `pipeline/storage.py`: local JSON IO under `data/aims/`, `data/digests/`, `data/raw/`
- [ ] Endpoints:
  - [ ] `POST /aim` → `AimCreate` → save + return `{aim_id}`
  - [ ] `GET /aim/{aim_id}` → 404 if missing
  - [ ] `PUT /aim/{aim_id}` → update; 404 if missing
  - [ ] `DELETE /aim/{aim_id}` → 204; 404 if missing
  - [ ] `GET /aims?user_id=demo` → list
  - [ ] `POST /aim/{aim_id}/digest?mode=incremental|force|cached` → issue `job_id`, enqueue `BackgroundTask(run_pipeline, aim_id, job_id, mode)`, return `{job_id, status:"queued"}`
  - [ ] `GET /digest/{digest_id}` → status while running, full Digest JSON when complete
  - [ ] `GET /health`
- [ ] `run_pipeline(aim_id, job_id, mode)` — `cached` skips ingest entirely, `force` clears seen-set, `incremental` (default) uses dedup
- [ ] Log every stage transition to `job_status[job_id]`: `queued → ingesting → processing → embedding → retrieving → generating → complete`

**Done when:** full curl flow (POST /aim → POST /aim/{id}/digest?mode=force → GET /digest/{id}) returns a Digest with ≥2 dynamic sections. `cached` mode completes in <10 s after a previous run.

**Subagent fan-out:** spawn 2 in parallel, main session refactors in-place:
- **A → `models/schemas.py`**: Pydantic v2 models for `Aim`, `AimCreate`, `DigestItem`, `DigestSection`, `Digest` per [PRODUCT_NOTES.md](PRODUCT_NOTES.md).
- **B → `pipeline/storage.py`**: JSON CRUD for `data/aims/*.json` + `data/digests/*.json` (save/get/update/delete/list_for_user + save_digest/get_digest).
- **Main session**: split `scripts/phase0_skeleton.py` into `pipeline/{ingestion,processing,embedding,vector_store,retrieval,report}.py`, write `main.py` FastAPI + BackgroundTask + three-mode trigger.

Save ~20 min. Main session integrates (imports, job_status dict, endpoint wiring) and runs the curl smoke test.

**Compare artefact:** `data/compare/phase1_api_version.json` (pipe through FastAPI, not script).

---

## Phase 2 — Dedup + reliability (45 min)

**Goal:** The pipeline can be re-run without re-embedding; transient failures retry; one broken source doesn't kill the run.

**Dedup strategy: Tier 1 (URL md5) live, Tier 3 (embedding cosine >0.93) live. Tier 2 (MinHash) *talked-about* in docs/DECISIONS.md, not implemented.** See D13.

**Checklist:**
- [x] `storage.save_raw_articles(articles, job_id)` → `data/raw/{job_id}.json` with `article_id = md5(url).hexdigest()`
- [x] `storage.get_seen_article_ids()` → union `article_id`s across `data/raw/*.json`
- [x] `ingest_all_sources(seen_ids=...)` skips matches
- [x] **Tier 3 semantic dedup:** before upserting a new chunk, query Pinecone `top_k=1`; if `score > 0.93` skip the chunk (log "semantic_dup of {id}")
- [x] `@tenacity.retry(stop_after_attempt(3), wait_exponential(...))` on `feedparser.parse` (convert `bozo_exception` to raised exception first — see [LESSONS § feedparser silent failures](LESSONS.md#feedparser-silent-failures)) and `trafilatura.fetch_url`
- [x] Per-source `try/except` in `ingest_all_sources` — broken feed must not kill the run (+ per-article try/except so one bad article can't kill a source)
- [x] Skip articles with <200 chars extracted text (fallback to RSS summary applied first)
- [x] `logging.getLogger(__name__)` everywhere; `logging.basicConfig(level=INFO)` in `main.py`
- [x] Capture `data/compare/phase2_dedup.json` before moving on

**Done when:** second run of the same Aim: 0 new articles ingested (Tier 1), 0 new chunks upserted (Tier 3), full Digest still emitted from cached Pinecone content. ✅ Measured run 2 `incremental`: 77/78 articles `skipped_seen` (the 1 "new" was a Forbes.cz entry published between the two runs, not a miss); 4 chunks upserted for it; retrieval + Digest still emit in ~13 s (see [DEMO_NOTES § 1 Phase 2 funnels](DEMO_NOTES.md#1-how-it-works--the-8-verb-spine)).

**Subagent fan-out:** spawn 3 in parallel — each touches a different file:
- **A → Tier 1 URL md5** in `pipeline/storage.py` (`save_raw_articles` with `article_id=md5(url).hexdigest()`, `get_seen_article_ids()` union).
- **B → Tier 3 semantic dedup** in `pipeline/vector_store.upsert_chunks` (per-chunk `top_k=1` query with `article_id` $ne filter; skip if `score>0.93`, log `semantic_dup of {id}`).
- **C → tenacity + per-source try/except** in `pipeline/ingestion.py` (`@retry(stop_after_attempt(3), wait_exponential(...))` on `feedparser.parse` + `trafilatura.fetch_url`; convert `bozo_exception` to raised exception first).

Main session reviews diffs, wires `seen_ids` into `ingest_all_sources`, runs second-run verification. Saves ~15 min.

---

## Phase 3 — Frontend (60 min)

**Goal:** A static HTML page served by FastAPI that turns curl into a clickable demo. Visual style mirrors startaiming.com so the HoE feels at home.

**Why this exists before rerank:** in a 10-minute demo window, a clickable UI rendering Digest cards wins against rerank precision improvements every time. The HoE can't *feel* rerank from JSON; they can feel a UI. Rerank is deferrable with a credible verbal answer; frontend-less is not.

Tokens in [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md). Vanilla HTML/CSS/JS, no framework, no build step.

**Checklist:**
- [x] `static/index.html` — three regions: Aim form, Aims list (row = title + summary bullets + entity/region/update chips + icon-button edit/delete + **Mode `<select>`** + Generate primary button), Digest view (headline + date_range + section cards with item cards + clickable source URL chips)
- [x] Pre-populate the two demo Aims on first load if none exist (already handled server-side by `_seed_demo_aims` startup hook in `main.py`)
- [x] `static/app.js` — `fetch` + 2 s poll loop on `GET /digest/{id}` + render. No framework.
- [x] `static/style.css` — use tokens from [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) (brand purple `#552CD9`, Inter, radius `0.5rem`). Inter via Google Fonts `<link>`.
- [x] Icon-only edit/delete (36×36 Lucide pen+trash SVG inline via `<use href="#i-pen|#i-trash">` symbol defs, `currentColor`, hover reveals semantic red on delete); text primary for Generate.
- [x] `app.mount("/", StaticFiles(directory=str(ROOT / "static"), html=True), name="static")` **after** all API routes in `main.py` (placed immediately after `/health`, before the BackgroundTask helpers, so API path patterns win on conflict)
- [x] Empty state ("No Aims yet" / "No digest yet") and error state (`.alert--error` red inline on digest failure or fetch error)
- [x] Per-row live status pill (gray queued · amber pulsing while running · green done · red failed) so multiple Aims can show their last-run state independently

**Done when:** opening `http://localhost:4444` lets you create an Aim, pick a mode, click Generate, watch the Digest render — no curl involved. ✅ Verified end-to-end via Playwright: loaded `/`, switched mode dropdown to `cached` on the CEE Founders Aim, clicked Generate → row pill turned green ("cached · done in 14s") in ~12 s → digest header rendered with date_range/headline/funnel meta (`retrieved 20 · 3 sections · 5 items`) → 3 LLM-chosen sections ("Funding Announcements", "Startup Developments", "Insights on CEE's AI Landscape") with item_type badges, ★ relevance scores, and clickable hostname source chips.

**Subagent fan-out:** mostly solo — frontend is one coherent look-and-feel iteration; splitting HTML/CSS/JS across subagents risks inconsistent spacing/naming. One useful fan-out at start:
- **A → `static/style.css` scaffold**: emit CSS custom properties from [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) tokens (brand purple, Inter, radii, spacing scale) and base resets. Main session builds `index.html` + `app.js` against that palette.
- **Opportunistic parallel task**: while the frontend is iterating locally, dispatch a subagent to write `scripts/compare_digests.py` (Phase 4 deliverable, fully independent). Comes back done; zero critical-path cost.

Open the page in the browser yourself — don't delegate the visual check.

---

## Phase 4 — Rerank + MMR + compare tooling (45 min)

**Goal:** Noticeably better digest quality, provable via `compare_digests.py`.

**Checklist:**
- [x] `retrieval.rerank_chunks(chunks, aim, top_n=15)` — one `gpt-4o-mini` call, JSON mode, receives full structured Aim, returns `{scores:[int]}`. **All defensive shape handling goes through `safe_llm_json` — truncate on over-length, fail loudly on under-length, strip markdown fences.** See [LESSONS § LLM output-shape handling](LESSONS.md#llm-output-shape-handling).
- [x] Wire: `retrieve(top_k=30)` → `rerank(top_n=15)` → `mmr_diversity(λ=0.7)` → `generate_digest(top_10)`
- [x] MMR using Pinecone's returned embeddings (or recompute — cheap)
- [x] `scripts/compare_digests.py` — takes two digest JSON paths, prints: section count, item count, unique URLs, distinct hosts, region coverage, item_type mix, URL Jaccard.
- [x] Capture all four cells of the 2×2 so rerank and source-expansion effects are separable, not confounded: `{phase2_dedup.json, phase4_rerank_only.json, phase4_sources_only.json, phase4_full.json}`
- [x] Eyeball each transition; append surprises to [LESSONS.md](LESSONS.md)

**Done when:** `uv run python scripts/compare_digests.py data/compare/phase2_dedup.json data/compare/phase4_full.json` prints a readable diff table; finding recorded in LESSONS.md.

**Subagent fan-out:** spawn 3 in parallel on independent deliverables:
- **A → `scripts/compare_digests.py`** (skip if already done in Phase 3 opportunistic slot): section count, item count, unique URLs, distinct hosts, region coverage, item_type mix, URL Jaccard. Pure analysis tool, no pipeline deps.
- **B → `retrieval.rerank_chunks`**: `gpt-4o-mini` JSON call, full Aim in prompt, returns `{scores:[int]}`, defensive shape via `safe_llm_json` (truncate/fence-strip/fail-on-short). Fallback to vector order on hard parse fail.
- **C → `retrieval.mmr_diversify`**: `λ=0.7` over Pinecone-returned embeddings, `top_k=10`.

Main session wires `retrieve(top_k=30) → rerank(top_n=15) → mmr → generate(top_10)` and captures the four 2×2 snapshots. Do **not** delegate the eyeball review of the compare table — surprises go in LESSONS.md in your own words.

---

## Phase 5 — Ship on GCP (90–120 min)

**The endpoint is a running Cloud Run URL** serving the same FastAPI app + frontend, backed by Firestore and BigQuery. Local stays as demo fallback only — not the deliverable. Everything swaps behind `storage.py` / `ingestion.py`; never break local fallback during the transition (pre-deploy dry-runs need to work).

> 🛑 **Branch-gate before starting Phase 5.** All earlier phases commit straight to `main` (one-day challenge — branches per feature are pure tax). Phase 5 is the exception: highest blast-radius change of the day, 90–120 min. Before any 5a code, **stop and ask the user** to confirm cutting `phase-5-gcp` off `main`. If Cloud Run deploy wedges, `git checkout main` and demo the local pipeline at 16:00 with zero damage. Do **not** auto-branch — explicit user accept required at execution time.

### Scope calls (locked)

- **Cron jobs:** out. Digest trigger stays manual via `POST /aim/{id}/digest?mode=force`. No Cloud Scheduler.
- **More sources:** out. The current ~10 text sources are the point — extendability is demonstrated by the `@register` registry, not by adding feeds.
- **VertexAI embeddings (was 5c):** cut. Pinecone reindex at `dim=768` costs more than the "GCP-native embeddings" talking point returns. Cloud Run calls OpenAI just fine. **Interview answer:** *"skipped because the Pinecone reindex cost dominates the architectural signal — `embedding.py` is a one-file swap when it matters."*
- **Typed digest items (was 5d):** cut. Product shape, not infra. Doesn't move the "runs on GCP" needle.

### Priority order

- **5a. Firestore for aims + digests (25 min)** — Cloud Run is stateless, so `data/aims/*.json` would evaporate per container. Dual-write JSON + Firestore behind `USE_FIRESTORE` env flag, flip reader last. `pipeline/storage.py` is the only file that changes. Use the default database in Native mode, region `europe-west3`.
- **5b. BigQuery `raw_articles` (25 min)** — `bigquery_client.insert_rows_json("aim_pipeline.raw_articles", rows)` in ingestion step 4. Local JSON stays as dedup truth. Strong demo: one SQL query showing per-source coverage this week.
- **5e. GCS bronze (10 min)** — mirror `data/raw/*.json` to `gs://<bucket>/raw/{date}/{job_id}.json`. Trivial once the BQ client is wired. "Raw archive for reprocessing" talking point.
- **5f. Cloud Run deploy (35 min) — THE GOAL.** Dockerfile + Artifact Registry push + `gcloud run deploy` with `--set-secrets` against Secret Manager. Service account with Firestore User + BigQuery Data Editor + BigQuery Job User + Storage Object Admin + Secret Manager Secret Accessor. Frontend is already mounted at `/` by FastAPI (`main.py:163`), so one service serves both. Keep `uvicorn --port 4444` warm locally — cold Cloud Run start is the one thing that can wreck the demo.

### Secrets handling (non-negotiable)

- **Do not** bake `OPENAI_API_KEY` / `PINECONE_API_KEY` into the image. Use Secret Manager.
- Create two secrets: `openai-api-key`, `pinecone-api-key` (values = current `.env` values).
- At deploy: `--set-secrets=OPENAI_API_KEY=openai-api-key:latest,PINECONE_API_KEY=pinecone-api-key:latest`.
- Grant the Cloud Run runtime service account `roles/secretmanager.secretAccessor` on each secret.
- `.env` stays local-only; `credentials.json` is local-only too — Cloud Run uses its runtime SA via ADC, so `GOOGLE_APPLICATION_CREDENTIALS` must be **unset** inside the container.

### Subagent fan-out

5a/5b touch different files (`storage.py` / `ingestion.py`) — safe to parallelise. 5f is sequential (needs 5a+5b merged first so the deployed image actually uses GCP services). Spawn 2 in parallel *after* the `phase-5-gcp` branch is cut:
- **A → 5a Firestore** in `pipeline/storage.py`: dual-write JSON + Firestore behind `USE_FIRESTORE` env flag; flip reader last. Never break local fallback during the transition.
- **B → 5b BigQuery** in `pipeline/ingestion.py` write path: `bigquery_client.insert_rows_json("aim_pipeline.raw_articles", rows)` after `save_raw_articles`. Local JSON remains dedup truth.

Main session merges sequentially, runs the full pipeline after each swap lands, then does 5e (trivial) and 5f (Dockerfile + deploy) solo.

### Console pre-work the user can do in parallel with Phase 4

These are web-console clicks — no code, no conflict with the in-flight Phase 4 session. Doing these now shaves ~20 min off the Phase 5 critical path:

1. **Enable APIs** in the target project: Firestore, Cloud Run, Cloud Build, Artifact Registry, BigQuery, Secret Manager, Cloud Storage. One-click each.
2. **Firestore:** create database in **Native mode**, region `europe-west3`. Mode cannot be changed after creation — pick Native.
3. **BigQuery:** create dataset `aim_pipeline` in `europe-west3`. Table `raw_articles` will be auto-created by code on first insert.
4. **Cloud Storage:** create a single-region bucket in `europe-west3` (e.g. `aim-raw-<suffix>`), standard class, uniform access.
5. **Artifact Registry:** create a Docker repo in `europe-west3` (e.g. `aim-images`) for Cloud Run container images.
6. **Secret Manager:** create two secrets — `openai-api-key` and `pinecone-api-key` — paste current `.env` values as `latest` versions.
7. **Service account** (optional but cleaner than default compute SA): create `aim-runtime@<project>.iam.gserviceaccount.com`, grant Firestore User + BigQuery Data Editor + BigQuery Job User + Storage Object Admin + Secret Manager Secret Accessor.

If any of these is easier via `gcloud` than the console, that's fine too — the point is they're infra provisioning, fully decoupled from Phase 4 code edits.

---

## Clock-based checkpoints

The phase numbers above flex; the clock doesn't.

| Clock | Checkpoint | What "done" looks like |
|---|---|---|
| **08:00–08:15** | Pre-flight + read brief twice | `.env` + `credentials.json` present, smoke tests pass, Pinecone index live |
| **08:15–08:45** | Scope conversation with HoE | Written list of what ships vs stubs, confirmed aloud |
| **~10:30** | **First demo** — Phase 0 walking skeleton green | Digest JSON printed, HoE sees it, asks "does this match?" |
| **~12:00** | **Second demo** — Phase 1 API + two Aims | curl flow works, both demo Aims produce contrasting digests |
| **12:00–12:45** | **Lunch** — do NOT code through | Casual tech chat with HoE if around; free signal |
| **~14:00** | **Third demo** — Phase 2 dedup + Phase 3 frontend in-progress | Running twice produces 0-new + full digest; UI renders at least one digest |
| **14:30 alarm** | **The fork** | See below |
| **14:30–16:15** | **One deepening track only** | Phase 4 (rerank + compare) OR Phase 5 (GCP). Not both. |
| **16:15–16:45** | Polish + README | Clean `.gitignore`, 5-min-from-clone setup, "How I measured" section |
| **16:45–17:00** | Final demo | Two-minute walkthrough + 10 min Q&A. See [§ Final demo structure](#final-demo-structure). |

### The 14:30 rule

Set an actual alarm. At 14:30:
- **API works + frontend done** → Phase 4 (rerank + compare), 90 min window
- **API works + no frontend** → stop everything, build Phase 3, skip rerank
- **API broken** → fix pipeline, skip frontend + rerank, demo via curl at 16:00

---

---

## Final demo structure

Two-minute walk-through, then 10 minutes of questions. Structure:

1. **"Here's the Aim I created."** Show the structured JSON — call out `regions` and `update_types` as filter dimensions, not prompt content.
2. **"Here's what the pipeline does."** One sentence per verb (ingest → extract → chunk → embed → store → retrieve → rerank → generate). Point at the funnel-metrics log line.
3. **"Here's the digest it produced."** Open the two demo Aims side by side. Source URLs are real, sections fit the content, item_type labels vary.
4. **"Here's what I chose to go deep on, and why."** The 14:30 fork decision — name the trade-off aloud.
5. **"Here's what I cut, and what I'd do next with another day."** Specific files, specific changes — never vague hand-waving.

Weak candidates hand-wave on "what's next"; strong ones name modules and line counts. Three prepared sentences cover it.

---

## Rules for pivoting mid-phase

- **30-min rule.** Single layer blocked >30 min → stub/degrade and continue.
- **Never commit a broken main.** Revert partial commits before pivoting.
- **Phases 0–1 are non-negotiable.** Phases 2–3 are expected. Phase 4 earns 45 min of explicit polish. Phase 5 (ship on GCP via Cloud Run) is the headline deliverable — HoE explicitly wants to see the deployed URL.
- **Verbalise every pivot.** "I'm stubbing X because Y — does that match your expectation?" Free signal, cheap course-correct.

---

## Progress tracker

Tick as each lands:

- [x] Pre-Phase-0 pre-flight (keys, credentials, Pinecone index)
- [x] Phase 0 — Walking skeleton
- [x] Phase 1 — FastAPI + CRUD + three-mode trigger
- [x] Phase 2 — Dedup (Tier 1 + Tier 3) + retries
- [x] Phase 3 — Frontend
- [x] Phase 4 — Rerank + MMR + compare tooling
- [x] Phase 5 console pre-work (APIs, Firestore db `(default)`, BQ dataset `aim_pipeline` in `europe-west3`, GCS bucket `aim-challenge-raw-494220` in `us-central1`, Artifact Registry repo `aim-images` in `europe-west3`, Secret Manager `openai-api-key` + `pinecone-api-key`, runtime SA `aim-pipeline-sa@aim-challenge-494220.iam.gserviceaccount.com` with 6 roles incl. Secret Manager Secret Accessor)
- [x] Phase 5a — Firestore swap (`storage.py`) — dual-write behind `USE_FIRESTORE`, reader flipped, local fallback preserved
- [x] Phase 5b — BigQuery `raw_articles` (`ingestion.py`) — `mirror_raw_to_bq()` auto-creates table, 82 rows across 3 jobs
- [x] Phase 5e — GCS bronze — `mirror_raw_to_gcs()` writes `gs://aim-challenge-raw-494220/raw/{date}/{job_id}.json`
- [x] Phase 5f — Cloud Run deploy with Secret Manager (the goal) — **live at https://aim-645297577758.europe-west3.run.app**
- [x] ~~Phase 5c — VertexAI embeddings~~ (cut: Pinecone reindex cost > signal)
- [x] ~~Phase 5d — Typed digest items~~ (cut: product, not infra)
- [ ] Phase 6 — Post-ship high-value additions (see below)

---

## Phase 6 — Post-ship high-value additions (4 hrs remaining, ~13:00)

With the deployed URL green and Phase 5 done, the remaining gaps are the things that make the demo *defensible under pushback*. Ranked by leverage; pick top-down until the 14:30 alarm.

### 6A — Eval harness (90 min) ⭐ highest leverage
There's no current answer to "how do you know the digest is good?" other than eyeballing.

- [x] `evals/golden.jsonl` — 21 hand-labeled `(aim_id, source_url, should_appear)` pairs across the two demo Aims (9 pos + 3 neg for CEE; 6 pos + 3 neg for SaaS-AI)
- [x] `scripts/eval_digest.py` — scores a captured Digest JSON: **recall@k / precision-on-labelled** vs golden + **LLM-as-judge** (gpt-4o-mini) scoring each item on {relevance, specificity, non_duplication} 1–5 with justification
- [x] Writes `data/evals/run_<ts>_<aim>_<label>.json` so the trend is inspectable. First run on `phase4_full` / CEE: recall@k 0.44 (4/9 pos, 0/3 neg), judge rel 3.25 / spec 3.75 / nondup 4.25 (n=4)
- [ ] Hook into `scripts/compare_digests.py` so every compare artifact gets a number attached

**Demo payoff:** converts every other change ("did dedup help? did rerank help?") from vibe into measurable claim.

### 6B — Wire dedup Tier 2 + 3 for real (60 min)
CLAUDE.md currently admits MinHash and semantic dedup are "talked about, not wired" — a known soft spot the brief explicitly grades.

- [ ] `datasketch` MinHashLSH over shingled article text (threshold 0.8) → near-duplicate cluster id
- [ ] Cosine-similarity pass on title+lede embeddings (threshold 0.92) catches paraphrases MinHash misses
- [ ] Log the funnel collapse: `"412 raw → 387 url-unique → 361 minhash-unique → 352 semantic-unique"`
- [ ] Highest-authority source wins within each cluster

**Demo moment:** ingest same event from Reuters + AP + Bloomberg, show all three collapse to one. Eval harness (6A) then proves duplicate rate dropped 18% → 2% with recall@10 unchanged.

### 6C — Pinecone region colocation (30 min, can run in background)
From parallel investigation: Pinecone serverless runs natively on GCP `europe-west4`, adjacent to Cloud Run `europe-west3`. Current index is cross-region → ~500× latency hit.

- [ ] Create new serverless index in `europe-west4`
- [ ] Re-embed + upsert 620 existing chunks (zero code change, just re-run ingest against new index)
- [ ] Flip `PINECONE_INDEX` env var on Cloud Run, redeploy
- [ ] Measure before/after p95 retrieval latency — expect sub-50ms

**Demo narration already in DEMO_NOTES § 6 as "L6 demo narration."** Lead with root cause, admit brief-scalability tension, present 3 fixes ranked by effort.

### 6D — Cross-encoder rerank stage (30 min)
CLAUDE.md quotes "Pinecone reranking cuts 85% cost vs passing all to GPT-4o" — if that line is in the docs but not in the code, it's a bluff waiting to be called. Verify whether this is wired; if not, wire Pinecone's built-in reranker or Cohere Rerank between ANN and LLM rerank.

### 6E — `/metrics` endpoint + structured per-stage logging (45 min)
Makes "inspectable intermediate outputs" a *live* artifact, not a design claim.
- [ ] Funnel counts per stage, p50/p95 latencies, per-source success rates over last N runs
- [ ] JSON response, renderable in frontend later

### 6F — One more live source (SEC EDGAR JSON, 45 min)
Promotes a registered stub to live. Makes "heterogeneous" concrete. Only if 6A+6B already done.

### Cloud Run hardening (5 min, do now)
- [ ] Set `--no-cpu-throttling` on Cloud Run service. Stays in free tier until ~300 full ingests/month. Safe one-command safety net before demo.

### Scalability layers (talk track, don't build)
Three-layer fix to 10k docs/day per user — name each verbally during the scaling question:
1. Pinecone region colocation (6C) — latency
2. Batch Tier-3 semantic dedup — throughput
3. Cloud Run Jobs for ingest fan-out — horizontal scale

### 6G — Rerank/MMR ranking-stage bug (follow-up, surfaced by eval harness) 🔍
**Lead, not yet fixed.** Eval re-run on saas-ai-legislation after wiring GovTrack showed judge relevance 2.50 → 3.00 (corpus fix worked) but **recall@k stayed 0.00**. Verified the 6 golden SEC press releases are all ingested and live in Pinecone — so retrieval has them, rerank or MMR is dropping them.

Three plausible culprits to instrument:
1. **Rerank scores regulatory prose lower than news-style headlines.** `gpt-4o-mini` rerank prompt doesn't weight `source_type`. Fix: include `source_type` in rerank context, or add a boost when `source_type ∈ aim.update_types`.
2. **MMR over-diversifies an SEC cluster.** 5 of 6 golden positives are SEC press releases — once one lands, MMR penalises the other four. Fix: loosen MMR λ when the Aim is topically narrow (`len(update_types) ≤ 2`), or cluster-cap rather than item-cap.
3. **Recency tilt in cheap-filter stage.** Older SEC filings lose to this morning's OpenAI posts. Fix: decay half-life configurable per `source_type`.

**Diagnostic first, fix second.** Add per-stage logging: for each golden URL, trace retrieved rank → rerank rank → post-MMR inclusion. ~30 min to instrument; then decide which of the three fixes moves recall most.

### 6H — Upgrade golden set to snapshot-backed (follow-up)
Current `evals/golden.jsonl` is URL-only — fragile if a source URL 404s later. **Phase 5 already built the fix**: GCS bronze + BigQuery `raw_articles` persist content keyed on `article_id` (md5-of-URL). Swap the golden set to `{article_id, source_url, content_hash}` triples and reconstitute content from GCS on eval. ~45 min. The snapshot infra exists; the harness just isn't using it.

### Skip
- Pub/Sub / Cloud Tasks fan-out — 2 hrs infra for identical demo semantics
- Prompt caching — real cost win, invisible at demo scale
- More Cloud Run tuning beyond `--no-cpu-throttling`
- **6D cross-encoder rerank** — deprioritised by 6G's finding. Ranking *is* the bottleneck, but a fancier reranker on top of the current prompt won't help until we diagnose why current rerank drops SEC content. Root cause first.

### Order of operations (actual, as executed)
1. ✅ `--no-cpu-throttling` (5 min) — revision `aim-00002-swg`, 152s end-to-end digest latency on deployed URL
2. ✅ 6A eval harness (90 min) — phase-over-phase CEE scorecard + saas snapshot
3. ✅ 6F-lite: GovTrack legislation connector wired live (promoted from stub) — saas judge relevance 2.50 → 3.00, Congress bill at top slot
4. 🔍 **6G surfaced** by the saas re-eval: recall@0 is a ranking-stage bug, not a corpus bug. Filed, not fixed.
5. **14:30 fork** — rehearsal over more code. 6G / 6H / 6B / 6C are all solid "next with a week" candidates.
