# Eval summary — both demo Aims

Quality measured the same way everywhere: **recall@k** against a hand-labelled golden set + **LLM-as-judge** (`gpt-4o-mini`) scoring each digest item on `{relevance, specificity, non-duplication}` 1–5. Harness: [`scripts/eval_digest.py`](../../scripts/eval_digest.py). Golden set: [`evals/golden.jsonl`](../../evals/golden.jsonl).

The point isn't that every phase beat the previous — it's that **every claim in the demo is now a measured number**, including the ones that don't go the way you'd expect.

## What each metric means

| metric | what it measures | higher means | lower means |
|---|---|---|---|
| **recall@k** | of the articles a human labelled "should appear," what fraction actually made it into the top-k digest | the pipeline is finding the right articles | good articles are being missed — retrieval, rerank, or filter is dropping them |
| **precision** | of the digest URLs that are in the golden set at all, what fraction are labelled positive (not negative) | no false positives promoted to the digest | the digest is surfacing articles a human labelled as "should NOT appear" |
| **relevance** (1–5) | judge's read of how on-topic each item is to the Aim | items genuinely match the Aim's intent | items drift to adjacent topics |
| **specificity** (1–5) | judge's read of how concrete each item is — named entities, numbers, dates vs. vague framing | items are newsworthy and detailed | items are generic / press-release soup |
| **non-duplication** (1–5) | judge's read of whether each item says something distinct from the others in the digest | the digest covers different angles / events | dedup + MMR failed — multiple items cover the same story |
| **n_items** | how many items ended up in the digest | — | 0 is a *silent failure signal* — the pipeline ran but produced nothing |

**Reading the numbers together matters more than any single one.** Rerank can lift non-duplication while dropping recall — that's the diversity-vs-coverage trade. A high relevance score on tiny `n_items` just means the few survivors happened to be on-topic, not that the pipeline is good.

## Aim 1 — `cee-founder-media` (phase-over-phase)

### Scorecard

| phase | recall@k | precision | relevance | specificity | non-dup | n_items |
|---|---|---|---|---|---|---|
| phase0 walking skeleton | 0.33 | 1.00 | 2.75 | 3.75 | 3.75 | 4 |
| phase1 API version      | 0.44 | 1.00 | 3.50 | 4.00 | 4.25 | 4 |
| phase2 dedup            | —    | —    | —    | —    | —    | 0 (skipped) |
| phase4 rerank only      | 0.33 | 1.00 | 3.33 | 4.00 | 4.67 | 3 |
| phase4 full (MMR)       | 0.44 | 1.00 | 3.25 | 3.75 | 4.50 | 4 |

Precision is 1.00 everywhere because every digest URL present in the golden set is a labelled positive — no false positives surfaced. Recall is the number doing real work.

## What the numbers actually say

**phase0 → phase1 is the biggest jump.** Relevance 2.75 → 3.50, non-dup 3.75 → 4.25, recall 0.33 → 0.44. The API path enforces a tighter Aim→retrieval coupling than the skeleton script, which pulled a visibly more on-topic set. This is the single largest quality delta in the build.

**phase4 rerank alone isn't strictly better than phase1.** Recall dropped 0.44 → 0.33 and relevance dipped 3.50 → 3.33, while non-dup climbed 4.25 → 4.67. Honest read: pure rerank over-collapsed near-duplicates and dropped a distinct-topic positive. This is the kind of finding you only see with evals — without them the rerank stage looks like an unambiguous win because the output "reads better."

**MMR repairs the recall regression.** phase4 rerank → phase4 full: recall 0.33 → 0.44, non-dup holds at 4.50, relevance trades a hair (3.33 → 3.25). This is the diversity-vs-peak-relevance trade working as designed — MMR re-admits a distinct positive that rerank had suppressed.

## Aim 2 — `saas-ai-legislation` (current pipeline snapshot)

Historical phase compare artifacts were only captured for the CEE Aim, so no phase-over-phase sweep is possible here. The most recent saas digest (current pipeline state, equivalent to `phase4_full`) was evaluated against the 9-row golden subset for this Aim.

### Scorecard — before/after wiring GovTrack legislation connector

| phase | recall@k | precision | relevance | specificity | non-dup | n_items |
|---|---|---|---|---|---|---|
| phase4_full (saas, RSS-only)       | **0.00** | — | 2.50 | 3.50 | **4.00** | 2 |
| phase6_congress (GovTrack live)    | **0.00** | — | **3.00** | **4.00** | 3.67 | **3** |

**Judge notes from the phase6 run:** the top item was *"Introduction of H.R. 8470: Surveillance Accountability Act"* — a real US Congress bill surfaced through the new GovTrack connector, scoring **relevance 5, specificity 4, non-dup 5** (a perfect top-slot item). Did not exist in the RSS-only run.

### Why recall@k didn't move — the real reason

Initial hypothesis was "golden URLs rolled off the RSS feeds" — that was wrong. Checked: **all 6 golden positives are ingested, embedded, and live in Pinecone** (they're in `data/raw/*.json`, written through to GCS bronze + BigQuery `raw_articles` per Phase 5). The corpus has the right documents.

So the bug is **downstream of retrieval**: rerank and/or MMR are actively dropping them in favour of tech-news items. Three plausible culprits, all testable:

1. **Rerank scores SEC press-release prose lower than TechCrunch-style headlines** because the latter read as "newsier" to `gpt-4o-mini`. The rerank prompt doesn't weight `source_type=regulatory` explicitly.
2. **MMR over-diversifies a cluster.** Five of six golden positives are SEC press releases — once one lands in the diverse top-10, MMR penalises the others for similarity, knocking all five out.
3. **Recency tilt in the cheap-filter stage** pushes 2-day-old SEC filings below this morning's OpenAI product posts.

This is a better finding than "the URLs expired" — it's a concrete, prioritisable lead inside the pipeline, filed as ROADMAP Phase 6 follow-up.

### Why LLM-judge scores moved despite recall stalling

Judge doesn't look at the golden URL list — it evaluates *whatever items the digest returned* against *the Aim's intent*. So when a Congress bill reached the top slot at 5/5 relevance + 4 specificity + 5 non-dup, that moved the averages even though the golden positives got ranked out. The two metrics measure different things:

- **recall@k** → *did we retrieve the right documents?* (URL set intersection, cheap, time-invariant)
- **LLM-judge** → *is what we returned on-topic?* (scores whatever showed up, agnostic to golden set)

Reading them together is how you learn that the corpus fix *worked* (judge up) but exposed a *ranking bug* (recall still 0) that's the next thing to fix.

### Note on eval infrastructure

A lingering worry — "what if golden URLs disappear from source feeds later?" — is already mooted by Phase 5 infra: **GCS bronze stores raw article payloads, BigQuery `raw_articles` stores the clean-extracted version**. Golden labels can be pinned to `article_id` (md5-of-URL) and reconstituted from either store. First-week improvement: swap `evals/golden.jsonl` from URL-only to `{article_id, source_url, content_hash}` triples backed by GCS lookup. The snapshot infra is there; the harness just isn't using it yet.

### What this surfaces (and it's the most interesting finding in the whole eval)

**Recall is zero.** Of 6 golden positives for this Aim, the digest hit none. The two items that *did* land (OpenAI Privacy Filter, OpenAI Workspace Agents) are OpenAI product news — not legislation, not SEC filings, not Congressional commentary. The judge agrees: relevance 2.50 / 5 is the lowest score in the whole matrix.

This is **topical drift** — the classic failure mode of semantic retrieval. The Aim's summary mentions "AI companies" and the embedding space pulls anything with "AI" in the headline, outranking actual legislative content on cosine similarity alone. Without evals, this digest reads fine to a human skimmer — it's coherent, recent, well-written. Only the golden set exposes that it's answering the wrong question.

### Root cause hypothesis

1. **RSS feed list is thin on legislation sources.** The Federal Register regulatory feed is one of ten; the other nine skew tech-news. Retrieval can only surface what was ingested.
2. **Congress.gov connector is stubbed, not live.** The brief's #1 example source isn't wired — so every Congressional bill, hearing, committee mention is invisible to the pipeline.
3. **No region/source_type weighting in retrieval.** A TechCrunch article about AI ranks as high as a SEC filing about AI — the `source_type` metadata is filtered on but not weighted.

Fixes 1 and 2 are in the "next with a week" column already; fix 3 is a 1-hour tweak to the retrieval scorer. The eval harness is what makes this a prioritizable bug instead of an abstract worry.

## Honest gaps to call out in the demo

- **phase2 dedup produced 0 items** on this run. That's the happy-path collapse — the dedup heuristic was too aggressive on this corpus snapshot. The harness surfacing this is exactly the point: a silent regression became a visible one.
- **Golden set is 20 rows** (11 CEE + 9 saas). Numbers at this sample size are directional, not statistically stable. First week of scaling is "grow the golden set to 200+ and ship a weekly eval job."
- **No phase-over-phase history for the saas Aim** — compare artifacts weren't captured for it. A clean first-week fix is running every phase's digest config against every demo Aim as part of CI.

## Demo framing

> *"The brief asks for ~10 relevant insights out of 10k docs. I didn't want to claim that without measuring it, so every digest gets a recall@k number and an LLM-as-judge score on three axes. Three findings the evals drove: pure rerank hurt recall on the CEE Aim until MMR repaired it; the SaaS-AI-Legislation Aim read fine but scored 2.50/5 relevance because the feed list was all tech-news; when I wired a GovTrack legislation connector, judge relevance moved 2.50 → 3.00 and a real Congress bill took the top slot at 5/5 — but recall stayed 0. That recall stall is actually the most interesting finding: I checked, and the 6 golden SEC press releases are all in Pinecone. So rerank or MMR is dropping them. That's a concrete ranking-stage bug, filed as follow-up, and it's exactly the kind of thing you'd never find without evals because the digest reads fine."*
