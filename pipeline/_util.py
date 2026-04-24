"""Shared helpers. Keep lean — this file must have no heavy imports."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_MARKDOWN_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class LLMShapeError(ValueError):
    """Raised when an LLM response cannot be coerced to the expected shape."""


def safe_llm_json(raw: str, expected_key: str, expected_len: int) -> list[Any]:
    """Parse a JSON-mode LLM response and extract a list at `expected_key`.

    Handles — in order — the failure modes documented in LESSONS.md:
      1. Markdown fences around the JSON object.
      2. Case-differing keys (`SCORES` vs `scores`).
      3. Value returned as a JSON-stringified list ('"[1,2,3]"').
      4. Array length = expected_len + 1 (truncate; model appended a mean/summary).
      5. Array length < expected_len (fail loud — cannot pad safely).

    Raises LLMShapeError on unrecoverable shape.
    """
    if raw is None:
        raise LLMShapeError("LLM returned None")

    cleaned = _MARKDOWN_FENCE_RE.sub("", raw).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise LLMShapeError(f"not valid JSON: {e}; first 120 chars={cleaned[:120]!r}") from e

    if not isinstance(obj, dict):
        raise LLMShapeError(f"expected JSON object, got {type(obj).__name__}")

    # Case-insensitive key lookup.
    keymap = {k.lower(): k for k in obj}
    key = keymap.get(expected_key.lower())
    if key is None:
        raise LLMShapeError(f"missing key {expected_key!r}; have {list(obj)}")

    arr = obj[key]
    if isinstance(arr, str):
        # Sometimes the model JSON-stringifies the list.
        try:
            arr = json.loads(arr)
        except json.JSONDecodeError as e:
            raise LLMShapeError(f"{expected_key} was a string that isn't JSON: {e}") from e

    if not isinstance(arr, list):
        raise LLMShapeError(f"{expected_key} is {type(arr).__name__}, want list")

    if len(arr) == expected_len + 1:
        log.warning("safe_llm_json: %s over by 1 (len=%d, want=%d), truncating", expected_key, len(arr), expected_len)
        arr = arr[:expected_len]
    elif len(arr) < expected_len:
        raise LLMShapeError(f"{expected_key} under-length: got {len(arr)}, want {expected_len}")
    elif len(arr) > expected_len:
        log.warning("safe_llm_json: %s over by %d (len=%d, want=%d), truncating", expected_key, len(arr) - expected_len, len(arr), expected_len)
        arr = arr[:expected_len]

    return arr


def strip_markdown_fences(raw: str) -> str:
    """Strip ```json ... ``` fences if the model emitted them around a bare JSON object."""
    return _MARKDOWN_FENCE_RE.sub("", raw).strip()
