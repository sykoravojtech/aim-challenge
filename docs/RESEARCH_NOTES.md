# Senior-Engineer Plan: 8-Hour Data Pipeline Design & Prototype for Aim (startaiming.com)

**Target:** Build an end-to-end multi-stage ingestion → enrichment → ranking → briefing pipeline that ingests ~10k heterogeneous documents/day and delivers ~10 personalized daily insights per user. Stack: GCP + BigQuery + Firestore + Pinecone + LLMs.

**Context on Aim (startaiming.com):** Aim is a "personal AI business briefing" service that tracks a user's "business bubble" across platforms, podcasts, and languages, for VCs, founders, and business owners. It combines LLMs with custom classification to separate signal from noise per-user ([startaiming.com](https://www.startaiming.com/), [Vestbee](https://www.vestbee.com/blog/articles/aim-secures-300k)). That positioning — **per-user theses, cross-language sources including podcasts, and a daily short digest** — is the crucial design anchor. Your 8-hour prototype should make those three things visibly work, even at tiny scale.

---

## 1. Recommended Architecture (Stage-by-Stage)

```
                          ┌────────────────────────────────────┐
 Cloud Scheduler (cron) ─▶│ STAGE 1: INGESTION (per-source CR) │
                          │ RSS · X/Reddit · YT · SEC · Congress│
                          │ · Firecrawl/Trafilatura crawlers    │
                          └───────────────┬────────────────────┘
                                          │ publish RawDoc{url, source, raw_text, fetched_at, hash}
                                          ▼
                                 ┌──────────────────┐
                                 │  Pub/Sub: raw.*  │──▶ DLQ (raw.dlq) on 5 fails
                                 └────────┬─────────┘
                                          ▼
                    ┌──────────────────────────────────────────┐
                    │ STAGE 2: NORMALIZE + DEDUPE (Cloud Run)  │
                    │  trafilatura clean · lang-detect ·       │
                    │  SHA256 (exact) · MinHash LSH (near) ·   │
                    │  embedding cosine (semantic)             │
                    └───────┬───────────────────┬──────────────┘
                            │ GCS (raw HTML)    │
                            ▼                   ▼
                     gs://raw/<src>/...   Pub/Sub: clean.docs
                                                 │
                                                 ▼
                    ┌──────────────────────────────────────────┐
                    │ STAGE 3: ENRICHMENT (Cloud Run worker)   │
                    │  LLM entity/topic/sentiment · chunk ·    │
                    │  embed (OpenAI text-embed-3-small 1536d) │
                    └───────┬──────────────────┬───────────────┘
                            │                  │
                            ▼                  ▼
                     BigQuery.docs      Pinecone (global index,
                     (append-only        namespace=GLOBAL;
                      analytic store)    per-user ns for userctx)
                                                 │
                                                 ▼
                    ┌──────────────────────────────────────────┐
                    │ STAGE 4: PER-USER RANKING (Cloud Run)    │
                    │  for each user (Firestore):              │
                    │   a. retrieve top 100 from Pinecone      │
                    │      using user-thesis embedding         │
                    │   b. cheap filter: recency + source      │
                    │      weight + sim score                  │
                    │   c. LLM rerank top 25 → top 10          │
                    └───────┬──────────────────────────────────┘
                            ▼
                     Firestore.briefings/{user}/{date}
                     BigQuery.ranking_events (audit)
                                 │
                                 ▼
                    ┌──────────────────────────────────────────┐
                    │ STAGE 5: DELIVERY (Cloud Run + Scheduler)│
                    │  LLM digest generator (summary+why+links)│
                    │  Email/Slack/web API                     │
                    └──────────────────────────────────────────┘
```

This mirrors the Pinecone-canonical **retrieve → filter → rerank** funnel, which reduces input cost by up to 85% vs. passing all candidates to a top-tier LLM ([Pinecone](https://www.pinecone.io/blog/introducing-reranking-to-pinecone-inference/)), and the Google-canonical **Pub/Sub + Cloud Run + DLQ** pattern with exponential backoff ([Google Cloud docs](https://docs.cloud.google.com/pubsub/docs/subscription-retry-policy)).

---

## 2. GCP Service Choices & Justification

| Stage | Service | Why (and what it beats) |
|---|---|---|
| Scheduling | **Cloud Scheduler → Pub/Sub** | Cron-style fan-out trigger. Scheduler publishes `{source_id}` messages; Pub/Sub fans them to per-source workers. Free tier generous. |
| Per-source fetching | **Cloud Run (services)** | Long-running HTTP scrapes, YouTube downloads, Whisper calls often exceed Cloud Functions Gen 1 timeouts; Cloud Run handles concurrency per instance and custom containers (ffmpeg, yt-dlp, Playwright) cleanly ([Reintech comparison](https://reintech.io/blog/google-cloud-functions-vs-cloud-run-when-to-use-each)). Note Cloud Functions was rebranded as **Cloud Run Functions** in Aug 2024 — they share infra now, so prefer Cloud Run services for anything non-trivial ([Modal blog](https://modal.com/blog/google-cloud-run-vs-google-cloud-function-article)). |
| Message bus (fan-out) | **Pub/Sub** | Topic-per-stage pattern (`raw.docs`, `clean.docs`, `enriched.docs`, `ranked.briefings`). Exponential-backoff retry (10s→600s) + DLQ after N attempts is native ([Google Cloud docs](https://docs.cloud.google.com/pubsub/docs/subscription-retry-policy)). |
| Rate-limited per-user work | **Cloud Tasks** | Use for user-specific work where you need **rate control and explicit endpoint targeting** (e.g., "call LLM at ≤15 req/s"). Pub/Sub is fan-out, Cloud Tasks is directed dispatch ([GCP docs](https://docs.cloud.google.com/pubsub/docs/choosing-pubsub-or-cloud-tasks)). Pair them: Pub/Sub ingests, Cloud Tasks dispatches LLM calls with `max-dispatches-per-second`. |
| Batch/streaming transforms | **Dataflow (optional, post-MVP)** | Only add when volumes grow. Dataflow's Pub/Sub connector supports exactly-once and `ID-attribute` dedupe inside a 10-minute window ([GCP docs](https://docs.cloud.google.com/dataflow/docs/concepts/streaming-with-cloud-pubsub)). For 10k docs/day a Cloud Run worker pool is cheaper and simpler. Don't over-engineer with Beam in the prototype. |
| Object store | **GCS** | Raw HTML, audio files, transcripts. Content-addressable paths: `gs://aim-raw/{source}/{yyyy}/{mm}/{dd}/{sha256}.html`. |
| Analytic store | **BigQuery** | Append-only `documents`, `entities`, `ranking_events`, `delivery_events`. Columnar, cheap for analytics, bad for ops ([Google Cloud blog](https://cloud.google.com/blog/topics/developers-practitioners/databases-google-cloud-part-2-options-glance/)). |
| Operational store | **Firestore** | Per-user config, thesis, source subscriptions, delivery state, today's briefing. Real-time listeners, low-latency reads, NoSQL flexibility ([DB-Engines comparison/GCP guidance](https://cloud.google.com/blog/topics/developers-practitioners/databases-google-cloud-part-2-options-glance/)). Use the Firebase→BigQuery extension for nightly sync to analytics ([Firebase docs](https://firebase.google.com/docs/firestore/solutions/bigquery)). |
| Vector store | **Pinecone (serverless)** | One index `aim-docs` (dim=1536 for `text-embedding-3-small`). **Namespace = `global` for all documents; namespace = `user:{uid}` only for a user's private documents / past briefings.** Per-tenant namespaces offer physical isolation and tenant-specific query latency ([Pinecone multi-tenancy docs](https://docs.pinecone.io/guides/index-data/implement-multitenancy)). |
| LLM access | **Vertex AI Gemini** (for summarization) + **OpenAI via API** (for reranking/embedding) | Keep provider-agnostic via a thin wrapper. Vertex gives IAM-native GCP calls; OpenAI embeddings (`text-embedding-3-small` @ $0.02/1M tokens or `-3-large` @ $0.13/1M tokens, 3072 dims) are the production baseline for RAG ([OpenAI](https://openai.com/index/new-embedding-models-and-api-updates/)). |
| Observability | **Cloud Logging + Cloud Monitoring** | Standard; expose custom counter metrics from each Cloud Run worker (malformed, deduped, enriched, ranked). |

**Key trade-off to say out loud in the show & tell:** You could run everything on Dataflow for unified batch/streaming, but for a startup at thousands-of-docs scale Cloud Run is simpler, cheaper, and faster to iterate ([NetApp summary](https://www.netapp.com/learn/gcp-cvo-blg-google-cloud-dataflow-the-basics-and-4-critical-best-practices/)). Move to Dataflow when volume or exactly-once becomes non-negotiable.

---

## 3. Data Model: What Goes Where

### BigQuery (append-only, analytic)
- `raw_documents(doc_id, source, url, fetched_at, sha256, gcs_uri, language, status)`
- `clean_documents(doc_id, title, body, author, published_at, canonical_url, word_count, lang, entities[], topics[])`
- `document_chunks(chunk_id, doc_id, text, embedding_model, token_count)`
- `dedup_events(doc_id, dup_of_doc_id, method, score)` — audit trail for near-dup/semantic-dup decisions
- `ranking_events(user_id, doc_id, stage, score, rank, ts)` — crucial for future ML training / A-B testing
- `delivery_events(user_id, briefing_id, channel, delivered_at, opened_at, clicks)`

### Firestore (operational, low-latency reads/writes)
- `users/{uid}` → `{email, timezone, delivery_channel, source_prefs, theses:[{title, description, keywords, weight}]}`
- `users/{uid}/thesis_embeddings/{tid}` → cached embedding vector
- `users/{uid}/briefings/{yyyy-mm-dd}` → final ranked list + generated digest text
- `sources/{source_id}` → `{type, config, cron, enabled, auth_secret_ref, last_crawled_at}` — the **connector registry**; adding a new source = adding a Firestore doc

### GCS
- `gs://aim-raw/...` raw pages, audio files, transcripts (immutable, content-addressed)

### Pinecone
- Index `aim-docs`, dim=1536, cosine
  - Namespace `global`: one vector per chunk (≤512 tokens, ~250-word target [Unstructured best practices](https://unstructured.io/blog/chunking-for-rag-best-practices)), metadata `{doc_id, source, published_at, lang, topics[], entities[]}`
  - Namespace `user:{uid}:context`: user's thesis embeddings and historical clicks (for personalization without noisy neighbors — namespaces give physical isolation per tenant ([Pinecone](https://www.pinecone.io/learn/series/vector-databases-in-production-for-busy-engineers/vector-database-multi-tenancy/))).

---

## 4. Multi-Stage Pipeline Design Details

### Stage 1 — Ingestion (pluggable connectors)

Define a single Python ABC every source implements:

```python
class SourceConnector(Protocol):
    source_id: str
    def list_new_items(self, since: datetime) -> Iterable[ItemRef]: ...
    def fetch(self, ref: ItemRef) -> RawDoc: ...  # returns {url, raw_html_or_text, meta}
```

Concrete connectors for the prototype:
- **RSS / blogs** → `feedparser` ([Python wiki](https://wiki.python.org/moin/RssLibraries)); fall back to `trafilatura` for full-text extraction — it is the top-F1 open-source article extractor (0.945 F1 on ScrapingHub benchmark) and outperforms `newspaper3k`, `readability-lxml` ([trafilatura benchmark](https://trafilatura.readthedocs.io/en/latest/evaluation.html)).
- **Reddit** → `praw` (LangChain also has a `RedditPostsLoader` wrapper ([LangChain docs](https://python.langchain.com/docs/integrations/document_loaders/reddit))).
- **X / Twitter** → v2 API with bearer token; stub if no key.
- **LinkedIn** → no clean public API; **stub in prototype**, flag as risk.
- **YouTube** → `yt-dlp` to grab audio, OpenAI Whisper API or `spoken.md` for transcription ([spoken.md](https://spoken.md/)). For speed in the demo, prefer `youtube-transcript-api` for channels that publish captions, fall back to Whisper for podcasts ([Gladia](https://www.gladia.io/blog/building-a-whisper-youtube-transcription-generator-for-automated-captioning)).
- **Podcasts** → RSS → mp3 → Whisper → text.
- **US Congress** → `api.congress.gov` (free, requires key, JSON).
- **SEC EDGAR** → `data.sec.gov/submissions/CIK{n}.json` + full-text search endpoint; user-agent required.
- **Generic web / company sites** → Firecrawl for JS-heavy sites, Trafilatura for static ([DEV.to](https://dev.to/murroughfoley/how-to-use-rs-trafilatura-with-firecrawl-36p9)).

All connectors emit a `RawDoc` Pub/Sub message with an **`id_attribute = sha256(canonical_url)`** so that Dataflow/Pub/Sub can dedupe publisher retries within the 10-minute window ([GCP docs](https://docs.cloud.google.com/dataflow/docs/concepts/streaming-with-cloud-pubsub)).

### Stage 2 — Normalization + Deduplication

Three layers, cheap → expensive:

1. **Exact dedupe** — SHA256 of canonical URL and of normalized text. Store in Firestore `seen_hashes` or BigQuery; O(1) lookup.
2. **Near-duplicate (MinHash LSH)** — Standard banding technique. For English news, industry-tested parameters from BigCode and others are:
   - Word or 5-char shingles, ~100–128 MinHash permutations
   - Bands `b=16`, rows `r=8` → targets Jaccard ≈ 0.75–0.85 threshold
   - Practical production config: `{shingle_k=5, hash_bits=128, signature_size=96, bands=18, rows_per_band=7, jaccard_threshold=0.85}` ([dev.to case study](https://dev.to/schiffer_kate_18420bf9766/my-battle-against-training-data-duplicates-implementing-minhash-lsh-at-scale-3nab), [HuggingFace BigCode](https://huggingface.co/blog/dedup))
   - Use `datasketch` library; cluster via union-find.
3. **Semantic dedupe (embedding)** — only for candidate pairs MinHash flags as borderline, or for cross-source "same story different outlet" detection: cosine similarity on 1536-d embeddings with threshold ≈ 0.92–0.95 (empirically tuned; RETSim paper uses 0.10 cosine distance ≈ 0.90 similarity as near-dup threshold ([arXiv](https://arxiv.org/html/2311.17264))).

**Important senior-engineer note to mention in the demo:** MinHash is cheap and good for "republished press releases"; embedding cosine catches "AP story rewritten by 5 outlets." Use *both*; don't replace one with the other ([Milvus blog on why both matter](https://milvus.io/blog/minhash-lsh-in-milvus-the-secret-weapon-for-fighting-duplicates-in-llm-training-data.md)).

### Stage 3 — Enrichment

Per clean doc:
- Language detect (`fasttext-langdetect`), auto-translate non-English to English for embeddings (mention this — Aim emphasizes multi-language ingestion, per [Vestbee writeup](https://www.vestbee.com/blog/articles/aim-secures-300k)).
- Entity + topic extraction via a single-call LLM prompt returning JSON. A "single-call LLM enrichment" approach (title, summary, keywords, typed entities, hypothetical questions) is recognized as best-practice and an order of magnitude cheaper than running separate NER/summarization models ([MDKeyChunker paper](https://arxiv.org/pdf/2603.23533)).
- Chunk text to ~250–512 tokens with 10–20% overlap — the empirically best default for RAG recall ([Firecrawl chunking guide](https://www.firecrawl.dev/blog/best-chunking-strategies-rag), [Unstructured](https://unstructured.io/blog/chunking-for-rag-best-practices)). Preserve tables/code blocks as atomic units ([NVIDIA](https://developer.nvidia.com/blog/finding-the-best-chunking-strategy-for-accurate-ai-responses/)).
- Embed with `text-embedding-3-small` in the prototype (1536 dim, $0.02/1M tokens, 2048 docs/batch). Upgrade path to `-3-large` (3072 dim, $0.13/1M tokens) for quality-critical users ([OpenAI announcement](https://openai.com/index/new-embedding-models-and-api-updates/)).
- Upsert chunks to Pinecone namespace `global` with metadata.
- Insert row in BigQuery `clean_documents`.

### Stage 4 — Per-User Ranking (retrieve → filter → rerank)

For each user's cron trigger:

```
# 1. Build user query vector from thesis
user_vec = mean([embed(thesis.description + keywords) for thesis in user.theses])
# (cache in Firestore, recompute on thesis change)

# 2. Pinecone retrieve — top 200 candidates from last 24–72h
candidates = pinecone.query(
    vector=user_vec,
    top_k=200,
    namespace="global",
    filter={"published_at": {"$gte": now-72h},
            "source": {"$in": user.source_prefs}})

# 3. Cheap filter: recency + source_weight + sim blend
scored = [ (0.5*c.score + 0.3*recency(c) + 0.2*source_weight(c.source)) for c in candidates ]
top_40 = take_top_by_score(scored, 40)

# 4. Cross-encoder / LLM rerank top 40 → top 15
reranked = llm_rerank(user_theses, top_40)  # see prompt below

# 5. Diversity: MMR or topic/entity diversity pass to avoid 10 bullets about the same event
final_10 = mmr(reranked, lambda_=0.7)[:10]
```

The two-stage **retrieve-then-rerank** pattern is now industry standard (Cohere Rerank, Pinecone reranking, bge-reranker-v2-m3) and lifts Hit@1 by ~20pp in benchmarks ([AIMultiple benchmark](https://aimultiple.com/rerankers)). Cost example: passing 75 candidates of 500 tokens each to GPT-4o vs. passing 20 after reranking cuts daily input cost ~72% while preserving 95% of accuracy ([ZeroEntropy](https://zeroentropy.dev/articles/ultimate-guide-to-choosing-the-best-reranking-model-in-2025/)).

**Rerank prompt (the trickiest part — worth showing):**

```
SYSTEM: You are a ruthless editor for a paid executive briefing for {persona}.
Score each candidate 0–10 for how likely it is to materially change this
person's business decisions *today*. Penalize: stale news, opinion without data,
duplicates of obvious stories. Reward: novel signal, primary sources,
competitor/market moves directly in scope. Return JSON: [{id, score, one_line_why}]
USER THESES: {theses_json}
CANDIDATES (id, source, date, title, 200-word excerpt):
{candidates}
```

Use `gpt-4o-mini` or `claude-haiku` for reranking (cheap, fast, accurate enough). Reserve the top-tier model only for digest generation.

### Stage 5 — Insight Generation and Delivery

Per-user daily digest prompt pattern uses **element-aware summary chain-of-thought** (two-stage: first extract entities/dates/events, then compose). This reduces hallucination and preserves detail vs. direct summarization in news summarization benchmarks ([ACL paper](https://aclanthology.org/2023.acl-long.482.pdf), [CoTHSSum](https://link.springer.com/article/10.1007/s44443-025-00041-2)).

**Digest prompt:**
```
SYSTEM: You write Aim's daily executive briefing for {user.name}, a
{user.persona}. Their theses:
{theses_json}

For each of the 10 items below:
1) Extract entities, dates, and the single most important event.
2) Write one sentence stating the *signal* (what changed, why it matters to the thesis it hits).
3) Write one sentence stating the *so-what* (concrete action or question for the user).
4) Include source link + original publication time.

Constraints: <= 60 words per item; no cliches; no "in today's fast-moving world";
never hallucinate — if source unclear, say "according to {source}". Output
markdown.
```

Note: chain-of-thought reasoning only meaningfully helps on models ≥~100B params ([Wei et al.](https://openreview.net/pdf?id=_VjQlMeSB_J)); use direct element extraction for Haiku/mini-class models, explicit CoT for GPT-4-class.

**Delivery:** Firestore write + email/Slack push (Cloud Run endpoint called by Cloud Scheduler per user timezone).

---

## 5. Fault Tolerance Patterns

- **Every Pub/Sub subscription**: retry policy `min-retry-delay=10s, max-retry-delay=600s`, DLQ topic with `max-delivery-attempts=5` ([GCP docs](https://docs.cloud.google.com/pubsub/docs/subscription-retry-policy)).
- **Distinguish transient vs. permanent errors in the worker** — bad JSON schema = `ack()` + log to `invalid_messages` topic; API 5xx = `nack()` for retry. Don't let poison messages loop forever ([OneUptime guide](https://oneuptime.com/blog/post/2026-02-17-how-to-implement-retry-logic-and-error-handling-in-python-pubsub-subscribers-with-dead-letter-topics/view)).
- **Application-level exponential backoff with jitter** inside workers for LLM/embedding API 429s (`base * 2^n + uniform(0, 0.1*base)`).
- **Idempotency via content hash** as the Pub/Sub `id_attribute` so retries within 10 min dedupe for free.
- **Circuit breaker** on LLM provider (e.g., `pybreaker`): if OpenAI fails >30% in 2 min window, fail-over to Vertex AI Gemini for embedding/rerank.
- **Per-source rate limits** via **Cloud Tasks** queues with `max-dispatches-per-second` (e.g., SEC = 10 req/s max per their TOS) ([Medium](https://medium.com/google-cloud/cloud-tasks-or-pub-sub-8dcca67e2f7a)).
- **DLQ replayer Cloud Function**: subscribes to DLQ, counts retries, republishes with backoff, gives up to an "ops DLQ" after N attempts ([replay pattern](https://omermahgoub.medium.com/replaying-messages-with-pub-sub-dead-letter-and-cloud-functions-9cec9a0152d0)).

---

## 6. The 8-Hour Prototype Plan (Time-Boxed)

**Philosophy:** One user, one day, three source types, fully end-to-end. Everything else stubbed. A senior engineer demonstrates *architectural instincts* and *cost/quality trade-offs*, not breadth of integrations.

### Tech stack for the prototype
- **Python 3.11**, **FastAPI** for any HTTP handlers (Cloud Run friendly)
- **Pydantic** models for `RawDoc`, `CleanDoc`, `Chunk`, `Insight`
- `feedparser`, `trafilatura`, `praw`, `yt-dlp`, `openai`, `pinecone-client`, `google-cloud-*`, `datasketch`, `tenacity`
- **One Docker image** with multiple entrypoints (`python -m aim.worker.{ingest|enrich|rank|digest}`) — deployed as separate Cloud Run services in a production build but runnable locally via `make demo`.
- **Terraform** snippet (optional, impressive) defining the Pub/Sub topic + DLQ + subscription retry policy — even if you don't apply it, having the IaC in the repo signals seniority.

### Hour-by-hour plan

| Hr | Work | Definition of done |
|---|---|---|
| **0:00–0:30** | Scaffold repo. `src/aim/{connectors,pipeline,ranking,digest,storage}`; Pydantic models; `.env`; Makefile with `make demo`. Create Pinecone serverless index, GCP project (use local creds, skip full deploy). | `python -m aim.main --help` runs. |
| **0:30–2:00** | **Stage 1: 3 connectors.** (a) RSS via `feedparser` + `trafilatura` on ~5 tech/business feeds; (b) HackerNews API or Reddit `r/investing` via `praw`; (c) **One SEC 8-K filing feed** from `data.sec.gov`. Each returns `list[RawDoc]`. Seed with ~300–500 docs. | `python -m aim.ingest` prints N docs fetched per source. |
| **2:00–3:00** | **Stage 2: Normalize + dedupe.** `trafilatura.extract`, lang detect, SHA256 exact-dup check (in-memory set for demo), **MinHash LSH** via `datasketch.MinHashLSH(threshold=0.85, num_perm=128)` for near-dup. Log dedupe stats. | Console shows `300 raw → 270 after exact → 245 after MinHash`. |
| **3:00–4:00** | **Stage 3: Enrichment.** One LLM call per doc: `gpt-4o-mini` with JSON mode returning `{title, summary_150w, entities, topics, sentiment}`. Chunk body to 400-token windows. Embed chunks with `text-embedding-3-small`. Upsert to Pinecone namespace `global`. Insert a row into a local SQLite/DuckDB table mimicking BigQuery `clean_documents`. | Pinecone index shows N vectors; DuckDB has rows. |
| **4:00–5:30** | **Stage 4: Ranking.** Hardcode **2 sample users** in a JSON fixture: (user1: "AI infrastructure VC" — theses: foundation models, inference, GPUs; user2: "Fintech founder" — theses: payments, SEC enforcement, interest rates). For each: build thesis embedding → Pinecone top-100 → recency+source filter → `gpt-4o-mini` rerank top-25 → MMR diversity → top-10. Log each stage's candidates to show the funnel. | JSON file `briefings/{date}/{user}.json` with 10 insights per user. |
| **5:30–6:30** | **Stage 5: Digest.** LLM prompt (element-aware CoT from §4) produces markdown digest. Simple CLI renderer and an HTML file. | `briefings/{date}/{user}.md` opens prettily. |
| **6:30–7:15** | **Demo glue + observability.** Add `structlog` with JSON output; write a 1-page `STATS.md` auto-generated at the end: `"Ingested 412 docs · 167 deduped · 245 enriched · top-10 selected for 2 users · total LLM cost $X.XX"`. This is what senior engineers notice. | Single `make demo` runs the whole pipeline in <5 min end-to-end. |
| **7:15–8:00** | **Show-&-tell prep.** Architecture diagram (excalidraw/mermaid), README with "what I'd build next", slide with cost napkin-math for 10k docs/day × 1k users. Rehearse the 10-min demo. | Slide deck + README. |

### What to stub vs. build
| Build | Stub/Mock |
|---|---|
| RSS + Reddit + SEC ingest | LinkedIn (mention legal risk), X (needs paid API), Congress.gov (same shape as SEC) |
| MinHash + embedding dedupe | Full Dataflow pipeline — use in-process LSH |
| Pinecone global namespace | Per-user-context namespace — show it with ONE demo user's click history pre-loaded |
| LLM rerank + digest for 2 users | Scaling to 1k users — show napkin math |
| Retry decorators with exponential backoff (`tenacity`) | Full Pub/Sub + DLQ wiring — show the Terraform config in the repo, don't run it |
| Cost log at end | Real billing — estimate via token counts |

---

## 7. Code Patterns for the Trickiest Parts

### 7a. Semantic + near-dup dedupe (production-shaped)

```python
from datasketch import MinHash, MinHashLSH
import numpy as np

class Deduper:
    def __init__(self, lsh_threshold=0.85, embed_threshold=0.93):
        self.seen_hashes: set[str] = set()              # exact
        self.lsh = MinHashLSH(threshold=lsh_threshold, num_perm=128)
        self.embed_threshold = embed_threshold

    @staticmethod
    def _shingles(text: str, k=5):
        toks = text.lower().split()
        return {" ".join(toks[i:i+k]) for i in range(len(toks)-k+1)}

    @staticmethod
    def _minhash(shingles):
        m = MinHash(num_perm=128)
        for s in shingles: m.update(s.encode())
        return m

    def check(self, doc_id: str, text_hash: str, text: str,
              embedding: np.ndarray, embed_index: "PineconeLike") -> DedupResult:
        if text_hash in self.seen_hashes:
            return DedupResult.EXACT_DUP
        self.seen_hashes.add(text_hash)

        mh = self._minhash(self._shingles(text))
        near_dups = self.lsh.query(mh)
        self.lsh.insert(doc_id, mh)
        if near_dups:
            return DedupResult.NEAR_DUP(near_dups[0])

        # semantic check only on remaining docs, k=1 top match
        hits = embed_index.query(embedding, top_k=1, namespace="global")
        if hits and hits[0].score > self.embed_threshold:
            return DedupResult.SEMANTIC_DUP(hits[0].id)

        return DedupResult.NEW
```

### 7b. Retrieve → rerank → diversify

```python
def build_user_briefing(user: User, window_hours=72, k=10) -> list[Insight]:
    # Combine thesis embeddings with weights
    thesis_vecs = [w * embed(t.description) for t, w in zip(user.theses, user.weights)]
    user_vec = l2_normalize(np.sum(thesis_vecs, axis=0))

    # Stage 1: Pinecone ANN
    candidates = pinecone.query(
        vector=user_vec, top_k=200, namespace="global",
        filter={"published_at": {"$gte": hours_ago(window_hours)},
                "lang": {"$in": user.languages}},
        include_metadata=True)

    # Stage 2: cheap scoring
    def cheap(c):
        rec = exp(-hours_since(c.metadata["published_at"]) / 24)   # half-life 1 day
        src = user.source_weights.get(c.metadata["source"], 0.5)
        return 0.55*c.score + 0.30*rec + 0.15*src
    top40 = sorted(candidates, key=cheap, reverse=True)[:40]

    # Stage 3: LLM rerank (batched)
    scored = llm_rerank(user.theses, top40)                       # returns [(c, llm_score, why)]

    # Stage 4: MMR diversity on embeddings to avoid redundancy
    selected = mmr(scored, lambda_=0.7, k=15)
    # Stage 5: cap 1 item per cluster of same "event" (entity overlap >= 60%)
    return dedup_by_event(selected)[:k]
```

### 7c. Pluggable connector registry

```python
# Firestore doc → Python connector
REGISTRY: dict[str, type[SourceConnector]] = {}
def register(name): 
    def deco(cls): REGISTRY[name] = cls; return cls
    return deco

@register("rss")
class RSSConnector(BaseConnector):
    def list_new_items(self, since): 
        for entry in feedparser.parse(self.cfg["url"]).entries:
            if dateutil.parse(entry.published) > since:
                yield ItemRef(id=entry.id, url=entry.link, meta=entry)
    def fetch(self, ref): 
        html = requests.get(ref.url).text
        return RawDoc(url=ref.url, raw_text=trafilatura.extract(html), ...)

# Adding a new source = add Firestore doc {type:"sec_edgar", cik:"0000320193"}
# + register("sec_edgar") class. Zero pipeline changes.
```

### 7d. User context → Pinecone personalization

Two complementary approaches Aim should support:
1. **Thesis-vector retrieval** (above) — cold-start friendly, explainable.
2. **Behavioral retrieval** via per-user namespace — store embeddings of articles the user clicked/saved. At ranking time blend: `user_vec = 0.6*thesis_mean + 0.4*click_mean`. This mirrors the LECOP/MIND-style news-rec pattern that consistently beats thesis-only or behavior-only ([arXiv 2411.06046](https://arxiv.org/abs/2411.06046)).

---

## 8. Biggest Architectural Risks & Mitigations

| Risk | Why it bites | Mitigation |
|---|---|---|
| **Source flakiness & bans** (X, LinkedIn, paywalled news) | Breaks ingestion; connector failures cascade | Per-source circuit breakers; DLQ with human review; graceful degradation (drop source for the day, log to `delivery_events` that it was excluded). |
| **LLM cost explosion at scale** | 10k docs × 1k users × GPT-4 rerank = $$$ | The retrieve→filter→rerank funnel; small models for reranking (`gpt-4o-mini`, Cohere Rerank, `bge-reranker-v2-m3` self-hosted ~$0/call on GPU) — Pinecone docs show 85% cost reduction vs naive passing ([Pinecone](https://www.pinecone.io/blog/introducing-reranking-to-pinecone-inference/)). Batch embedding API gives 50% discount ([OpenAI](https://openai.com/index/new-embedding-models-and-api-updates/)). |
| **Duplication leakage** into daily briefing ("10 insights about the same story") | Kills product quality | Three-tier dedupe (§4, Stage 2) + MMR diversity + event-clustering on entity overlap in the final step. |
| **User thesis drift / cold start** | New users get bad recs | Seed on signup: embed 3–5 "example articles they'd love", store in Firestore. Ask for explicit thesis text; recompute thesis vector on edit. |
| **Hallucinated summaries** | Brand risk for paid exec briefing | Never ask LLM to summarize from memory — always pass the source text; include literal source URL; include a `confidence` field in the rerank output; prompt-constrain with "if source unclear, say 'according to {source}'". |
| **Per-user ranking latency at 1k+ users** | N users × 200 Pinecone reads × LLM calls = slow + expensive | Batch the embedding + Pinecone queries; rank at off-peak hours (Cloud Scheduler per timezone); cache thesis vectors; pre-cluster users by thesis overlap and share the candidate pool. |
| **Whisper/podcast cost & latency** | One hour of audio ≈ 1–5 min transcription | Keep podcasts on a slower cron (hourly → daily); use `youtube-transcript-api` first, Whisper fallback; consider `spoken.md` API at $0.08–0.15/transcript to avoid self-hosting GPUs ([spoken.md](https://spoken.md/)). |
| **Namespace limits in Pinecone** | Enterprise plan caps at 100k namespaces/index | For user-scale scenarios, shard via multiple indexes with a tenant→index mapping table ([Pinecone multi-tenancy](https://www.pinecone.io/learn/series/vector-databases-in-production-for-busy-engineers/vector-database-multi-tenancy/)). |
| **Legal/ToS** (LinkedIn, X scraping; publisher content) | Existential risk | Prefer licensed feeds (Reuters, AFP partnerships — which is exactly the path [Particle took](https://techcrunch.com/2025/05/06/particle-brings-its-ai-powered-news-reader-to-the-web/)); respect robots.txt; store only excerpts + link-back. |

---

## 9. The 10-Minute Show-&-Tell: Talking Points

**Open (1 min) — Frame the product problem.** "Aim promises a daily briefing that beats a full-time analyst. The engineering problem is: ingest thousands of heterogeneous docs, find ~10 that matter to *this* user's theses, and do it cheaply enough to scale. My 8-hour prototype proves the end-to-end architecture with RSS, Reddit, and SEC as source types and two synthetic user personas."

**Walkthrough (4 min) — Run the demo.** `make demo` → watch the funnel numbers: `412 raw → 245 clean → 2× top-10 digests`. Show the two markdown briefings side-by-side: the VC gets GPU/AI items, the fintech founder gets SEC enforcement items. Open the rerank logs to show *why* each item scored what it did.

**Design choices (3 min) — the three decisions a senior engineer makes.**
1. **Pub/Sub + Cloud Run per stage, not a Dataflow monolith.** "At 10k docs/day Beam is overkill; Cloud Run with topic-per-stage gives me cheap horizontal scale, DLQs per stage, and independent deploys. I'd migrate to Dataflow when we need exactly-once or cross-pipeline windowing."
2. **Retrieve → filter → rerank, not "throw it all at GPT-4."** "Cost napkin: 1k users × 200 candidates × 500 tokens via GPT-4o would be ~$160k/day. With `text-embedding-3-small` for retrieval and `gpt-4o-mini` reranking only 40 candidates, we land in single-digit thousands/day ([ZeroEntropy benchmarks show ~72% reduction](https://zeroentropy.dev/articles/ultimate-guide-to-choosing-the-best-reranking-model-in-2025/))."
3. **BigQuery is append-only audit; Firestore is the live product.** "`ranking_events` in BigQuery gives us the dataset to later train a real cross-encoder personalized to Aim's users. Firestore holds live theses and today's briefing with real-time listeners for the UI."

**Risks & what's next (2 min).** "Biggest risks are source-ToS (LinkedIn), LLM cost at 10x scale, and duplicate-in-digest. In the next sprint I'd: (a) add Whisper-based podcast ingestion — this is Aim's real moat vs. competitors; (b) build a bge-reranker service on Cloud Run GPU to zero out per-rerank cost; (c) add click-through feedback into per-user Pinecone namespaces so ranking improves weekly; (d) add Terraform+CI so stages deploy independently."

**Close.** "What you saw is intentionally small — 2 users, 3 sources, 8 hours — but every component is the real one: trafilatura is what HuggingFace/IBM use, the MinHash params match BigCode's, the retrieve-rerank pattern matches Cohere and Pinecone's own recommendations. Swapping in Dataflow or bge-reranker is a migration, not a rewrite."

---

## 10. Quick Reference: "If I only do this right, it's fine"

1. **One repo, one Docker image, multiple entrypoints.** Don't scatter 10 microservices across 8 hours.
2. **Use Pydantic models** as the contract between stages — it's your documentation.
3. **Log funnel metrics aggressively** (`412 → 245 → 40 → 10`). Demo gold.
4. **Hardcode 2 users with thoughtfully different theses** — the contrast sells the personalization.
5. **Don't try to make it pretty.** Markdown CLI is enough; spend the time on dedupe + rerank logic.
6. **Show the cost math.** A senior engineer always knows what their system costs.
7. **Write the `NEXT_STEPS.md`** — Dataflow migration, bge-reranker, user feedback loop, Terraform. It shows you thought past the 8-hour boundary.

---

### Appendix: Key References Used

- Pinecone multi-tenancy & namespaces — [docs.pinecone.io](https://docs.pinecone.io/guides/index-data/implement-multitenancy), [pinecone.io](https://www.pinecone.io/learn/series/vector-databases-in-production-for-busy-engineers/vector-database-multi-tenancy/)
- Retrieve→rerank pattern and cost math — [Pinecone](https://www.pinecone.io/blog/introducing-reranking-to-pinecone-inference/), [ZeroEntropy](https://zeroentropy.dev/articles/ultimate-guide-to-choosing-the-best-reranking-model-in-2025/), [AIMultiple reranker benchmark](https://aimultiple.com/rerankers)
- MinHash LSH parameters & scale — [HuggingFace BigCode](https://huggingface.co/blog/dedup), [dev.to at-scale case](https://dev.to/schiffer_kate_18420bf9766/my-battle-against-training-data-duplicates-implementing-minhash-lsh-at-scale-3nab), [Milvus on combining with semantic](https://milvus.io/blog/minhash-lsh-in-milvus-the-secret-weapon-for-fighting-duplicates-in-llm-training-data.md)
- GCP Pub/Sub retry + DLQ — [GCP docs](https://docs.cloud.google.com/pubsub/docs/subscription-retry-policy), [Dataflow + Pub/Sub dedupe](https://docs.cloud.google.com/dataflow/docs/concepts/streaming-with-cloud-pubsub)
- Pub/Sub vs Cloud Tasks — [GCP comparison](https://docs.cloud.google.com/pubsub/docs/choosing-pubsub-or-cloud-tasks)
- Cloud Run vs Cloud Functions — [Modal](https://modal.com/blog/google-cloud-run-vs-google-cloud-function-article), [Reintech](https://reintech.io/blog/google-cloud-functions-vs-cloud-run-when-to-use-each)
- BigQuery vs Firestore — [GCP blog](https://cloud.google.com/blog/topics/developers-practitioners/databases-google-cloud-part-2-options-glance/), [Firebase→BigQuery](https://firebase.google.com/docs/firestore/solutions/bigquery)
- Trafilatura is best open-source article extractor — [benchmarks](https://trafilatura.readthedocs.io/en/latest/evaluation.html), [ScrapingHub](https://github.com/scrapinghub/article-extraction-benchmark)
- Chunking for RAG best practices — [Unstructured](https://unstructured.io/blog/chunking-for-rag-best-practices), [Firecrawl](https://www.firecrawl.dev/blog/best-chunking-strategies-rag), [NVIDIA](https://developer.nvidia.com/blog/finding-the-best-chunking-strategy-for-accurate-ai-responses/)
- Summary Chain-of-Thought for news — [ACL 2023](https://aclanthology.org/2023.acl-long.482.pdf), [CoTHSSum](https://link.springer.com/article/10.1007/s44443-025-00041-2)
- OpenAI embeddings pricing/dims — [OpenAI announcement](https://openai.com/index/new-embedding-models-and-api-updates/)
- Whisper/YouTube/podcast pipelines — [Gladia](https://www.gladia.io/blog/building-a-whisper-youtube-transcription-generator-for-automated-captioning), [spoken.md](https://spoken.md/)
- Personalized news recommendation using LLM embeddings — [arXiv 2411.06046](https://arxiv.org/abs/2411.06046)
- Aim product context — [startaiming.com](https://www.startaiming.com/), [Vestbee](https://www.vestbee.com/blog/articles/aim-secures-300k)
- Comparable products for benchmarking your pitch — [Particle](https://techcrunch.com/2024/02/29/former-twitter-engineers-are-building-particle-an-ai-powered-news-reader/), [Artifact](https://en.wikipedia.org/wiki/Artifact_(app))