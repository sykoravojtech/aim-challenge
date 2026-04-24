"""Ingestion — connectors, registry, per-source fan-out with try/except."""
from __future__ import annotations

import calendar
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

import feedparser
import trafilatura

log = logging.getLogger(__name__)

MAX_ITEMS_PER_SOURCE = 8
MIN_EXTRACT_CHARS = 200

# SEC and some Czech sites 403 a generic python-requests UA.
HTTP_UA = "aim-challenge/0.1 (vojtech.sykora@miton.cz)"

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

# Seed Aims written to data/aims/ on first boot (Phase 1). Both demo Aims from
# ROADMAP § "The two demo Aims" — contrast is the point of having two.
SEED_AIMS: list[dict[str, Any]] = [
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


@dataclass
class RawDoc:
    article_id: str  # md5(url) — Tier 1 dedup handle for Phase 2
    source_url: str
    title: str
    text: str
    source_type: str
    region: str
    published_at: str | None = None      # RSS-reported human-readable date
    published_ts: int = 0                # epoch seconds — Phase 4 recency handle
    source_feed: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "article_id": self.article_id,
            "source_url": self.source_url,
            "title": self.title,
            "text": self.text,
            "source_type": self.source_type,
            "region": self.region,
            "published_at": self.published_at,
            "published_ts": self.published_ts,
            "source_feed": self.source_feed,
        }


def _published_ts(entry: Any) -> int:
    """Epoch seconds from an RSS entry's pubDate. Falls back to ingest-time so
    every chunk has a number the recency filter can compare against."""
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
    """One instance per feed URL. feedparser's silent-empty failure mode (see
    LESSONS § feedparser silent failures) is raised explicitly so tenacity
    (Phase 2) can retry."""

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
            summary = re.sub(r"<[^>]+>", " ", ref.get("summary") or "").strip()
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


# Stubs — register so the extensibility pattern is visible; implementing any of
# these is a pluggable config change, not a pipeline rewrite (see DECISIONS D6).
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


def ingest_all_sources(seen_ids: set[str] | None = None) -> tuple[list[RawDoc], dict[str, dict[str, int]]]:
    """Fan out across SOURCES with per-source try/except. `seen_ids` is the
    Tier 1 dedup handle — matched article_ids are skipped at ref time (cheap
    check before any trafilatura fetch). Phase 1 passes None (force) or the
    union of prior raw-article ids (incremental). Phase 2 makes dedup loud."""
    docs: list[RawDoc] = []
    stats: dict[str, dict[str, int]] = {}
    for src in SOURCES:
        stat = {"listed": 0, "skipped_seen": 0, "extracted": 0, "used": 0}
        try:
            conn = RSSConnector(src["url"], src["region"], src["source_type"])
            refs = list(conn.list_new_items())
            stat["listed"] = len(refs)
            for ref in refs:
                if seen_ids:
                    article_id = hashlib.md5(ref["link"].encode()).hexdigest()
                    if article_id in seen_ids:
                        stat["skipped_seen"] += 1
                        continue
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
