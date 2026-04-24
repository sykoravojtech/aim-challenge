## Data Pipeline Design & Prototype

---

### Context
**Aim** briefs users on the exact information they care about. Before anyone else knows it exists.

A user can ask: “Brief me on US Congress filings, debates, hearings, and early legislative movements that might affect my business.” Another user might ask: “Every signal on social media, podcasts, news, videos, transcripts, blogs, SEC filings, and niche websites relevant to my markets.”

Importantly, users already have a very clear idea of where to look and what to digest.

The pipeline must continuously ingest heterogeneous data, normalize it, extract signal, and output only a handful of highly relevant insights (**~10/day/user**) from a daily firehose of **~10k documents/user**.

This must scale. It must be robust. And the insights must be delivered **the same day they appear, ideally instantly**.

---

### Problem
You are designing a **multi-stage data pipeline** that:

1. **Ingests thousands of documents/day** from heterogeneous sources, for example:
    * US Congress Filings
    * Social media (X, LinkedIn, Reddit, etc.)
    * Podcasts
    * Videos (YouTube or a random webinar on a particular website)
    * News and Blogs
    * long-tail sources: company websites and content
    * Anything else that matters for the user’s business context
2. **Normalizes, enriches, deduplicates, and stores** the raw data.
3. **Produces a ranked list of daily insights** relevant to a specific user task (e.g., “legislative changes that could impact SaaS AI companies”, “supply-chain regulation risk for EV startups”, etc.).
4. **Is architected so Aim could scale users**, each with their own personalized briefing.
5. **Is extendable** to support new source types or source exploration strategies. For example, we might learn that a client needs data from Mexican state institutions or SEC filings. How do we facilitate this?

A good pipeline handles:
* unpredictable source reliability
* deduplication (e.g. two distinct articles covering the same event)
* multi-stage relevance filtering using LLMs or standard Recommendation Systems
* fault-tolerance and reprocessing

---

### Your Task
Design the system and build a **minimal functional prototype** that demonstrates the critical ideas.

#### Part 1: System Architecture
Depth matters more than breadth. Feel free to use and misuse ChatGPT, Claude Code or any AI of your likings.

#### Part 2 — Prototype (small, focused)
Implement a small demo that illustrates the **core logic** of the pipeline. This is a prototype, not a full product: the goal is to show your architectural instincts and your technical judgment. Again, feel free to use ChatGPT, Claude or AI of your likings as much as possible.

---

### Suggestions
* Think in **multi-stage ranking** instead of “one giant LLM call”.
* Think **modularly**: every stage should be replaceable independently.
* Think **scalability**: what are the bottlenecks and how to overcome them.
* We value **clarity**: explicit, inspectable intermediate outputs.

---

### Expected Output
By end of day:
1.  **Prototype repo**.
2.  **10-minute show & tell** walking through:
    * how the system works
    * why you made the design choices you did
    * what you would build next if given a week
    * what the biggest risks are