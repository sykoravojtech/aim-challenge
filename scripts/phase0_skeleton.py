"""Phase 0 walking skeleton — frozen demo artifact, NOT the runtime path.

Status: superseded by ``main.py`` + ``pipeline/*.py`` in Phase 1. Kept verbatim
as an interview exhibit for the "walking skeleton first" principle (see
CLAUDE.md). Editing this file does NOT affect the live API — SOURCES and AIMS
are intentionally duplicated from ``pipeline/ingestion.py`` / seeded aims so
this file reads top-to-bottom as a single ~560-line end-to-end pipeline.

Flow: ingest (RSS) → extract (trafilatura, fallback to RSS summary) → chunk
(LangChain Recursive, 800/100, title prepended) → embed (text-embedding-3-small,
batched) → upsert (Pinecone, metadata carries region + source_type) → retrieve
(hybrid filter: region $in aim.regions + Global) → generate (gpt-4o-mini, JSON
mode, dynamic section titles) → print Digest JSON.

No FastAPI, no dedup, no rerank. Every stage logs a funnel line.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

import feedparser
import trafilatura
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from openai import OpenAI
from pinecone import Pinecone

# Make `from pipeline import …` work when running this script directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline._util import safe_llm_json, strip_markdown_fences  # noqa: E402,F401
from pipeline.ingestion import mirror_raw_to_bq, mirror_raw_to_gcs  # noqa: E402

load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("phase0")

# ---------------------------------------------------------------------------
# Config — Aims and sources (both hardcoded for Phase 0; Phase 1 persists them)
# ---------------------------------------------------------------------------

AIMS: list[dict[str, Any]] = [
    {
        "aim_id": "saas-ai-legislation",
        "user_id": "demo",
        "title": "SaaS-AI US Legislative Watch",
        "summary": [
            "Track US bills, hearings, and regulatory filings that could affect SaaS and AI companies",
            "Monitor SEC enforcement activity against AI product claims",
            "Follow Congressional committee commentary on AI model regulation",
        ],
        "monitored_entities": ["SaaS companies", "AI companies", "SEC", "US Congress"],
        "regions": ["US", "Global"],
        "update_types": ["Legislation", "Regulatory", "Enforcement", "Hearings"],
    },
    {
        "aim_id": "cee-founder-media",
        "user_id": "demo",
        "title": "Czech & CEE Founder Media Monitoring",
        "summary": [
            "Track every mention of Czech and CEE founders in regional media",
            "Monitor fundraising and product announcements from CEE AI startups",
            "Follow VC fund commentary on the CEE early-stage market",
        ],
        "monitored_entities": ["Founders", "Companies", "VC funds"],
        "regions": ["Czechia", "Slovakia", "CEE"],
        "update_types": ["News", "Announcements", "Reports", "Media Mentions"],
    },
]

# 10 text sources, region-weighted so both demo Aims have a pool.
# Replacements vs ROADMAP list (smoke-tested 2026-04-24): federalregister.gov
# SEC search.rss → sec.gov/news/pressreleases.rss; hn.cz/feed (404) →
# lupa.cz/rss/clanky/; euvc.com/feed (HTML-not-XML) → therecursive.com/feed/.
SOURCES: list[dict[str, str]] = [
    {"url": "https://techcrunch.com/feed/", "source_type": "news", "region": "US"},
    {"url": "https://news.ycombinator.com/rss", "source_type": "news", "region": "Global"},
    {"url": "https://arxiv.org/rss/cs.AI", "source_type": "research", "region": "Global"},
    {"url": "https://www.theverge.com/rss/index.xml", "source_type": "news", "region": "US"},
    {"url": "https://venturebeat.com/feed/", "source_type": "news", "region": "US"},
    {"url": "https://www.sec.gov/news/pressreleases.rss", "source_type": "regulatory", "region": "US"},
    {"url": "https://cc.cz/feed/", "source_type": "news", "region": "Czechia"},
    {"url": "https://www.forbes.cz/feed/", "source_type": "news", "region": "Czechia"},
    {"url": "https://www.lupa.cz/rss/clanky/", "source_type": "news", "region": "Czechia"},
    {"url": "https://therecursive.com/feed/", "source_type": "news", "region": "CEE"},
]

# Per-run caps — keep Phase 0 fast.
MAX_ITEMS_PER_SOURCE = 8
MIN_EXTRACT_CHARS = 200
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
EMBED_BATCH = 100
RETRIEVE_TOP_K = 20
EMBED_MODEL = "text-embedding-3-small"
LLM_MODEL = "gpt-4o-mini"

# SEC and some Czech sites 403 generic python-requests UAs.
HTTP_UA = "aim-challenge/0.1 (vojtech.sykora@miton.cz)"

PINECONE_INDEX = os.environ.get("PINECONE_INDEX", "aim-chunks")

# ---------------------------------------------------------------------------
# Connector registry — one class registered for Phase 0, stubs registered in
# name only so the extensibility story is visible from day one.
# ---------------------------------------------------------------------------


@dataclass
class RawDoc:
    article_id: str  # md5(url) — Tier 1 dedup handle for Phase 2
    source_url: str
    title: str
    text: str
    source_type: str
    region: str
    published_at: str | None = None      # RSS-reported human-readable date (for data/raw/ debug)
    published_ts: int = 0                # epoch seconds — filter-side handle for Phase 4 recency
    source_feed: str = ""


def _published_ts(entry: Any) -> int:
    """Epoch seconds from an RSS entry's pubDate. Feedparser normalises
    RFC822/Atom dates into time.struct_time (UTC); calendar.timegm converts
    that to epoch. Falls back to ingest-time so every chunk has a number the
    recency filter can compare against (safer than omitting — an undated chunk
    should age from when we saw it, not sort to the top forever)."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return int(calendar.timegm(st))
            except (TypeError, ValueError, OverflowError):
                continue
    return int(time.time())


class BaseConnector(Protocol):
    source_id: str
    region: str
    source_type: str

    def list_new_items(self) -> Iterable[dict[str, Any]]: ...
    def fetch(self, ref: dict[str, Any]) -> RawDoc | None: ...


REGISTRY: dict[str, type] = {}


def register(name: str):
    def deco(cls):
        REGISTRY[name] = cls
        return cls
    return deco


@register("rss")
class RSSConnector:
    """One connector instance per feed URL. feedparser's silent-empty failure
    mode (see LESSONS § feedparser silent failures) is handled explicitly:
    empty entries + bozo=1 raises so tenacity (Phase 2) can retry."""

    def __init__(self, url: str, region: str, source_type: str):
        self.url = url
        self.region = region
        self.source_type = source_type
        self.source_id = url

    def list_new_items(self) -> Iterable[dict[str, Any]]:
        feed = feedparser.parse(self.url, request_headers={"User-Agent": HTTP_UA})
        if not feed.entries and feed.get("bozo", 0):
            exc = feed.get("bozo_exception")
            raise RuntimeError(f"feed {self.url} bozo-empty: {exc}")
        for entry in feed.entries[:MAX_ITEMS_PER_SOURCE]:
            link = entry.get("link")
            if not link:
                continue
            yield {
                "link": link,
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "") or entry.get("description", ""),
                "published": entry.get("published") or entry.get("updated"),
                "published_ts": _published_ts(entry),
            }

    def fetch(self, ref: dict[str, Any]) -> RawDoc | None:
        url = ref["link"]
        article_id = hashlib.md5(url.encode()).hexdigest()
        try:
            html = trafilatura.fetch_url(url)
        except Exception as e:
            log.warning("fetch_url raised on %s: %s", url, e)
            html = None
        text = ""
        if html:
            extracted = trafilatura.extract(html, include_comments=False, favor_precision=True)
            if extracted:
                text = extracted.strip()
        if len(text) < MIN_EXTRACT_CHARS:
            # Fallback to RSS summary. Strip HTML tags crudely — we just need words.
            import re as _re
            summary = _re.sub(r"<[^>]+>", " ", ref.get("summary") or "").strip()
            if len(summary) >= MIN_EXTRACT_CHARS:
                text = summary
        if len(text) < MIN_EXTRACT_CHARS:
            return None
        return RawDoc(
            article_id=article_id,
            source_url=url,
            title=ref.get("title") or url,
            text=text,
            source_type=self.source_type,
            region=self.region,
            published_at=ref.get("published"),
            published_ts=ref.get("published_ts") or int(time.time()),
            source_feed=self.url,
        )


# Stubs — register so the extensibility pattern is visible without the rabbit hole.
for _name in ("sec", "congress", "reddit", "x", "linkedin", "youtube", "podcast"):
    def _make_stub(n):
        class _Stub:
            source_id = n
            region = ""
            source_type = n
            def list_new_items(self):
                raise NotImplementedError(f"{n} connector stubbed — see DECISIONS D6")
            def fetch(self, ref):
                raise NotImplementedError(f"{n} connector stubbed")
        _Stub.__name__ = f"{n.capitalize()}Connector"
        return _Stub
    register(_name)(_make_stub(_name))


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def ingest_all_sources() -> tuple[list[RawDoc], dict[str, dict[str, int]]]:
    """Fan out across SOURCES with per-source try/except. Returns raw docs plus
    per-source stats so the funnel line can surface dead feeds."""
    docs: list[RawDoc] = []
    stats: dict[str, dict[str, int]] = {}
    for src in SOURCES:
        stat = {"listed": 0, "extracted": 0, "used": 0}
        try:
            conn = RSSConnector(src["url"], src["region"], src["source_type"])
            refs = list(conn.list_new_items())
            stat["listed"] = len(refs)
            for ref in refs:
                doc = conn.fetch(ref)
                if doc is None:
                    continue
                stat["extracted"] += 1
                docs.append(doc)
                stat["used"] += 1
        except Exception as e:
            log.warning("source %s failed: %s", src["url"], e)
        stats[src["url"]] = stat
    return docs, stats


def chunk_articles(docs: list[RawDoc]) -> list[dict[str, Any]]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )
    chunks: list[dict[str, Any]] = []
    for doc in docs:
        # Prepend title so each chunk is self-identifying at rerank time.
        body = f"{doc.title}\n\n{doc.text}"
        parts = splitter.split_text(body)
        for i, part in enumerate(parts):
            chunks.append(
                {
                    "chunk_id": uuid.uuid4().hex,
                    "article_id": doc.article_id,
                    "source_url": doc.source_url,      # article URL — the real page we extracted
                    "source_feed": doc.source_feed,    # feed URL we polled — provenance for debugging + per-feed rerank
                    "title": doc.title,
                    "text": part,
                    "source_type": doc.source_type,
                    "region": doc.region,
                    "published_ts": doc.published_ts,  # epoch seconds — Phase 4 recency filter handle
                    "chunk_index": i,
                    "total_chunks": len(parts),
                }
            )
    return chunks


def embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i : i + EMBED_BATCH]
        resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
        out.extend(d.embedding for d in resp.data)
    return out


def upsert_chunks(index, chunks: list[dict[str, Any]], embeddings: list[list[float]]) -> int:
    vectors = []
    for chunk, emb in zip(chunks, embeddings):
        # Pinecone metadata caps: keep text short, numeric fields optional.
        vectors.append(
            {
                "id": chunk["chunk_id"],
                "values": emb,
                "metadata": {
                    "article_id": chunk["article_id"],
                    "source_url": chunk["source_url"],
                    "source_feed": chunk.get("source_feed", ""),
                    "title": chunk["title"],
                    "text": chunk["text"][:1000],
                    "source_type": chunk["source_type"],
                    "region": chunk["region"],
                    "published_ts": int(chunk.get("published_ts") or 0),
                    "chunk_index": chunk["chunk_index"],
                },
            }
        )
    upserted = 0
    for i in range(0, len(vectors), 100):
        batch = vectors[i : i + 100]
        index.upsert(vectors=batch)
        upserted += len(batch)
    return upserted


def build_query_text(aim: dict[str, Any]) -> str:
    return (
        f"{aim['title']} — "
        f"{' '.join(aim['summary'])} — "
        f"monitoring: {', '.join(aim['monitored_entities'])}"
    )


def build_query_filter(aim: dict[str, Any]) -> dict[str, Any]:
    # Hybrid retrieval core: regions become Pinecone filter dimensions, not
    # prompt content. "Global" is always OR'd so Global-tagged pieces still
    # serve regional Aims (see ARCHITECTURE § Hybrid retrieval).
    return {"region": {"$in": list(aim["regions"]) + ["Global"]}}


def retrieve_relevant_chunks(
    client: OpenAI, index, aim: dict[str, Any], top_k: int = RETRIEVE_TOP_K
) -> list[dict[str, Any]]:
    query_text = build_query_text(aim)
    [query_emb] = embed_texts(client, [query_text])
    res = index.query(
        vector=query_emb,
        top_k=top_k,
        filter=build_query_filter(aim),
        include_metadata=True,
    )
    out = []
    for match in res.get("matches", []):
        md = match.get("metadata", {}) or {}
        out.append(
            {
                "chunk_id": match["id"],
                "score": match["score"],
                "article_id": md.get("article_id"),
                "source_url": md.get("source_url"),
                "source_feed": md.get("source_feed", ""),
                "title": md.get("title"),
                "text": md.get("text"),
                "source_type": md.get("source_type"),
                "region": md.get("region"),
                "published_ts": md.get("published_ts"),
            }
        )
    return out


def generate_digest(client: OpenAI, aim: dict[str, Any], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """One gpt-4o-mini JSON call. Sections are LLM-chosen per run."""
    # Compact chunk payload — we can afford ~20 chunks of ~800 chars each, model
    # gets the structured Aim + excerpts + an instruction to choose 2–5 section
    # titles to fit the content.
    payload_chunks = [
        {
            "i": i,
            "title": c["title"],
            "source_url": c["source_url"],
            "region": c["region"],
            "source_type": c["source_type"],
            "excerpt": (c["text"] or "")[:600],
        }
        for i, c in enumerate(chunks)
    ]

    system = (
        "You are a senior market intelligence analyst producing a personalised "
        "digest for a monitoring configuration called an Aim. Respond with a "
        "JSON object matching the schema in the user message. Section titles "
        "are chosen by you, 2–5 of them, to fit the retrieved content (do not "
        "reuse a fixed taxonomy). Every item must cite at least one source_url "
        "that appears in the provided chunks."
    )

    user = {
        "aim": aim,
        "date_range": _today_range(),
        "instructions": (
            "From the provided chunks, build a Digest JSON for this Aim. "
            "Pick 2–5 section titles that fit what you found (e.g. 'Fundraises', "
            "'Product Updates', 'Regulatory'). Each section has items; each item "
            "has a title, body (1–3 sentences), source_urls (list of real URLs "
            "from the chunks), source_count, item_type (one of 'news', 'quote', "
            "'investment', 'product_update', 'regulatory', 'announcement'), and "
            "relevance_score (integer 1-10). Return ONLY this JSON schema: "
            '{"headline": str, "date_range": str, "sections": ['
            '{"title": str, "items": ['
            '{"title": str, "body": str, "source_urls": [str], '
            '"source_count": int, "item_type": str, "relevance_score": int}'
            "]}]}. If nothing relevant found, return an empty sections list."
        ),
        "chunks": payload_chunks,
    }

    resp = client.chat.completions.create(
        model=LLM_MODEL,
        response_format={"type": "json_object"},
        temperature=0.3,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user)},
        ],
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        digest = json.loads(strip_markdown_fences(raw))
    except json.JSONDecodeError as e:
        log.error("digest JSON parse failed: %s; raw head=%r", e, raw[:200])
        digest = {"headline": "Digest generation failed", "date_range": _today_range(), "sections": []}

    digest.setdefault("sections", [])
    digest.setdefault("headline", "")
    digest.setdefault("date_range", _today_range())
    return digest


def _today_range() -> str:
    d = datetime.now(timezone.utc).strftime("%b %d, %Y")
    return f"{d} (single-day run)"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_for_aim(aim: dict[str, Any]) -> dict[str, Any]:
    client = OpenAI()
    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    index = pc.Index(PINECONE_INDEX)

    log.info("=== Phase 0 walking skeleton — aim=%s ===", aim["aim_id"])

    log.info("[1/7] ingesting %d sources (max %d items each)...", len(SOURCES), MAX_ITEMS_PER_SOURCE)
    docs, source_stats = ingest_all_sources()
    log.info("  INGESTED=%d across %d sources", len(docs), len(SOURCES))
    for url, st in source_stats.items():
        log.info("    %s listed=%d extracted=%d used=%d", url, st["listed"], st["extracted"], st["used"])

    if not docs:
        log.error("no docs ingested — aborting")
        return {"headline": "No sources returned content", "date_range": _today_range(), "sections": []}

    # BQ + GCS mirrors (Phase 5b/5e, gated by USE_BIGQUERY / GCS_BUCKET). No-op
    # when flags unset. This script doesn't persist raw JSON locally (Phase 0
    # skeleton); the mirrors still go out so GCP smoke-tests work end-to-end.
    job_id = f"phase0-{aim['aim_id']}-{int(time.time())}"
    raw_rows = [
        {
            "article_id": d.article_id,
            "source_url": d.source_url,
            "title": d.title,
            "text": d.text,
            "source_type": d.source_type,
            "region": d.region,
            "published_at": d.published_at,
            "published_ts": d.published_ts,
            "source_feed": d.source_feed,
        }
        for d in docs
    ]
    mirror_raw_to_bq(raw_rows, job_id)
    mirror_raw_to_gcs(raw_rows, job_id)

    log.info("[2/7] chunking (size=%d overlap=%d)...", CHUNK_SIZE, CHUNK_OVERLAP)
    chunks = chunk_articles(docs)
    log.info("  CHUNKED=%d from %d docs", len(chunks), len(docs))

    log.info("[3/7] embedding %d chunks (model=%s, batch=%d)...", len(chunks), EMBED_MODEL, EMBED_BATCH)
    embeddings = embed_texts(client, [c["text"] for c in chunks])
    log.info("  EMBEDDED=%d", len(embeddings))

    log.info("[4/7] upserting to Pinecone index=%s...", PINECONE_INDEX)
    upserted = upsert_chunks(index, chunks, embeddings)
    log.info("  UPSERTED=%d", upserted)

    log.info("[5/7] retrieving (top_k=%d, filter region $in %s + Global)...", RETRIEVE_TOP_K, aim["regions"])
    retrieved = retrieve_relevant_chunks(client, index, aim, top_k=RETRIEVE_TOP_K)
    log.info("  RETRIEVED=%d (post-hybrid-filter)", len(retrieved))
    if not retrieved:
        log.warning("retrieval returned 0 chunks — check region tags / filter. regions=%s", aim["regions"])

    log.info("[6/7] generating digest (model=%s, JSON mode)...", LLM_MODEL)
    digest = generate_digest(client, aim, retrieved)
    n_sections = len(digest.get("sections", []))
    n_items = sum(len(s.get("items", [])) for s in digest.get("sections", []))
    log.info("  SECTIONS=%d ITEMS=%d", n_sections, n_items)

    # Attach funnel for inspectability (brief suggestion #4).
    digest["_funnel"] = {
        "ingested": len(docs),
        "chunked": len(chunks),
        "embedded": len(embeddings),
        "upserted": upserted,
        "retrieved": len(retrieved),
        "sections": n_sections,
        "items": n_items,
        "aim_id": aim["aim_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    digest["_source_stats"] = source_stats

    log.info("[7/7] done.")
    return digest


def main():
    # Per ROADMAP: Phase 0 runs the CEE aim (more concrete smoke test).
    aim_id = os.environ.get("AIM_ID", "cee-founder-media")
    aim = next((a for a in AIMS if a["aim_id"] == aim_id), None)
    if aim is None:
        raise SystemExit(f"no such aim {aim_id!r}; choices: {[a['aim_id'] for a in AIMS]}")
    digest = run_for_aim(aim)
    print(json.dumps(digest, indent=2, ensure_ascii=False))
    return digest


if __name__ == "__main__":
    main()
