# Architecture

Product nouns (Aim, Digest, Account) come from [PRODUCT_NOTES.md](PRODUCT_NOTES.md). Read that first if any term is unfamiliar.

## System diagram (prototype, 1 process)

```
[caller: curl / static HTML / future Cloud Scheduler]
             │
             ▼
┌─────────────────────────────────────────────┐
│  FastAPI  (main.py)                         │
│    CRUD /aim  ·  /aim/{id}/digest?mode=…    │
│    GET /digest/{id}  ·  /health             │
└─────────────────────────────────────────────┘
             │                                │
             ▼                                ▼
┌──────────────────────────┐    ┌────────────────────────────────────────┐
│  local JSON storage      │    │  pipeline (BackgroundTask)             │
│  data/aims/*.json        │    │                                        │
│  data/digests/*.json     │    │  1. load_aim(aim_id)                   │
│  data/raw/*.json         │    │  2. get_seen_article_ids()    (Tier 1) │
│  data/compare/*.json     │    │  3. for connector in REGISTRY:         │
└──────────────────────────┘    │       ingest (per-source try/except)   │
                                 │  4. save_raw_articles                  │
                                 │  5. chunk_articles                     │
                                 │  6. embed_chunks                       │
                                 │  7. vector_store.upsert                │
                                 │       (skip if cos>0.93       Tier 3)  │
                                 │       metadata: region + source_type   │
                                 │  8. retrieve_relevant_chunks           │
                                 │       filter={"region": {"$in": …}}    │
                                 │  9. rerank_chunks (full Aim)           │
                                 │ 10. mmr_diversify                      │
                                 │ 11. generate_digest (dynamic sections) │
                                 │ 12. save_digest                        │
                                 └────────────────────────────────────────┘
                                             │
                                             ▼
                                 ┌────────────────────────────────────────┐
                                 │  external services                     │
                                 │   • RSS feeds (feedparser)             │
                                 │   • api.congress.gov (JSON)            │
                                 │   • data.sec.gov (JSON, UA-required)   │
                                 │   • OpenAI (embeddings + 4o-mini JSON) │
                                 │   • Pinecone serverless (ANN + filter) │
                                 └────────────────────────────────────────┘
```

## Scaling target (talk about, don't build)

```
Cloud Scheduler (cron per timezone)
      │
      ▼
Pub/Sub topic-per-stage  ──DLQ (5 attempts, exp backoff 10s→600s)
      │
      ▼
Cloud Run workers ── fan out per-source ── Cloud Tasks for rate-limited APIs
      │
      ▼
GCS (raw HTML, content-addressed)  +  BigQuery (append-only analytics)
      │                                         │
      ▼                                         ▼
Pinecone (vectors + metadata)        Firestore (live aims, current digest)
      │
      ▼
Per-user Cloud Run worker ── batched embedding + Pinecone query ──
 LLM rerank (gpt-4o-mini) ── MMR ── digest generator (gpt-4o)
      │
      ▼
Firestore briefings/{user}/{date}  +  BigQuery ranking_events (audit)
      │
      ▼
Email / Slack / web app
```

Every stage is a Cloud Run service consuming one topic and publishing to the next. DLQs per stage, replayer Cloud Function for poison messages. See [DECISIONS D4 + D6](DECISIONS.md) for why this prototype doesn't wire it.

## Data flow (happy path, prototype)

1. `POST /aim {user_id, title, summary, monitored_entities, regions, update_types}` → server mints `aim_id`, persists to `data/aims/{aim_id}.json`, returns `{aim_id}`.
2. `POST /aim/{aim_id}/digest?mode=force` → server mints `job_id = digest_id`, enqueues `BackgroundTask(run_pipeline, aim_id, job_id, mode="force")`, returns `{job_id, status:"queued"}` immediately.
3. Background task runs the 12-step pipeline above; updates `job_status[job_id]` per stage; persists final digest to `data/digests/{digest_id}.json`.
4. Client polls `GET /digest/{digest_id}` → `{status: ingesting|processing|embedding|retrieving|reranking|generating}` while running, full Digest JSON on complete.

### Three modes on the digest trigger

| Mode | Ingest | Dedup | Retrieve+Rerank+Generate | Latency |
|---|---|---|---|---|
| `incremental` (default) | yes (skip seen) | Tier 1 + 3 | yes | 30–60 s |
| `force` | yes (ignore seen) | Tier 3 only | yes | 60–120 s |
| `cached` | no | n/a | yes (against current Pinecone state) | 5–10 s |

`cached` is the real demo gift — it lets you iterate on rerank/prompts without spending ingest tokens, and it's structurally *the read-only digest service* from the scaled architecture (Cloud Run Job ingest + Cloud Run Service retrieve/rank). One hot button proves the split.

**Demo choreography:** click `force` first → narrate the full ingest (~60–90 s with polling spinner). Flip to `cached` → click again → ~5–10 s. Then say: *"The fast one isn't cached cleverness — it's the shape production should have. Ingest is a scheduled Cloud Run Job on a cron; this endpoint is read-only against Pinecone. The monolith you see now is those two stages welded together for one-day scope. The mode selector ablates the cost of not splitting."* That paragraph is the answer to "why didn't you split ingest and digest?".

## The 8-verb pipeline — modules and responsibilities

Every stage is a module in `pipeline/`. Each module is individually importable and unit-testable. The `REGISTRY` pattern for connectors makes extensibility explicit: adding a Mexican-state-institutions source is one new class + one line of config.

### `pipeline/ingestion.py`

```python
class BaseConnector(Protocol):
    source_id: str
    region: str
    source_type: str
    def list_new_items(self, since: datetime) -> Iterable[ItemRef]: ...
    def fetch(self, ref: ItemRef) -> RawDoc: ...

REGISTRY: dict[str, type[BaseConnector]] = {}
def register(name):
    def deco(cls): REGISTRY[name] = cls; return cls
    return deco

@register("rss")
class RSSConnector(BaseConnector): ...       # live

@register("sec")
class SECConnector(BaseConnector): ...       # live (Phase 2)

@register("congress")
class CongressConnector(BaseConnector): ...  # live (Phase 0/2 depending on API key)

@register("reddit")
class RedditConnector(BaseConnector):        # stubbed
    def list_new_items(self, since): raise NotImplementedError(...)

# Same for XConnector, LinkedInConnector, YouTubeConnector, PodcastConnector
```

Adding a new source type = register a new class. Adding a new feed within an existing type = append a dict to `SOURCES`. **That distinction is the extensibility story.**

### `pipeline/processing.py`
- `chunk_articles(articles) -> list[dict]` — LangChain `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100)`, title prepended. Each chunk carries `chunk_id` (uuid), `article_id`, `source_url`, `source_type`, `region`, `title`, `text`, `chunk_index`, `total_chunks`.

### `pipeline/embedding.py`
- `embed_texts(texts) -> list[list[float]]` — OpenAI `text-embedding-3-small`, batched ≤100, tenacity-retried.
- `embed_chunks(chunks)` — mutates in place with `embedding`.
- `embed_query(q) -> list[float]`.
- **Swap path:** replace with VertexAI `text-embedding-004` + `task_type=RETRIEVAL_DOCUMENT|RETRIEVAL_QUERY`. Pinecone index recreated at `dim=768`. See [DECISIONS D2](DECISIONS.md).

### `pipeline/vector_store.py`
- Wraps Pinecone client (one `Index` handle).
- `upsert_chunks(chunks)` — batches ≤100. **Tier 3 dedup:** for each chunk, `index.query(vector=emb, top_k=1, filter={"article_id": {"$ne": article_id}})`; if `score > 0.93`, skip + log `semantic_dup of {id}`.
- `query(embedding, top_k, filter=None) -> list[dict]` — flattens to `{chunk_id, score, metadata}`.
- **Swap path:** BigQuery `VECTOR_SEARCH`. One file change.

### `pipeline/retrieval.py`
- `build_query_text(aim) -> str` — `f"{aim.title} — {' '.join(aim.summary)} — monitoring: {', '.join(aim.monitored_entities)}"`.
- `build_query_filter(aim) -> dict` — `{"region": {"$in": [*aim.regions, "Global"]}}`. "Global" always OR'd in so a Global-tagged "CEE VC outlook" piece serves a regional Aim (prevents over-filtering on narrow Aims).
- `retrieve_relevant_chunks(aim, top_k=30)` — embed query, Pinecone query with filter, return chunks.
- `rerank_chunks(chunks, aim, top_n=15)` — one `gpt-4o-mini` call, JSON mode, full Aim passed in, returns `{scores:[int,...]}`. Defensive shape handling via `safe_llm_json` (truncate on `len>N`, fail on `<N`, strip markdown fences). Fallback to vector-search order only on hard parse failure.
- `mmr_diversify(chunks, top_k=10, lambda_=0.7)` — maximal marginal relevance over embeddings to avoid three items about the same event.

### `pipeline/report.py`
- `generate_digest(aim, ranked_chunks) -> dict` — `gpt-4o-mini`, `response_format={"type":"json_object"}`, `temperature=0.3`. System: "senior market intelligence analyst". User: structured Aim + top chunks + instruction to choose 2–5 section titles. Fallback to minimal `{headline:"…", sections:[]}` on JSON parse failure.

### `pipeline/storage.py`
- `save_aim(aim)`, `get_aim(aim_id)`, `update_aim(aim)`, `delete_aim(aim_id)`, `list_aims_for_user(user_id)`.
- `save_digest(digest)`, `get_digest(digest_id)`.
- `save_raw_articles(articles, job_id)` → `data/raw/{job_id}.json`.
- `get_seen_article_ids()` — union across `data/raw/*.json` (Tier 1 dedup source of truth).
- **Swap path:** Firestore. One file. See [DECISIONS D3](DECISIONS.md).

### `pipeline/_util.py`
- `safe_llm_json(raw, expected_key, expected_len) -> list` — one helper, used by every structured LLM call. Strips markdown fences, handles JSON-stringified arrays, truncates over-length lists, raises on under-length.

## Dedup — three-tier strategy (Tier 1 + 3 live)

| Tier | Catches | Cost | Live in prototype? |
|---|---|---|---|
| 1. Exact URL md5 | Same URL re-crawled | O(1) set lookup | **Yes** — `storage.get_seen_article_ids()` |
| 2. MinHash LSH over 5-word shingles | "Press release reprinted by 5 outlets with minor edits" | `datasketch`, ~30 LOC | No — talked-about in DECISIONS D13 |
| 3. Semantic cosine > 0.93 | "AP story rewritten by WSJ & Reuters" | Free (reuses chunk embedding, one `top_k=1` query before upsert) | **Yes** — in `vector_store.upsert_chunks` |

Tiers 1 + 3 cover the common cases with minimal moving parts. Tier 2 earns its pay above ~100k docs/day when embedding every candidate before indexing becomes expensive; below that, Tier 3's reuse of the chunking embedding makes it strictly cheaper. See [DECISIONS D13](DECISIONS.md).

## Hybrid retrieval = structured filter ∩ semantic search

**The single strongest RAG signal in the build.** Aim's structured fields (`regions`, `source_types`) are not prompt content — they're **filter dimensions** indexed into Pinecone metadata at ingest. Retrieval applies:

```python
filter = {"region": {"$in": [*aim.regions, "Global"]}}
# Phase 5 (optional): also filter on source_type if aim.source_types is set
```

Why not just pass regions in the prompt? Because rerank can't overrule what was never retrieved. If a TechCrunch chunk about a US acquisition leaks past the filter, the rerank prompt has to spend a token budget saying "reject off-region content" — and it occasionally fails. With the Pinecone filter, the LLM never sees those chunks; rerank scores *quality within the region*, not *regionality itself*.

**Interview framing for the HoE's "why do Aims have a `regions` field — isn't that just prompt content?":** *"Regions aren't prompt content — they're filter dimensions. Chunks carry `region` + `source_type` metadata at ingest, and retrieval applies `{"region": {"$in": [*aim.regions, "Global"]}}` as a Pinecone filter before the vector search. Hybrid retrieval — structured filter ∩ semantic search. The rerank can't overrule what was never retrieved, which is the whole point. I include Global alongside user regions so a Global-tagged 'CEE VC outlook' piece still serves a regional Aim."*

## Multi-stage ranking (addressing brief suggestion #1)

```
RETRIEVE         FILTER              RERANK              DIVERSIFY          GENERATE
Pinecone ANN  →  recency + source →  gpt-4o-mini JSON →  MMR λ=0.7       →  gpt-4o-mini JSON
top_k=30         weight + sim       top_n=15             top_k=10            1 digest
                 blend (cheap)      (expensive)          (cheap)             (expensive)
```

**Why each stage earns its pay:**

- **Retrieve (Pinecone ANN)** — fast, broad net. Hybrid filter pre-constrains by region/source_type so we don't waste rerank budget on off-Aim chunks.
- **Filter (cheap score)** — `0.55 × sim + 0.30 × recency_halflife_24h + 0.15 × source_weight`. Prunes to 20–40 candidates before the expensive call.
- **Rerank (LLM)** — the precision layer. Sees the full structured Aim + each candidate's 200-word excerpt; returns scores 0–10. Penalises staleness, rewards novelty and primary sources. `gpt-4o-mini` is ~15× cheaper than `gpt-4o` for structured-output tasks where the delta is usually small ([DECISIONS D11](DECISIONS.md)).
- **Diversify (MMR)** — prevents 10 items about the same event. `λ=0.7` = 70% relevance / 30% novelty. Cheap: cosine over already-computed embeddings.
- **Generate (LLM)** — one call with the top 10. `response_format="json_object"`. LLM chooses 2–5 section titles to fit the content (not a fixed enum). Every item cites `source_urls` drawn from its source chunks.

**Cost napkin math (for the show-&-tell):** at 1k users × 200 candidates × 500 tokens on `gpt-4o`, naive = ~$160k/day. With retrieve→filter→rerank funnel + `gpt-4o-mini` rerank on 40 + `gpt-4o` only for final digest generation = single-digit-thousands/day. ~85% reduction, per [Pinecone's published benchmark](https://www.pinecone.io/blog/introducing-reranking-to-pinecone-inference/).

## Failure isolation

| Failure | Handling |
|---|---|
| One RSS feed fails (404, DNS, SSL) | `try/except` per source in `ingest_all_sources`; convert `feed.bozo_exception` to raised exception so tenacity retries; log and continue |
| Trafilatura returns empty / 429 | Fall back to RSS `entry.summary`; skip if total <200 chars. Log per-source success rate — see [LESSONS § Per-source success rates](LESSONS.md#per-source-success-rates). |
| OpenAI embedding batch fails | Tenacity retry; if persistent, log failed chunk IDs and continue (partial upsert is better than no upsert) |
| Pinecone upsert fails | Retry once; log and continue — retrieval uses whatever did upsert |
| LLM rerank returns non-JSON or wrong shape | `safe_llm_json` handles truncate/fence-strip; on hard failure fall back to vector-search order, **logged visibly** — see [LESSONS § LLM output-shape handling](LESSONS.md#llm-output-shape-handling) |
| LLM digest returns non-JSON | `json.loads` in try/except; minimal fallback `{headline:"Digest generation failed", sections:[]}` |
| Digest has 0 sections (empty ingestion) | Surface `status:"complete"` with `headline:"No new coverage this run"` and empty `sections` |

**Principle:** the pipeline reports *partial success* rather than failing entirely. A digest with 2 sections is better than no digest.

**Silent fallbacks are worse than hard failures** — they look like success. Every fallback path logs visibly + bumps a counter that's surfaced in the per-run funnel metrics line.

## Inspectable intermediate outputs (addressing brief suggestion #4)

Every stage logs a funnel line:
```
INGESTED=412  EXTRACTED=380  DEDUPED_T1=270  CHUNKED=1840  EMBEDDED=1840
UPSERTED=1712  DEDUPED_T3=128  RETRIEVED=30  RERANKED=15  FINAL=10
```

Every stage writes raw JSON:
- `data/raw/{job_id}.json` — raw articles post-extraction
- `data/digests/{digest_id}.json` — final digest
- `data/compare/phase{N}_{variant}.json` — before/after snapshots per quality-changing phase (committed — interview exhibits)

`scripts/compare_digests.py` diffs any two digest JSONs and prints: section count, item count, unique URL count, distinct hosts, region coverage, item_type mix, URL Jaccard. *That table* is the answer to "did it get better?"

## Scaling talking points (addressing brief suggestion #3)

If asked "how would you scale this to Aim's real 10k docs/day × 1k users?":

1. **`SOURCES` moves to Firestore** — adding a source = `POST /sources` (Mexican state institutions = one Firestore doc + a line of `@register("mexico_gov")` code, zero pipeline changes).
2. **Ingestion** — Cloud Run workers triggered by Cloud Scheduler → Pub/Sub fan-out, one message per source batch. DLQ with 5 attempts, 10s→600s exp backoff.
3. **Per-source rate limits** — Cloud Tasks queues (e.g., SEC 10 req/s per ToS). Pub/Sub for fan-out, Cloud Tasks for directed rate-limited dispatch.
4. **Embedding throughput** — VertexAI `RETRIEVAL_DOCUMENT` task type (better retrieval quality than OpenAI for indexing, symmetric doc/query asymmetry). Batched embedding API gives 50% discount.
5. **Dedup at scale** — add MinHash LSH (Tier 2) between exact-URL and semantic checks; embedding-cosine alone becomes expensive at 100k+ new chunks/day.
6. **Rerank at scale** — self-host `bge-reranker-v2-m3` on Cloud Run GPU; zero per-call cost. Or batch rerank across users with overlapping theses.
7. **Per-user ranking** — rank at off-peak per user timezone (Cloud Scheduler per timezone). Cache thesis vectors. Pre-cluster users by thesis overlap to share candidate pools.
8. **Observability** — log all LLM latencies + token counts to BigQuery. Kendall tau between vector order and rerank order, weekly per aim. Per-source success rate.
9. **Multi-tenancy** — Pinecone namespaces `global` (all docs) + `user:{uid}:context` (click history for personalisation blend). Namespace cap at 100k/index → shard via tenant→index mapping table for Enterprise plan.

**Trade-off to say out loud:** "Dataflow + Beam would unify batch/streaming and give exactly-once semantics, but at Aim scale (10k/day) Cloud Run + Pub/Sub is simpler, cheaper, and faster to iterate. Move to Dataflow when exactly-once or cross-pipeline windowing becomes non-negotiable."

## Tech choice summary

| Layer | Choice | Alternative rejected | See |
|---|---|---|---|
| Nouns / schema | Aim + Digest | UserProfile + Report | [D10](DECISIONS.md#d10-adopt-aims-real-product-nouns) |
| Vector store | Pinecone serverless | BigQuery VECTOR_SEARCH | [D1](DECISIONS.md#d1-pinecone-over-bigquery-vector-search) |
| Embeddings | OpenAI `text-embedding-3-small` | VertexAI `text-embedding-004` | [D2](DECISIONS.md#d2-openai-embeddings-over-vertexai) |
| Aim/digest store | Local JSON | Firestore | [D3](DECISIONS.md#d3-local-json-over-firestore) |
| Async execution | FastAPI BackgroundTasks | Pub/Sub + Cloud Run | [D4](DECISIONS.md#d4-backgroundtasks-over-pubsub) |
| Source scope | Text-only, 3+ live connector types + stubs | All 8 source types from the brief | [D6](DECISIONS.md#d6-text-only-sources-no-videoaudio) |
| GCP | Deferred to Phase 5 | In core path | [D7](DECISIONS.md#d7-defer-gcp-to-phase-5) |
| Rerank | `gpt-4o-mini` JSON mode | `gpt-4o` / no-rerank | [D11](DECISIONS.md#d11-gpt-4o-mini-default-gpt-4o-upgrade) |
| Dedup | Tier 1 (md5) + Tier 3 (embedding cos>0.93) | Add Tier 2 MinHash | [D13](DECISIONS.md#d13-dedup-tiers-1--3-live-tier-2-talked-about) |
| Frontend | Vanilla HTML/CSS/JS | React/Vue/Svelte + build step | [D14](DECISIONS.md#d14-vanilla-frontend) |

## Ports / paths

- API: `http://localhost:4444`
- Data dir: `./data/` (gitignored except `compare/`) — subdirs: `aims/`, `digests/`, `raw/`, `compare/`
- Env: `./.env` (gitignored); `./credentials.json` for GCP (gitignored)
