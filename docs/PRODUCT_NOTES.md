# Product notes — what Aim actually ships

What we learned by reading screenshots of the live app (`tmp/aim aims.pdf`, `tmp/aim digest.pdf`). These are the product nouns and data shapes we should mirror in our build.

Treat this file as the source of truth for terminology. If something here contradicts older drafts, this doc wins.

---

## Core concepts

```
Account  ──1:N──▶  Aim  ──1:N──▶  Digest
                    │
                    ├─ title
                    ├─ summary (bullets of intent)
                    ├─ monitored_entities (who)
                    ├─ regions           (where)
                    └─ update_types      (what type of news)
```

One user has **many aims**. Each aim generates **many digests** over time (weekly cadence in production; one digest per triggered run in our build).

---

## Aim — the monitoring configuration

An **Aim** is not a freeform thesis sentence. It is a structured object. From the live UI:

| Field | Type | Example |
|---|---|---|
| `title` | string | "Founders Media Monitoring Czechia Slovakia" |
| `summary` | list[str] | 3 bullets of intent, e.g. "Track every mention of selected founders in Czech media" |
| `monitored_entities` | list[str] | `["Founders", "Companies"]` — or specific names/tags |
| `regions` | list[str] | `["Czechia", "Slovakia"]` — or `["Global"]` |
| `update_types` | list[str] | `["News", "Announcements", "Reports", "Media Mentions"]` |

**Why structured matters for us:** the three tag fields (`monitored_entities`, `regions`, `update_types`) aren't prompt decoration — they're **hybrid retrieval filters**. They map to Pinecone metadata so retrieval becomes "semantic match, filtered by structured dimensions," which is a meaningfully better RAG pattern than packing them into the prompt.

### Two aims shown in the live app

```
Aim #1 — Founders Media Monitoring Czechia Slovakia
  summary:             ["Track mentions in Czech media (Forbes.cz, HN.cz, Cc.cz)",
                        "Monitor news, announcements, reports, media coverage",
                        "Focus on Czechia and Slovakia public visibility"]
  monitored_entities:  ["Founders", "Companies"]
  regions:             ["Czechia", "Slovakia"]
  update_types:        ["News", "Announcements", "Reports", "Media Mentions"]

Aim #2 — xAI News Monitoring
  summary:             ["Monitor official announcements and press releases from xAI",
                        "Track product updates and feature launches",
                        "Follow statements and interviews from leadership",
                        "Observe market reactions and coverage"]
  monitored_entities:  ["xAI"]
  regions:             ["Global"]
  update_types:        ["News", "Announcements", "Reports"]
```

---

## Digest — the output artifact

A **Digest** is not a flat `summary + bullets`. It has a title, a date range, a one-line headline ("ticker"), and an ordered list of **sections** whose titles are *chosen by the LLM per run* to fit the content.

From the live app ("Start Aiming – Miton AI Newsletter #31"):

```
Digest {
  title:       "Start Aiming – Miton AI Newsletter #31"
  date_range:  "Mar 23–30, 2026"
  headline:    "Anthropic wins court reprieve and ships autonomous
                features, Perplexity's 'Computer' hits Pro, Google rolls
                out Gemini Live & memory import, Apple opens Siri…"
  sections: [
    {
      title: "Opinion leaders mentioned",
      items: [
        { type: "quote",
          quote: "AI is probably the most likely way to destroy everything.",
          attribution: "Karen Hao",
          source_count: 1 }
      ]
    },
    {
      title: "Investments",
      items: [
        { type: "investment",
          entity: "Credo Ventures",
          amount: "$88 million (Fund V)",
          body:   "Backing pre-seed founders across CEE with focus on AI, LLMs, and agentic software; ~7–8 investments per year",
          source_count: 1 }
      ]
    },
    {
      title: "Product Updates",
      items: [
        { type: "product_update",
          title: "Claude Code & Cowork: computer use on macOS (research preview)",
          body:  "Anthropic enabled Claude to operate your Mac…",
          source_count: 1 },
        { type: "product_update",
          title: "Gemini adds memory import and chat-history ZIP migration",
          body:  "…",
          source_count: 1 }
      ]
    }
  ]
}
```

### Three things this tells us

1. **Sections are dynamic.** "Opinion leaders mentioned / Investments / Product Updates" appeared for this digest. A different aim (e.g. "Czech founders") would produce different sections (e.g. "Hires & exits / Fundraises / Regulatory"). We should not hard-code section names in the schema — ask the LLM to choose them.
2. **Items are heterogeneously typed.** A `quote` item has attribution and a quote string; an `investment` item has entity + amount; a `product_update` has title + body. Modelling this as a discriminated union is Tier B (nice to have); Tier A is a flexible `item_type: str` plus optional structured fields.
3. **Each item cites N sources.** The UI shows a "1 source" badge. In our build, each digest item should carry `source_urls: list[str]` and `source_count: int`, populated from the retrieved chunks used to generate it.

---

## API implications

Our endpoints evolve from "profile + briefing" to match the real hierarchy:

| Old endpoint (draft) | New endpoint | Purpose |
|---|---|---|
| `POST /profile` | `POST /aim` | Create an aim (body carries `user_id`, aim fields) |
| — | `GET /aim/{aim_id}` | Fetch one aim |
| — | `GET /aims?user_id=…` | List aims for a user (useful for debugging / demo) |
| `POST /briefing/generate` | `POST /aim/{aim_id}/digest` | Trigger a digest for that aim |
| `GET /briefing/{id}` | `GET /digest/{digest_id}` | Poll status / fetch digest |
| `GET /health` | `GET /health` | unchanged |

The pipeline `run_pipeline` signature becomes `run_pipeline(aim_id, job_id)` — it loads the Aim, constructs the retrieval query from its fields, and emits a Digest.

---

## Pipeline implications (what actually changes)

The 8-verb pipeline (ingest → extract → chunk → embed → store → retrieve → rerank → generate) is unchanged. Only the ends change:

| Stage | Change |
|---|---|
| **Ingestion** | Each source dict carries `region`, `source_type` (already planned) so chunks inherit them. No change to the scraping logic. |
| **Chunking** | Unchanged. Title still prepended. |
| **Embedding** | Unchanged. |
| **Vector upsert** | Metadata now includes `region` and `source_type` so they can be filtered on retrieval. |
| **Retrieval** | The **query** is constructed from the aim: `title + " — " + " ".join(summary) + " — " + ", ".join(monitored_entities)`. Optionally pass `filter={"region": {"$in": aim.regions}}` to Pinecone (Tier B). |
| **Re-rank** | LLM prompt includes the structured aim (not a freeform thesis), sharpening relevance scoring. |
| **Report generation** | LLM is told to produce `Digest` with dynamic `sections`, section titles chosen to fit the retrieved content. JSON mode required. |

---

## Terminology reference (use these exactly)

| Say | Don't say |
|---|---|
| **Account** or **User** | (either is fine) |
| **Aim** | thesis, profile, query |
| **Digest** | report, briefing, summary |
| **Section** (within a digest) | category, bucket, theme |
| **Item** (within a section) | entry, card, deep_dive (legacy term) |
| `aim_id`, `digest_id`, `job_id` | `user_id` stays for accounts |

Old docs may still use "thesis" / "report" in places — those are gradually being migrated. Prefer Aim/Digest in any new file.

---

## What we still intentionally cut from the real product

Even with this richer schema, these stay out of scope for the one-day build:

- Weekly cron / Cloud Scheduler regeneration
- Bookmark and delete actions on items (UI state)
- Source logos/icons
- Per-user newsletter numbering ("Newsletter #31") — we just use `generated_at`
- Opinion-leader headshots
- Cross-aim deduping of items
- HTML/web-app rendering (Phase 5 stretch only)
