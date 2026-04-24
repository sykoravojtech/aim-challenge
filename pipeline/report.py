"""Digest generation — one gpt-4o-mini JSON call. Sections are LLM-chosen per run."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from openai import OpenAI

from models.schemas import Aim
from pipeline._util import strip_markdown_fences

log = logging.getLogger(__name__)

LLM_MODEL = "gpt-4o-mini"


def today_range() -> str:
    d = datetime.now(timezone.utc).strftime("%b %d, %Y")
    return f"{d} (single-day run)"


def generate_digest(client: OpenAI, aim: Aim, chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Returns `{headline, date_range, sections:[{title, items:[...]}]}` as a
    plain dict. Caller wraps in the full Digest model."""
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
        "aim": aim.model_dump(mode="json"),
        "date_range": today_range(),
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
        digest = {"headline": "Digest generation failed", "date_range": today_range(), "sections": []}

    digest.setdefault("sections", [])
    digest.setdefault("headline", "")
    digest.setdefault("date_range", today_range())
    return digest
