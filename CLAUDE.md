# CLAUDE.md

Guidance for Claude Code working in this repo. Claude Code is in use during the Aim 1-day hiring challenge (08:00–17:00, in-office with the Head of Engineering).

## The assignment (source of truth)

Build a **multi-stage data pipeline** that:

1. Ingests thousands of docs/day from heterogeneous sources (US Congress filings, social media, podcasts, videos, news & blogs, long-tail company sites).
2. Normalises, enriches, deduplicates, and stores raw data.
3. Produces a **ranked list of ~10 daily insights per user** for a specific user task (e.g. "legislative changes affecting SaaS AI companies").
4. Is architected to scale across many users, each with a personalised briefing.
5. Is **extendable** to new source types (adding Mexican state institutions or a new SEC feed is a config change, not a pipeline rewrite).

A good pipeline handles source flakiness, dedup (two distinct articles on the same event), multi-stage relevance filtering (LLMs or classical RecSys), and fault-tolerant reprocessing.

**Deliverable:** Part 1 = system-architecture doc. Part 2 = prototype demonstrating the core logic. 10-min show & tell must cover: how it works, why these choices, what to build next with a week, biggest risks.

Full brief: [ASSIGNMENT.md](ASSIGNMENT.md). Senior-engineer deep-research reference (architecture patterns, cost napkin math, connector-design notes): [docs/RESEARCH_NOTES.md](docs/RESEARCH_NOTES.md).

## Brief's four "suggestions" — treat as graded dimensions

1. **Multi-stage ranking, not one giant LLM call.** Retrieve (Pinecone ANN) → cheap filter (recency + source weight + sim blend) → LLM rerank (top 40 → top 15) → MMR diversity → top 10. Every stage logged.
2. **Modularity.** Each stage is a Python module behind a thin interface — `ingestion.py`, `processing.py`, `embedding.py`, `vector_store.py`, `retrieval.py`, `report.py`, `storage.py`. Sources are a pluggable registry (`@register("rss") class RSSConnector`). Swapping Pinecone → BigQuery VECTOR_SEARCH is a one-file change.
3. **Scalability.** Know the bottlenecks out loud even when we don't build them: per-source rate limits (Cloud Tasks), fan-out (Pub/Sub topic-per-stage + DLQ), embedding throughput (VertexAI `RETRIEVAL_DOCUMENT` task type), rerank cost (`gpt-4o-mini` over `gpt-4o` is 15× cheaper, Pinecone reranking cuts 85% cost vs passing all to GPT-4o).
4. **Explicit, inspectable intermediate outputs.** Funnel metrics at every stage (`412 raw → 245 clean → 40 reranked → 10 items`). Compare-artifacts in `data/compare/` for every quality-changing phase. `scripts/compare_digests.py` as an auto-diff tool.

## Scope — what we're building and what we're not

**Deliberate scope calls:**

- **Text-only sources.** RSS/news/blogs + SEC EDGAR JSON + Congress.gov + Reddit (all text). **No YouTube, no podcasts, no Whisper.** Rationale: video/audio adds yt-dlp + Whisper + 1-hour transcription latency for zero marginal architectural signal. They're *what's next with a week*, not what gets built today. Stubbing a `PodcastConnector` class in the registry is a 30-second talking point; implementing it is a 3-hour rabbit hole.
- **~10 text sources live, 2–3 other source types stubbed.** The pluggable connector pattern is what matters — having RSS live and `SECConnector` / `RedditConnector` as registered stubs demonstrates extendability without burning ingest time.
- **One or two synthetic users with thoughtfully different Aims.** The contrast is what sells personalisation. Not scale.
- **Local JSON storage → Firestore swap later.** Keep the critical path fast; GCP swap is Phase 5 conditional on time + credentials.
- **BackgroundTasks for async, not Pub/Sub.** Show we *know* the Pub/Sub + Cloud Run pattern verbally; running it would cost 2 hours of infra for identical semantics at this scale.
- **Auth is out of scope** — `user_id` is trust-the-client. See [DECISIONS D15](docs/DECISIONS.md#d15-auth-is-out-of-scope-user_id-is-trust-the-client).

**Not stubbed, not optional:**

- Dedup must be *honest*: exact URL hash + (talk about) MinHash + (talk about) semantic embedding cosine. Show a `Deduper` class with the three tiers even if only tier 1 is wired.
- Hybrid retrieval (Pinecone metadata filter `{"region": {"$in": aim.regions}}`) must be live — this is the single strongest RAG signal.
- Per-source try/except + tenacity retries from the start. "One flaky source does not kill the run" is demo-critical.

## Product nouns (from Aim's live app)

**Aim** = structured monitoring config (not a thesis/profile/query). Fields:
- `title: str` — "xAI News Monitoring"
- `summary: list[str]` — bullets of intent
- `monitored_entities: list[str]` — ["xAI"] or ["Founders", "Companies"]
- `regions: list[str]` — ["Czechia", "Slovakia"] or ["Global"]
- `update_types: list[str]` — ["News", "Announcements", "Reports"]

**Digest** = generated briefing. Fields: `headline`, `date_range`, `sections: list[{title, items}]`. **Section titles are LLM-chosen per run** (not a fixed enum) — live app produces "Opinion leaders mentioned / Investments / Product Updates" for one Aim, "Hires & exits / Fundraises / Regulatory" for another.

Use `Aim` + `Digest` everywhere. Full product spec with schema + example digests from the live app: [docs/PRODUCT_NOTES.md](docs/PRODUCT_NOTES.md).

## The 8-verb pipeline

```
ingest → extract → chunk → embed → store → retrieve → rerank → generate
  │         │        │       │       │         │          │        │
  RSS/API   trafil.  Lang-   OpenAI  Pinecone  hybrid     gpt-4o-  gpt-4o-
  connectors         Chain           (global)  filter     mini     mini
                     splitter                  + ANN      + top-15 + JSON
```

Modules in `pipeline/` map 1:1 onto stages. Each emits structured Pydantic, each can be unit-tested in isolation.

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Python | 3.11 (`.python-version`) | via `uv` — always `uv run ...`, `uv add <pkg>` |
| Web | FastAPI + uvicorn | Aim/Digest endpoints + `/health` |
| LLM | OpenAI `gpt-4o-mini` | `response_format={"type":"json_object"}` always. Upgrade to `gpt-4o` if digest quality lags. |
| Embeddings | OpenAI `text-embedding-3-small` | 1536 dim |
| Vector store | **Pinecone** serverless | matches Aim's stack; chunks tagged with `region` + `source_type` at ingest |
| Aims/digests | local JSON (`data/aims/`, `data/digests/`) | Firestore swap is one file (`storage.py`) |
| Ingestion | feedparser + trafilatura + tenacity | |
| Chunking | LangChain `RecursiveCharacterTextSplitter` | `chunk_size=800`, `overlap=100`, title prepended |
| Models | Pydantic v2 | |
| Frontend (Phase 3) | Vanilla HTML/CSS/JS, Inter from Google Fonts | No framework, no build step. Design tokens in [docs/DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md). |

Phase 5 (ship on GCP — HoE's top ask): Firestore (aims+digests), BigQuery `raw_articles`, GCS bronze, Cloud Run deploy with Secret Manager. **Priority order: Firestore → BigQuery → GCS → Cloud Run deploy.** VertexAI embeddings and typed digest items are **cut** (see ROADMAP Phase 5 for reasoning). Pinecone metadata filters are Tier A (wired from Phase 0). See [docs/ROADMAP.md § Phase 5](docs/ROADMAP.md#phase-5--ship-on-gcp-90120-min) for the exact swap plan.

## Core principles

- **Walking skeleton first.** Phase 0 = one script that runs every stage end-to-end for one hardcoded Aim and prints a Digest JSON. No FastAPI, no dedup, no rerank. ~60–90 min. Nothing else matters until that runs green.
- **One layer at a time, but never without the rest.** The whole pipe must run after every change. If Pinecone is down → numpy cosine. If trafilatura 403s → fall back to `entry.summary`.
- **30-minute rule.** If a layer blocks >30 min, stub/degrade and keep moving. Coming back to polish is easier than recovering from an unrun pipeline at 17:00.
- **Verify end-to-end after each phase.** Run the pipe, eyeball the Digest JSON, check source URLs are real.
- **Capture before/after compare artefacts for every quality-changing phase** into `data/compare/phase{N}_{variant}.json`. These are the interview exhibit for "did this change help and by how much?".

## Working rules

- Always `uv run …`, never bare `python`.
- Keep `main` runnable at all times — commit after each phase.
- Pinecone chunk metadata must carry `region` + `source_type` from day one — they're the hooks for hybrid filter.
- `Digest.sections` is dynamic — LLM picks 2–5 titles per run. Schema validates `list[{title, items}]`, nothing more.
- Every structured LLM call uses `response_format={"type":"json_object"}`. The prompt must contain the literal word "JSON".
- Defensive LLM output handling: arrays may come back length N+1 (truncate, log), wrapped in markdown fences (strip), or as a JSON-stringified list (parse again). Write a `safe_llm_json(raw, key, expected_len)` helper in Phase 0; reuse it everywhere.
- Per-source `try/except` in ingestion — broken feed must not kill the run.
- Don't write docstrings that restate code. Comment only the non-obvious (API quirk, rate-limit gotcha, invariant).

## Phase order (from the prep retro — battle-tested)

Phase numbers map to the brief's demands; the clock is what matters (`data/compare/` proves it).

| Phase | Work | Time | Done when |
|---|---|---|---|
| 0 | Walking skeleton: 1 hardcoded Aim, **~10 region-weighted sources** (not 5), hybrid filter wired, print Digest JSON | 60–90 min | `uv run python scripts/run_pipeline.py` prints a valid Digest whose source URLs are real |
| 1 | FastAPI + full Aim CRUD (POST/GET/PUT/DELETE) + **three-mode digest trigger** (`incremental\|force\|cached`) + local JSON storage | 60 min | curl flow works; `cached` mode runs in <10 s |
| 2 | Dedup (URL md5 live + MinHash + semantic talked-about) + tenacity retries + per-source try/except | 45 min | re-running a digest returns 0 new articles but still a digest |
| 3 | **Frontend** (vanilla HTML/CSS/JS card-based Digest rendering, Aim form, mode selector) | 60 min | clicking through the UI without curl works |
| 4 | LLM rerank + MMR diversity + `scripts/compare_digests.py` + before/after snapshots | 45 min | compare tool prints a readable diff table |
| 5 | Ship on GCP: Firestore → BigQuery raw → GCS bronze → Cloud Run deploy (Secret Manager for keys) | 90–120 min | Cloud Run URL serves the UI + API; Firestore holds aims/digests; BQ has raw_articles rows |

**The 14:30 rule.** Set an alarm. At 14:30:
- API works + frontend done → go to Phase 4 (rerank).
- API works + no frontend → stop everything, build Phase 3.
- API broken → fix pipeline, skip Phase 3–5, demo via curl at 16:00.

Full phase breakdown with clock-based checkpoints + 14:30 fork + final demo structure: [docs/ROADMAP.md](docs/ROADMAP.md). System design + module responsibilities + scaling target: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Decisions with reasoning + reversal cost + interview notes: [docs/DECISIONS.md](docs/DECISIONS.md). Standing defensive idioms + running lessons: [docs/LESSONS.md](docs/LESSONS.md). Senior-engineer deep-research reference: [docs/RESEARCH_NOTES.md](docs/RESEARCH_NOTES.md).

## Commands

```bash
uv sync                                   # install deps
uv add <pkg>                              # add a dep
uv run python scripts/run_pipeline.py     # Phase 0 walking skeleton
uv run uvicorn main:app --reload --port 4444   # Phase 1+ API

# curl smoke test (Phase 1+)
curl -X POST http://localhost:4444/aim \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","title":"…","summary":["…"],"monitored_entities":["…"],
       "regions":["…"],"update_types":["…"]}'
# → {"aim_id":"…"}

curl -X POST "http://localhost:4444/aim/<aim_id>/digest?mode=force"
# → {"job_id":"…","status":"queued"}

curl http://localhost:4444/digest/<job_id>
# → {status or full Digest JSON}
```

## Environment

```
OPENAI_API_KEY=sk-...
PINECONE_API_KEY=...
PINECONE_INDEX=aim-chunks
# GCP (Phase 5, optional)
GOOGLE_APPLICATION_CREDENTIALS=./credentials.json
GCP_PROJECT_ID=aim-challenge-494220
GCP_LOCATION=europe-west3
```

Pinecone setup (one-time): create a serverless index matching `PINECONE_INDEX`, `dim=1536`, `metric=cosine`.

## Self-maintenance

These docs exist so the **16:45 show & tell** is a curation job, not a scramble. Keep them current as the day unfolds — the brief's four demo themes (*how it works, why these choices, what to build next with a week, biggest risks*) each have a home below. If the 15 minutes before demo are spent writing rather than rehearsing, we failed this rule.

- **Decisions** → new entry in [docs/DECISIONS.md](docs/DECISIONS.md) (format: Decision / Why / Alternatives rejected / Cost if reversed / Interview note). The *Interview note* field is demo-script material — write it in the voice you'd say it aloud.
- **Surprises discovered by running** → [docs/LESSONS.md](docs/LESSONS.md) (format: What surprised me / Context / What to do). If a lesson exposes real fragility, also mirror a one-liner into [docs/DEMO_NOTES.md § 6 Biggest risks](docs/DEMO_NOTES.md#6-biggest-risks--whats-fragile-in-the-current-build).
- **Phase completions** → tick [docs/ROADMAP.md § Progress tracker](docs/ROADMAP.md#progress-tracker) **and** update the corresponding verb row in [docs/DEMO_NOTES.md § 1 How it works](docs/DEMO_NOTES.md#1-how-it-works--the-8-verb-spine) — only claim a verb as live if the phase actually wired it.
- **Architecture changes** → update [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Scope cuts, stubs, degraded paths, 30-min-rule pivots** → append to [docs/DEMO_NOTES.md § 4 What I cut](docs/DEMO_NOTES.md#4-what-i-cut--deliberate-scope-calls) with the named module + 1-line why + what would unstub it. Every cut is also a "what's next with a week" candidate — if it's a feature you'd resurrect, add a named-module, rough-LOC bullet to [§ 5 What's next](docs/DEMO_NOTES.md#5-whats-next-with-a-week--named-modules-named-line-counts). Weak candidates hand-wave here; strong ones name the file and line count.
- **The 14:30 fork decision** → fill in [docs/DEMO_NOTES.md § 3](docs/DEMO_NOTES.md#3-what-i-chose-to-go-deep-on--the-1430-fork) the moment the alarm goes off. Name the trade-off in the voice you'd say it aloud.
- **Before every commit**: `git check-ignore -v .env credentials.json` must print matches for both. Never commit `data/raw|aims|digests/` either.
