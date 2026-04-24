# Lessons log

Things discovered by *running* the pipeline — gotchas, silent failures, API quirks, rate limits, data shapes the docs didn't warn about.

**How this differs from [DECISIONS.md](DECISIONS.md):** decisions are forward-looking choices; lessons are backward-looking facts about the world. If a lesson invalidates a decision, update DECISIONS.md and cross-link.

Format per entry: **What surprised me** — Context — What to do about it. Prefix with the phase it was found in.

---

## Standing idioms — patterns to apply from call #1

These are the defensive patterns worth baking in before any code runs. Each was painful enough to earn a rule; write them once and reuse.

### LLM output-shape handling

Treat LLM output-shape bugs as the default, not the exception. Every structured-output LLM call in this codebase routes through a single `pipeline/_util.safe_llm_json(raw, expected_key, expected_len)` helper that handles:

- **Array length overage** (N+1 for N items): log + truncate to the first N. The model sometimes appends a summary/mean element; dropping the trailing one is safe.
- **Array length underage**: fail loudly — cannot safely pad with sentinel scores.
- **JSON wrapped in markdown fences** (` ```json … ``` `): strip fences before `json.loads`.
- **Keys with different casing**: normalise to lowercase before `.get()`.
- **Arrays returned as JSON-stringified strings** (`"[1,2,3]"`): `isinstance(scores, str): scores = json.loads(scores)`.

Silent fallbacks are worse than hard failures — they look like success. Every fallback path bumps a counter that shows up in the funnel metrics line.

### OpenAI JSON mode

`response_format={"type": "json_object"}` is not sufficient on its own. If the prompt does not contain the literal word "JSON" (case-insensitive), the API returns a 400 `messages must contain the word 'json' in some form`. Every JSON-mode call's system prompt includes a phrase like "Respond with a JSON object matching this schema:" to cover it.

### `feedparser` silent failures

`feedparser.parse(url)` almost never raises. DNS failures, connection resets, 404s served as HTML — all look like a "successful" call returning `feed.entries = []` with `feed.bozo = 1` and the real exception stashed on `feed.bozo_exception`. A `@tenacity.retry` decorator won't fire because no exception bubbles out.

Rule: in `ingestion._parse_feed`, check `feed.bozo_exception` and re-raise it when `feed.entries` is empty. This converts silent failures into retryable exceptions without false-positives on "harmless" bozo feeds (the `bozo` bit also fires on unescaped ampersands in otherwise-valid XML — we don't want to retry those).

### Per-source success rates

Watch success rate *per source*, not overall. A single source at 0% extraction is a signal, not noise — historical example: a rate-limited news site returning HTTP 429 silently as `trafilatura.fetch_url → None`, zero exceptions, zero logs, 7/7 articles dropped. The overall success rate still looked acceptable; per-source was dead.

Log `ingested / extracted / used` per source at the end of every run.

### Rerank's precondition

Rerank's precision gain is conditional on candidate-pool diversity. With a thin pool, rerank does not refuse — it *fragments* content (two sections about the same article, different angles, because the LLM wants to fill the structure it was asked to produce). On a rich pool with region-weighted sources, rerank earns its pay; without them, it can make a digest measurably worse than a no-rerank baseline.

Rule: before adding rerank, verify the pool has at least ~3× as many distinct on-Aim articles as target final items. If final items = 5, pool needs ~15 on-Aim candidates pre-rerank.

---

## Today's lessons (append as they happen)

## L1. Three of the ten ROADMAP RSS feeds were silently empty at 08:30

**Phase:** 0

**What surprised me:** `federalregister.gov` SEC search.rss (302 → non-RSS payload, `bozo=1`, 0 entries), `hn.cz/feed` (404 HTML), and `euvc.com/feed` (HTML-not-XML, `bozo=1` undefined entity) all return `feed.entries=[]` with no exception — exactly the silent-empty trap from the standing idiom. Caught by the pre-flight smoke-test subagent, so cost was 0 min; without the smoke test these would have silently produced 0 chunks each for regions/source_types that matter.

**Context:** `ROADMAP § Source plan` hardcodes 10 feeds; 3 were dead on arrival. The replacements shipped in `scripts/phase0_skeleton.py` (and mirrored into `pipeline/ingestion.py:SOURCES` for the live API) are: `sec.gov/news/pressreleases.rss` (US regulatory), `lupa.cz/rss/clanky/` (Czechia news), `therecursive.com/feed/` (CEE news). The replacements give 4 Czech/CEE feeds, keeping the two demo Aims' pools balanced.

**What to do about it:** Before trusting any hardcoded feed list, run a parallel smoke-test (one script, 10 URLs, print `status/entries/bozo` per feed). Any feed that returns `entries=[]` *at all* is dead to us — don't debug, swap. Keep `therecursive.com`, `lupa.cz`, `sec.gov/news/pressreleases.rss` as known-good CEE/US substitutes.

## L2. trafilatura 403s on SEC + VentureBeat are invisible unless RSS-summary fallback is wired

**Phase:** 0

**What surprised me:** 15/77 `trafilatura.fetch_url` calls hit 403 (all 8 SEC press-release pages, all 7 VentureBeat article pages). With the RSS-`entry.summary` fallback already in `RSSConnector.fetch`, every one of those 15 still produced a usable doc — per-source `used` counts matched `listed` — so a glance at the funnel line would miss the entire failure class.

**Context:** SEC returns 403 to any generic python-requests User-Agent regardless of the one declared to feedparser; VentureBeat seems to Cloudflare-block non-browser UAs on article pages but serves a generous `<content:encoded>` in the feed. Both are saved by the ≥200-char RSS-summary fallback.

**What to do about it:** Log per-source `fetched_via={trafilatura|rss_summary}` counts, not just `used`. A source whose docs all come from `rss_summary` is fine for a skeleton but risky at scale (RSS summaries truncate; chunks carry less signal than full extractions). Keep watching the per-source fetch-method split when Phase 2 wires the real tenacity retries.

## L3. `mode=cached` latency is LLM-generate-dominated, not pipeline-dominated

**Phase:** 1

**What surprised me:** ROADMAP targeted `cached` at `<10 s`. Measured end-to-end: **17 s** (retrieve ~3 s, generate ~14 s). The generate call is a synchronous `gpt-4o-mini` JSON completion over 20 chunks of 600-char excerpts — intrinsic LLM-latency, not pipeline waste. Shaving retrieve won't help; the dominant cost is the LLM call itself.

**Context:** The demo point was never "cached is under 10 seconds" — it's "cached shows what the production architecture looks like: ingest is a scheduled Cloud Run Job on a cron, the digest endpoint is read-only against Pinecone." The interesting number is the **delta** vs `force`: **83 s → 17 s, a ~5× drop** with zero change in output quality. That ratio is the demo story; the absolute `<10 s` target was aspirational.

**What to do about it:** Don't bite the prompt size to chase `<10 s` — the cost comes straight out of digest quality (fewer chunks in context = fewer sourcing options for the LLM). Reframe the demo script to lead with the 5× delta, not the absolute wall-clock. If a later phase wants to squeeze more, the lever is **prompt caching of the Aim + static system message** (saves input tokens + some TTFT) or the **`reasoning: "low"` hint** on newer models, not chunk-count reduction.

## L4. Tier 3 semantic dedup catches arxiv more than news

**Phase:** 2

**What surprised me:** The first Tier-3-enabled `force` run flagged **9/623 chunks** as semantic dupes. 7 of the 9 were arxiv abstracts matching *other* arxiv abstracts at cosine 1.000 — arxiv's `cs.AI` RSS publishes abstracts with boilerplate phrasing ("We propose…", "In this paper we show…") that embeds near-identically. The other 2 were within-TechCrunch front-matter (shared editor-note preamble bytes across two articles). **Zero** semantic dupes across *news sources* (cc.cz vs forbes.cz vs techcrunch), which was the case D13's interview note sold on ("AP story rewritten by Reuters + WSJ with same facts") — the 10-source demo pool doesn't have that failure mode at volume.

**Context:** `SEMANTIC_DUP_THRESHOLD = 0.93` fired hardest on: two arxiv papers whose chunk-0 = title + abstract + DOI boilerplate; one TechCrunch pair sharing a newsletter-subscription footer chunk. All legit catches — the boilerplate really is the same content — but it's not the "cross-outlet event coverage" story the demo implies.

**What to do about it:** Two framing tweaks for the demo:
- **Lead with the cheap-talk version of the decision**: "Tier 1 is the workhorse — 77/78 skips in run 2. Tier 3 is the *insurance* that catches what Tier 1 can't see, mostly arxiv boilerplate in the current source mix. The 'AP rewritten by Reuters' story needs a denser wire-service source mix to show up in numbers."
- **Don't oversell** Tier 3 in the DECISIONS interview note without this caveat — D13 currently implies cross-outlet rewrites; at the current source mix that's talked-about, not measured. Append a cross-ref to this lesson on D13.

## L5. MMR over rerank trades mean relevance for section coverage

**Phase:** 4

**What surprised me:** The `rerank_only` snapshot (retrieve 30 → rerank to 15 → generate top 15) produced a **mean relevance of 8.00** across 3 items in 2 sections; the `full` snapshot (same pipe + MMR λ=0.7 to 10 → generate top 10) produced **mean 7.00** across 4 items in 3 sections. The "worse" number on paper is the better artefact — MMR pushed out a cluster of near-duplicate high-score chunks and made room for a third section ("AI Landscape Developments") that rerank-alone had collapsed into "Startup Ecosystem Insights". URL Jaccard between the two was 0.75 (3 of 4 shared).

**Context:** With a 10-feed cached Pinecone state (~600 chunks), the rerank stage saturates the top of the pool on a narrow topic — MMR does exactly what it's designed to: trade some top-end relevance for breadth. `λ=0.7` is the ROADMAP-prescribed default; a sweep is afternoon-sized, not today-sized.

**What to do about it:** Lead the demo with the compare tool, not the raw digest. Say it aloud: *"mean relevance drops 8.00→7.00 because MMR is doing its job — that's the section coverage buying you 3 sections instead of 2 at the cost of swapping one high-score near-duplicate for a lower-but-still-on-Aim chunk."* If mean relevance ever goes up with MMR on, the candidate pool was thinner than it looks — re-check the retrieve top_k before blaming rerank.

## L6. Cloud Run europe-west3 → Pinecone serverless has 6–10 s per-query roundtrips

**Phase:** 5

**What surprised me:** First `incremental` run on the deployed Cloud Run service ingested 78 articles cleanly, mirrored them to BigQuery + GCS in ~6 s, then **stalled in the upsert stage for >10 minutes**. Laptop runs of the same path complete in ~60–90 s. Cloud Run logs show the Tier 3 semantic-dedup loop (`semantic_dup of {id}`) emitting one line every **6–10 s per chunk** — the per-chunk `top_k=1` Pinecone query is paying a full cross-region roundtrip from `europe-west3` to Pinecone's AWS-hosted serverless index on every call. Locally that round-trip is <200 ms; Cloud-Run-to-Pinecone is 30–50× slower.

**Context:** Pinecone serverless is hosted on AWS `us-east-1` / `us-west-2` by default (no europe-west3 option when the index was created). The laptop isn't noticeably faster per-hop, but its dedup loop batches much less aggressively and still finishes because the laptop isn't paying inter-cloud egress. On Cloud Run, 78 articles × ~4 chunks each = ~300 per-chunk queries × 8 s average = ~40 min just for Tier 3 dedup. BQ + GCS writes finished long before that because both are same-region (europe-west3 / us-central1 same-continent) and the payload is one blob / one batched insert.

**What to do about it:** Three mitigations, any combination:
1. **Batch the Tier 3 check.** Instead of one `top_k=1` per chunk, batch N chunks into a single multi-vector query (`index.query` accepts one vector; use `index.fetch` across a candidate set of article_ids we already have in the seen-set). Reduces ~300 roundtrips to ~10 batched ones. ~1 hour to wire.
2. **Skip Tier 3 in `incremental` mode on the deployed service.** Tier 1 URL md5 already catches ~99% of real duplicates (77/78 on the L-measured re-run); Tier 3 is insurance. Gate it behind a flag and flip off on Cloud Run — lose measured-semantic-dedup coverage, keep `incremental` under 2 min.
3. **Move Pinecone index to `eu-west1` AWS** (Pinecone serverless supports it). Same cost, ~100 ms roundtrips instead of 8 s. Requires full reindex — afternoon-sized, not today-sized.

**Demo implication today:** use `cached` mode for the live Cloud Run demo. It's the read-only path — retrieve from existing Pinecone state → rerank → generate, 30 s cold-start, no cross-region upsert loop. `incremental` + `force` still work, just slow; show them on the laptop if asked, point at this lesson as the named "what's next with an hour" bullet.

## L7. gpt-4o-mini rerank degrades silently above ~80 chunks

**Phase:** 6H

**What surprised me:** Raising `per_article_cap` from 1 → 3 fed **119 chunks** into rerank, and gpt-4o-mini consistently failed to produce a valid `{"scores": [...]}` JSON response. Output either truncated mid-array at char ~28K ("line 4096 column 7") or ran away into verbose multi-line formatting. The original `safe_llm_json` path then hit `LLMShapeError` and fell back to `chunks[:top_n]` in **vector order** — which silently wipes the entire rerank stage. Digest looked OK on surface (no exception, items generated) but was effectively un-reranked.

**Context:** The prompt asks for one integer 0–10 per chunk. With 40 chunks that's trivially in-distribution. At 80 chunks the model sometimes drops or appends items (over/under by 1–3, which `safe_llm_json` already handles). At 119 the model consistently either truncates or inserts commentary/whitespace until `max_tokens` cuts it mid-array.

**What to do about it:**
- **Cap rerank input size.** `per_article_cap=2` (~80 chunks) is the stable operating point. `per_article_cap=3` is tested-bad even with recovery — the padded-score mean drops to ~2.9 vs ~5.2 fully-scored.
- **Don't silent-fallback to vector order.** Added `_recover_scores` in `pipeline/retrieval.py`: tries JSON parse, then regex-extracts ints from `"scores": [ ... `, pads with neutral=5 up to `expected_len`. Surfaces "rerank recovered with padding" at WARN level so the failure is visible instead of masked.
- **Explicit `max_tokens` on the rerank call.** `min(2000, 200 + 12 * len(chunks))` — hard ceiling against runaway generation. Model can still drop scores, but can't emit 28KB of noise.
- **Pinecone reranker (`cohere-rerank-3.5` / `bge-reranker-v2-m3`) is the real fix.** Cross-encoder models return a deterministic score-per-pair — no LLM shape-drift at all. Named next-step in DEMO_NOTES § 5; ~30-LOC swap in `rerank_chunks`.

**Demo line:** *"Raising chunks-per-article into rerank exposed that gpt-4o-mini silently degrades above ~80 inputs — valid-looking JSON but missing scores. I added regex-based recovery so the failure is visible (warn log + padded neutrals) instead of silent-fallback to vector order. Right fix is swapping to a cross-encoder reranker — that's the 30-LOC next step."*

## L8. SEC + Congress.gov feeds return stub text; saas recall is ingest-bound

**Phase:** 6H

**What surprised me:** Ran a corpus profiler across `data/raw/*.json` (189 docs across 7 batches) looking for why saas-ai-legislation recall stays stochastic at 0.00–0.17 across every retrieval change we've shipped. The numbers: **news median 3.3KB, p90 10KB. Research median 2.1KB. Regulatory (SEC EDGAR) median 253 characters. Legislation (Congress.gov via GovTrack) median 384 characters.** The sources the saas Aim most depends on ingest as ~one-sentence summaries, not full filings or bill text.

**Context:** `RSSConnector` + trafilatura handles HTML news well. SEC EDGAR's press-release RSS returns a headline + 1–2 sentence summary; the actual 10-K/8-K body lives behind a separate "filing index" URL in the submissions JSON. Congress.gov/GovTrack's summary field is a one-paragraph abstract, with full bill XML at a separate endpoint. We're not hitting those endpoints. Consequence: retrieval can't rank what isn't ingested — multi-chunk context, semantic clustering, better rerankers all cap at "best possible ranking of one-sentence stubs."

**What to do about it:** Don't tune retrieval for saas further until ingest is fixed. The real fix is **~100 LOC across `pipeline/ingestion.py::SECConnector.fetch_text` + `CongressConnector.fetch_text`** — follow SEC's submissions-JSON → filing-index chain to pull 10-K/8-K body text; call Congress.gov's `/bill/{congress}/{type}/{number}/text` endpoint to pull bill XML. Re-chunk, re-upsert, re-eval. Named next-step in DEMO_NOTES § 5.

**Demo line:** *"When the 6H retrieval fix lifted CEE but not saas, I dropped down and profiled the corpus. SEC feed median: 253 characters. Congress feed median: 384. Multi-chunk retrieval can't help what was never ingested. That's a one-day connector fix, not a retrieval problem — it's the #1 'what's next' bullet because it's the saas ceiling in a literal sense."*

