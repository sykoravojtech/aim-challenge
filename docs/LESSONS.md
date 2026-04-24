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

**Context:** `ROADMAP § Source plan` hardcodes 10 feeds; 3 were dead on arrival. The replacements shipped in `scripts/run_pipeline.py` are: `sec.gov/news/pressreleases.rss` (US regulatory), `lupa.cz/rss/clanky/` (Czechia news), `therecursive.com/feed/` (CEE news). The replacements give 4 Czech/CEE feeds, keeping the two demo Aims' pools balanced.

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

