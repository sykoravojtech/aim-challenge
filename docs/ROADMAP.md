# Roadmap — the phased build plan

**The most important doc in the repo.** Follow phase order. Do not start a phase until the previous one runs end-to-end. If a layer blocks >30 min, stub/degrade and keep the skeleton running.

The clock is firm (see [§ Clock-based checkpoints](#clock-based-checkpoints)); phase scope flexes within it. A boring working pipeline by noon beats any deep feature half-built at 17:00.

Terminology (**Aim** = monitoring config, **Digest** = output) — see [PRODUCT_NOTES.md](PRODUCT_NOTES.md).

---

## Pre-Phase-0 checklist (08:00–08:15)

Before any code runs:

- [ ] `.env` in repo root with `OPENAI_API_KEY`, `PINECONE_API_KEY`, `PINECONE_INDEX=aim-chunks` (+ GCP vars if Phase 5 is in scope)
- [ ] `credentials.json` (GCP service account with Datastore / BigQuery / VertexAI / GCS roles) in repo root if Phase 5 is in scope
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

**Deliverable:** `scripts/run_pipeline.py` (single file) and a `safe_llm_json(raw, key, expected_len)` helper living in `pipeline/_util.py`.

**Checklist:**
- [x] Create Pinecone index `aim-chunks` (`dim=1536`, `cosine`) if not already live
- [x] Hardcode both Aims from the § above (even though Phase 0 only runs one, having both defined early means Phase 1 can demo the contrast without retrofitting)
- [x] Hardcode the 10 RSS sources from the § above, smoke-tested
- [x] `RSSConnector` with `list_new_items()` + `fetch()` — registered in a `REGISTRY` dict even though it's the only live one, so the extensibility pattern is present from day one
- [x] For each article URL: `trafilatura.fetch_url` + `.extract`; skip if <200 chars; fall back to RSS `entry.summary` on empty
- [x] Chunk with `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)`, title prepended
- [x] Embed with `text-embedding-3-small` (batch ≤100)
- [x] Upsert to Pinecone: `id = uuid4()`, `metadata = {article_id, source_url, source_feed, title, text[:1000], source_type, region}` — `region` + `source_type` mandatory on every chunk
- [x] Retrieve: `build_query_text(aim)` → embed → `index.query(top_k=20, filter={"region": {"$in": [*aim.regions, "Global"]}})`
- [x] Generate: one `gpt-4o-mini` call with `response_format={"type":"json_object"}`, `temperature=0.3`, system "senior market intelligence analyst producing a personalised digest", user prompt embeds the full structured Aim + top-20 chunks, returns `{headline, date_range, sections:[{title, items:[{title, body, source_urls, source_count, item_type, relevance_score}]}]}`
- [x] Log funnel metrics at every stage: `INGESTED=N, EXTRACTED=N, CHUNKED=N, EMBEDDED=N, UPSERTED=N, RETRIEVED=N`
- [x] `print(json.dumps(digest, indent=2))`

**Done when:** `uv run python scripts/run_pipeline.py` prints a valid Digest JSON whose `sections[*].items[*].source_urls[*]` are real, ingested article URLs.

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
- [ ] Split `scripts/run_pipeline.py` into `pipeline/{ingestion,processing,embedding,vector_store,retrieval,report,storage}.py`
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
- **Main session**: split `scripts/run_pipeline.py` into `pipeline/{ingestion,processing,embedding,vector_store,retrieval,report}.py`, write `main.py` FastAPI + BackgroundTask + three-mode trigger.

Save ~20 min. Main session integrates (imports, job_status dict, endpoint wiring) and runs the curl smoke test.

**Compare artefact:** `data/compare/phase1_api_version.json` (pipe through FastAPI, not script).

---

## Phase 2 — Dedup + reliability (45 min)

**Goal:** The pipeline can be re-run without re-embedding; transient failures retry; one broken source doesn't kill the run.

**Dedup strategy: Tier 1 (URL md5) live, Tier 3 (embedding cosine >0.93) live. Tier 2 (MinHash) *talked-about* in docs/DECISIONS.md, not implemented.** See D13.

**Checklist:**
- [ ] `storage.save_raw_articles(articles, job_id)` → `data/raw/{job_id}.json` with `article_id = md5(url).hexdigest()`
- [ ] `storage.get_seen_article_ids()` → union `article_id`s across `data/raw/*.json`
- [ ] `ingest_all_sources(seen_ids=...)` skips matches
- [ ] **Tier 3 semantic dedup:** before upserting a new chunk, query Pinecone `top_k=1`; if `score > 0.93` skip the chunk (log "semantic_dup of {id}")
- [ ] `@tenacity.retry(stop_after_attempt(3), wait_exponential(...))` on `feedparser.parse` (convert `bozo_exception` to raised exception first — see [LESSONS § feedparser silent failures](LESSONS.md#feedparser-silent-failures)) and `trafilatura.fetch_url`
- [ ] Per-source `try/except` in `ingest_all_sources` — broken feed must not kill the run
- [ ] Skip articles with <200 chars extracted text (fallback to RSS summary applied first)
- [ ] `logging.getLogger(__name__)` everywhere; `logging.basicConfig(level=INFO)` in `main.py`
- [ ] Capture `data/compare/phase2_dedup.json` before moving on

**Done when:** second run of the same Aim: 0 new articles ingested (Tier 1), 0 new chunks upserted (Tier 3), full Digest still emitted from cached Pinecone content.

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
- [ ] `static/index.html` — three regions: Aim form, Aims list (row = title + summary bullets + entity/region/update chips + icon-button edit/delete + **Mode `<select>`** + Generate primary button), Digest view (headline + date_range + section cards with item cards + clickable source URL chips)
- [ ] Pre-populate the two demo Aims on first load if none exist
- [ ] `static/app.js` — `fetch` + 2 s poll loop on `GET /digest/{id}` + render. No framework.
- [ ] `static/style.css` — use tokens from [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) (brand purple `#552CD9`, Inter, radius `0.5rem`). Inter via Google Fonts `<link>`.
- [ ] Icon-only edit/delete (36×36 Lucide pen+trash SVG inline, `currentColor`, hover reveals semantic colour); text primary for Generate. Rows with ≥3 actions read noisy when all three are text buttons — icon-only on utility actions fixes the hierarchy.
- [ ] `app.mount("/", StaticFiles(directory="static", html=True), name="static")` **after** all API routes in `main.py`
- [ ] Empty state ("Create your first Aim") and error state (red inline on digest failure)

**Done when:** opening `http://localhost:4444` lets you create an Aim, pick a mode, click Generate, watch the Digest render — no curl involved.

**Subagent fan-out:** mostly solo — frontend is one coherent look-and-feel iteration; splitting HTML/CSS/JS across subagents risks inconsistent spacing/naming. One useful fan-out at start:
- **A → `static/style.css` scaffold**: emit CSS custom properties from [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md) tokens (brand purple, Inter, radii, spacing scale) and base resets. Main session builds `index.html` + `app.js` against that palette.
- **Opportunistic parallel task**: while the frontend is iterating locally, dispatch a subagent to write `scripts/compare_digests.py` (Phase 4 deliverable, fully independent). Comes back done; zero critical-path cost.

Open the page in the browser yourself — don't delegate the visual check.

---

## Phase 4 — Rerank + MMR + compare tooling (45 min)

**Goal:** Noticeably better digest quality, provable via `compare_digests.py`.

**Checklist:**
- [ ] `retrieval.rerank_chunks(chunks, aim, top_n=15)` — one `gpt-4o-mini` call, JSON mode, receives full structured Aim, returns `{scores:[int]}`. **All defensive shape handling goes through `safe_llm_json` — truncate on over-length, fail loudly on under-length, strip markdown fences.** See [LESSONS § LLM output-shape handling](LESSONS.md#llm-output-shape-handling).
- [ ] Wire: `retrieve(top_k=30)` → `rerank(top_n=15)` → `mmr_diversity(λ=0.7)` → `generate_digest(top_10)`
- [ ] MMR using Pinecone's returned embeddings (or recompute — cheap)
- [ ] `scripts/compare_digests.py` — takes two digest JSON paths, prints: section count, item count, unique URLs, distinct hosts, region coverage, item_type mix, URL Jaccard.
- [ ] Capture all four cells of the 2×2 so rerank and source-expansion effects are separable, not confounded: `{phase2_dedup.json, phase4_rerank_only.json, phase4_sources_only.json, phase4_full.json}`
- [ ] Eyeball each transition; append surprises to [LESSONS.md](LESSONS.md)

**Done when:** `uv run python scripts/compare_digests.py data/compare/phase2_dedup.json data/compare/phase4_full.json` prints a readable diff table; finding recorded in LESSONS.md.

**Subagent fan-out:** spawn 3 in parallel on independent deliverables:
- **A → `scripts/compare_digests.py`** (skip if already done in Phase 3 opportunistic slot): section count, item count, unique URLs, distinct hosts, region coverage, item_type mix, URL Jaccard. Pure analysis tool, no pipeline deps.
- **B → `retrieval.rerank_chunks`**: `gpt-4o-mini` JSON call, full Aim in prompt, returns `{scores:[int]}`, defensive shape via `safe_llm_json` (truncate/fence-strip/fail-on-short). Fallback to vector order on hard parse fail.
- **C → `retrieval.mmr_diversify`**: `λ=0.7` over Pinecone-returned embeddings, `top_k=10`.

Main session wires `retrieve(top_k=30) → rerank(top_n=15) → mmr → generate(top_10)` and captures the four 2×2 snapshots. Do **not** delegate the eyeball review of the compare table — surprises go in LESSONS.md in your own words.

---

## Phase 5 — GCP swap (90–180 min, conditional)

**If the HoE blesses it OR if Phases 0–4 land by 15:00, do 5a–5c minimum.** Everything swaps behind `storage.py`/`vector_store.py`/`embedding.py`; never break local fallback.

> 🛑 **Branch-gate before starting Phase 5.** All earlier phases commit straight to `main` (one-day challenge — branches per feature are pure tax). Phase 5 is the exception: highest blast-radius change of the day, conditional, 90–180 min. Before any 5a code, **stop and ask the user** to confirm cutting `phase-5-gcp` off `main`. If GCP eats the afternoon, `git checkout main` and demo the local pipeline at 16:00 with zero damage. Do **not** auto-branch — explicit user accept required at execution time.

### Priority order (stop when time runs out)

- **5a. Firestore for aims + digests (25 min)** — dual-write JSON + Firestore, flip reader last. `storage.py` is the only file that changes.
- **5b. BigQuery `raw_articles` (30 min)** — `bigquery_client.insert_rows_json("aim_pipeline.raw_articles", rows)` in ingestion step 4. Local JSON stays as dedup truth. Demo SQL: "per-source coverage this week".
- **5c. VertexAI embeddings (25 min)** — `text-embedding-004` with `task_type="RETRIEVAL_DOCUMENT"` at index, `"RETRIEVAL_QUERY"` at retrieve. Pinecone index must be recreated at `dim=768` OR keep a provider flag per run. Strong talking point.
- **5d. Typed digest items (30 min)** — per-`item_type` structured fields (`quote`+`attribution`, `entity`+`amount`). Matches live Digest shape from [PRODUCT_NOTES](PRODUCT_NOTES.md).
- **5e. GCS bronze (15 min)** — mirror `data/raw/*.json` to `gs://aim-raw-articles/raw/{date}/{job_id}.json`. Trivial after 5b.
- **5f. Cloud Run deploy (20 min)** — Dockerfile + `gcloud run deploy`. Pure polish. Skip unless everything else is rock-solid and the demo has a cached fallback to localhost: a cold-starting Cloud Run URL timing out mid-demo is worse than `uvicorn --port 4444`.

**Subagent fan-out:** 5a/5b/5c touch different files (`storage.py` / `ingestion.py` / `embedding.py`) — ideal fan-out shape. Spawn 3 in parallel *after* the `phase-5-gcp` branch is cut:
- **A → 5a Firestore** in `pipeline/storage.py`: dual-write JSON + Firestore behind an env flag; flip reader last. Never break local fallback.
- **B → 5b BigQuery** in `pipeline/ingestion.py` write path: `bigquery_client.insert_rows_json("aim_pipeline.raw_articles", rows)` after `save_raw_articles`. Local JSON remains dedup truth.
- **C → 5c VertexAI** in `pipeline/embedding.py`: `text-embedding-004` with `task_type=RETRIEVAL_DOCUMENT|RETRIEVAL_QUERY`; recreate Pinecone index at `dim=768` OR gate behind a provider flag per run.

Main session merges sequentially, running the full pipeline after each swap lands. 5d/5e/5f are solo (typed items needs product judgement; GCS + Cloud Run are single-file, trivial).

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
- **Phases 0–1 are non-negotiable.** Phases 2–3 are expected. Phase 4 earns 45 min of explicit polish. Phase 5 is conditional on time + HoE steer.
- **Verbalise every pivot.** "I'm stubbing X because Y — does that match your expectation?" Free signal, cheap course-correct.

---

## Progress tracker

Tick as each lands:

- [x] Pre-Phase-0 pre-flight (keys, credentials, Pinecone index)
- [x] Phase 0 — Walking skeleton
- [ ] Phase 1 — FastAPI + CRUD + three-mode trigger
- [ ] Phase 2 — Dedup (Tier 1 + Tier 3) + retries
- [ ] Phase 3 — Frontend
- [ ] Phase 4 — Rerank + MMR + compare tooling
- [ ] Phase 5a — Firestore swap
- [ ] Phase 5b — BigQuery raw_articles
- [ ] Phase 5c — VertexAI embeddings
- [ ] Phase 5d — Typed digest items
- [ ] Phase 5e — GCS bronze
- [ ] Phase 5f — Cloud Run deploy
