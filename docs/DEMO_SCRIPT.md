# 10-min demo script

Cue card for the 16:45 slot. Four beats from the brief: *how it works, why, what's next, risks.* Source-of-truth is [DEMO_NOTES.md](DEMO_NOTES.md) — this is the teleprompter.

**Live URL:** https://aim-645297577758.europe-west3.run.app
**Repo:** github.com/sykoravojtech/aim-challenge
**Backup:** laptop `uv run uvicorn main:app --reload --port 4444`

---

## 0 · Open (30 s)

> *"One-day prototype of Aim's pipeline. Two Aims live — a CEE founder-media brief and a SaaS-AI-legislation brief. I'll walk the 8-verb spine, show the GCP services, then close on what the eval harness caught."*

**Show:** browser on live Cloud Run URL → landing page with both Aim cards + screenshots `docs/screenshots/01_landing_both_aims.png`.

---

## 1 · How it works — the 8-verb spine (2 min)

> *"Ingest → extract → chunk → embed → store → retrieve → rerank → generate. Each verb is one module in `pipeline/`, each emits Pydantic, each can be unit-tested in isolation. The whole thing runs end-to-end as a `BackgroundTask` off one FastAPI handler."*

**Click** the CEE Aim → "Run digest (cached)" → digest renders in ~15 s. **Point at** stage-timing strip + source-stats (Phase 6H UI).

**Name these aloud while it runs:**
- `ingest` → 10 RSS + SEC + Congress (GovTrack) behind a `@register(...)` registry. Adding Mexico is a one-line config change, not a rewrite.
- `store` → Pinecone serverless, chunks tagged `region` + `source_type` at ingest. **These are filter dimensions, not prompt content.**
- `retrieve` → hybrid: `{"region": {"$in": [*aim.regions, "Global"]}}` as a Pinecone filter *before* ANN. Strongest RAG signal in the build.
- `rerank` → `gpt-4o-mini` scores 0–10 per chunk; MMR (λ=0.7) picks final 10. 15× cheaper than gpt-4o passthrough.
- `generate` → LLM picks 2–5 section titles per run — matches how Aim's live app renders digests.

---

## 2 · Why these choices — top 3 (1.5 min)

> *"Three decisions I'd defend first."*

1. **Structured Aim fields are filter dimensions, not prompt text.** Hybrid retrieval ∩ ANN. The rerank can't overrule what was never retrieved.
2. **Walking skeleton first.** All 8 verbs ran end-to-end before anything got polished — `scripts/phase0_skeleton.py` is kept as a frozen exhibit. Funnel metrics + `data/compare/*.json` snapshots prove each phase helped.
3. **Three-tier dedup, cheap-to-expensive, only the two whose plumbing is free are live.** Tier 1 URL md5 (77/78 skips on incremental re-run); Tier 3 embedding cosine ≥0.93 (free because we already have the embedding). Tier 2 MinHash is talked-about — the add above 100k docs/day.

---

## 3 · GCP services tour (1.5 min)

> *"The HoE asked for this explicitly — here's what's actually live on GCP."*

**Show** GCP console tabs (pre-open before demo):

| Service | What to point at |
|---|---|
| **Cloud Run** `aim` · europe-west3 | min=0, one container serves API + static frontend. `gcloud run deploy --source .` redeploys. |
| **Firestore** (default) | aims + digests collections. Behind `pipeline/storage.py` `USE_FIRESTORE=1`. Local JSON stays as dev substrate. |
| **BigQuery** `aim_pipeline.raw_articles` | run `SELECT source_type, COUNT(*) FROM raw_articles GROUP BY 1` live — per-source coverage. Analytical read-path. |
| **GCS** `aim-challenge-raw-494220` | `raw/{date}/{job_id}.json` — bronze layer, replay if extraction logic changes. |
| **Secret Manager** | `openai-api-key`, `pinecone-api-key` mounted as env vars by Cloud Run. Never baked into image. |

> *"Six-role least-privilege SA: Firestore User + BQ Data Editor + BQ Job User + Storage Object Admin + Secret Accessor. Cloud Run uses ADC, not a key file."*

---

## 4 · The eval harness thread (2 min) — *strongest moment*

> *"This is the story I'd lead with if I had 30 seconds."*

Point at `data/evals/SUMMARY.md`:

1. **Built an eval harness, not just a pipeline** — `scripts/eval_digest.py` + 20-row `evals/golden.jsonl` + LLM-as-judge (relevance/specificity/non-dup, 1–5).
2. **Eval caught a corpus bug** — SaaS Aim scored 0.00 recall because Congress was stubbed. Promoted `CongressConnector` stub → live via GovTrack. Relevance 2.50 → 3.00.
3. **The fix exposed a ranking bug** — recall stayed 0.00. Wrote `scripts/diagnose_saas_ranking.py`. Root cause: one hot article held 126 chunks in the top-30 pool — Tier 3 dedup filters `article_id $ne`, so same-URL re-upserts accumulated.
4. **20-line fix: `collapse_chunks_by_article`** — group retrieved chunks by `article_id`, keep the best-scored one per article, take top 40 unique articles into rerank. **Recall 0.00 → 0.17, relevance 3.00 → 4.33.**
5. **Diagnostic discipline:** three hypotheses were wrong. Dropping down a layer saved an hour of tweaking the wrong stage.

> *"That's the whole day in one thread: measurement drove the fixes, and the infra I shipped for Phase 5 (BQ + GCS) turns out to solve the next eval problem too — snapshot-pinned golden labels."*

---

## 5 · What's next with a week (1 min)

Name three, with files and line counts:

- **SEC + Congress full-text ingestion** — `SECConnector.fetch_text` (~60 LOC), `CongressConnector.fetch_text` (~40 LOC). Corpus profiling showed SEC feed median is **253 chars** — no retrieval tweak can rank what was never ingested.
- **Cross-encoder reranker** — swap gpt-4o-mini for Pinecone Inference `cohere-rerank-3.5`, ~30 LOC in `retrieval.py::rerank_chunks`. Purpose-built, no JSON-shape failure mode, ~85% cost reduction.
- **Retrieval-time clustering + `source_count` signal** — "N outlets covered this story" is the strongest editorial-importance signal in news. Move the semantic near-dup check from upsert → retrieval so the near-duplicates survive as a *popularity count* instead of getting dropped, then boost rerank by `+min(2.0, 0.5*(source_count-1))`. ~90 min across `vector_store.py` + `retrieval.py` + `report.py`.

---

## 6 · Biggest risks (1 min)

- **Cloud Run → Pinecone latency.** Pinecone index is AWS `us-east-1`, Cloud Run is `europe-west3` — every Tier-3 query is a 6–10 s cross-cloud roundtrip. `force` ingest takes ~40 min on-cluster vs ~90 s local. Demo the live URL on `cached` mode; `force` on the laptop. Fix is **3 layers, only the first is Pinecone:** colocate index to GCP `europe-west4` (500× win, 30-min reindex, zero code change) → batch Tier-3 check (O(chunks)→O(1)) → move ingest to Cloud Run Jobs.
- **Silent-empty feeds, not exceptions, dominate failures.** 3/10 seed feeds were dead on arrival; mitigation is re-raise `bozo_exception` + pre-flight smoke test.
- **gpt-4o-mini rerank degrades silently above ~80 chunks.** 6H added regex-recovery + `max_tokens` cap so failures WARN-log instead of silent-fallback to vector order. Real fix is the cross-encoder named in §5.
- **SaaS ceiling is ingest-bound, not retrieval-bound** — corpus profiling proved it. Honest framing.

---

## 7 · Close (30 s)

> *"One-day scope calls: text-only sources, ~10 live feeds + stubs for the rest, local-JSON → Firestore swap, BackgroundTasks not Pub/Sub — all called out in `docs/DEMO_NOTES.md § 4`. The pipeline's extendability is the `@register` registry; the measurability is `data/compare/*.json` + the eval harness. Happy to go deeper on any verb."*

---

## Tabs/windows to pre-open

1. Live Cloud Run URL (both Aim cards visible)
2. GCP console: Cloud Run · Firestore · BigQuery · GCS (4 tabs)
3. Terminal: `uvicorn` running locally as fallback
4. Editor on `docs/DEMO_NOTES.md` + `data/evals/SUMMARY.md`
5. Repo on GitHub (share link at the end)

## Timing budget

| Beat | Target | Hard cap |
|---|---|---|
| 0 Open | 0:30 | 0:45 |
| 1 How it works | 2:00 | 2:30 |
| 2 Why | 1:30 | 2:00 |
| 3 GCP tour | 1:30 | 2:00 |
| 4 Eval thread | 2:00 | 2:30 |
| 5 What's next | 1:00 | 1:15 |
| 6 Risks | 1:00 | 1:15 |
| 7 Close | 0:30 | 0:45 |
| **Total** | **10:00** | **13:00** |

If running long, cut §5 first (already in the repo), then §6 bullets 2–4 (keep Pinecone-latency, it's the probe they'll ask anyway).
