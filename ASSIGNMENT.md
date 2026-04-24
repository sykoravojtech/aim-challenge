## Data Pipeline Design & Prototype

---

### Context
**Aim** briefs users on the exact information they care about before anyone else knows it exists. Examples of user requests include:
* “Brief me on US Congress filings, debates, hearings, and early legislative movements that might affect my business.”
* “Every signal on social media, podcasts, news, videos, transcripts, blogs, SEC filings, and niche websites relevant to my markets.”

Users typically have a very clear idea of where to look and what to digest. The pipeline must continuously ingest heterogeneous data, normalize it, extract signal, and output approximately **10 highly relevant insights per day** from a daily firehose of **10,000 documents per user**. This system must be scalable, robust, and deliver insights ideally instantly.

---

### Problem
You are designing a **multi-stage data pipeline** that:
* **Ingests thousands of documents/day** from heterogeneous sources including US Congress Filings, social media (X, LinkedIn, Reddit), podcasts, videos (YouTube/webinars), news, blogs, and long-tail company websites.
* **Normalizes, enriches, deduplicates, and stores** the raw data.
* **Produces a ranked list of daily insights** relevant to specific user tasks (e.g., SaaS AI legislative changes or EV supply-chain risks).
* **Scales effectively** as Aim adds more users with personalized briefings.
* **Is extendable** to support new source types (e.g., Mexican state institutions or SEC filings) or exploration strategies.

#### Key Pipeline Requirements:
* Handling unpredictable source reliability.
* Deduplication (e.g., two distinct articles covering the same event).
* Multi-stage relevance filtering using LLMs or standard Recommendation Systems.
* Fault-tolerance and reprocessing capabilities.

---

### Your Task
Design the system and build a **minimal functional prototype** that demonstrates the critical ideas.

#### Part 1: System Architecture
Focus on depth over breadth. You are encouraged to use AI tools (ChatGPT, Claude Code, etc.) to assist in the design.

#### Part 2: Prototype
Implement a small demo illustrating the **core logic** of the pipeline. This should showcase your architectural instincts and technical judgment rather than being a full product.

---

### Suggestions
* **Multi-stage Ranking:** Avoid "one giant LLM call".
* **Modularity:** Every stage should be replaceable independently.
* **Scalability:** Identify and plan for bottlenecks.
* **Clarity:** Ensure explicit, inspectable intermediate outputs.

---

### Expected Output
By the end of the day, provide:
1.  **Prototype repo**.
2.  **10-minute show & tell** walking through:
    * How the system works.
    * The reasoning behind your design choices.
    * What you would build next if given a week.
    * What the biggest risks are.