# aim-challenge

One-day prototype of Aim's market-intelligence pipeline. Ingests heterogeneous
sources, filters via hybrid RAG (structured Pinecone metadata ∩ semantic
search), re-ranks with an LLM, and emits a per-Aim **Digest**.

Written during the Aim hiring challenge — see `docs/` once populated.

## Quickstart

```bash
uv sync
cp .env.example .env            # fill in OPENAI_API_KEY, PINECONE_API_KEY

# Phase 0 walking skeleton (frozen demo artifact, no API):
uv run python scripts/phase0_skeleton.py

# Phase 1+ FastAPI:
uv run uvicorn main:app --reload --port 4444
```

See [CLAUDE.md](CLAUDE.md) for the working rules and phase order.
