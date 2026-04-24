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

<!--
Template:

## L{N}. Short title

**Phase:** {phase number}

**What surprised me:** …

**Context:** …

**What to do about it:** …

-->
