"""Diff two Digest JSON files and print a readable before/after table.

Stdlib-only so an upstream pipeline bug can't break the demo exhibit.
Accepts raw Digest dicts, pre-Phase-1 shapes (headline/sections at top
level), and wrapper dicts that nest a digest one level deep.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def load_digest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: {path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"error: {path}: top-level is not a JSON object", file=sys.stderr)
        sys.exit(1)
    if isinstance(data.get("sections"), list):
        return data
    # Wrapper shape (phase2_dedup.json): first nested dict whose
    # `sections` is a list (skip e.g. funnel.sections which is an int).
    for v in data.values():
        if isinstance(v, dict) and isinstance(v.get("sections"), list):
            return v
    print(f"error: {path}: no 'sections' list found", file=sys.stderr)
    sys.exit(1)


def all_items(digest: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in digest.get("sections") or []:
        for it in (s or {}).get("items") or []:
            if isinstance(it, dict):
                out.append(it)
    return out


def all_urls(items: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for it in items:
        for u in it.get("source_urls") or []:
            if isinstance(u, str) and u:
                urls.append(u)
    return urls


def host_of(url: str) -> str | None:
    try:
        h = urlparse(url).hostname
    except ValueError:
        return None
    if not h:
        return None
    h = h.lower()
    return h[4:] if h.startswith("www.") else h


def jaccard(a: set[str], b: set[str]) -> tuple[float, int, int]:
    union = a | b
    inter = a & b
    if not union:
        return 0.0, 0, 0
    return len(inter) / len(union), len(inter), len(union)


def mean_relevance(items: list[dict[str, Any]]) -> str:
    scores: list[int] = []
    for it in items:
        raw = it.get("relevance_score")
        if raw is None:
            continue
        try:
            scores.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not scores:
        return "n/a"
    return f"{sum(scores) / len(scores):.2f}"


def section_titles(digest: dict[str, Any]) -> list[str]:
    return [str((s or {}).get("title", "")) for s in digest.get("sections") or []]


def item_type_counts(items: list[dict[str, Any]]) -> Counter:
    return Counter(str(it.get("item_type", "unknown")) for it in items)


def truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def render_table(rows: list[tuple[str, str, str, str]]) -> str:
    # rows: (metric, a, b, delta)
    w0 = max(len(r[0]) for r in rows)
    w1 = max(len(r[1]) for r in rows)
    w2 = max(len(r[2]) for r in rows)
    w3 = max(len(r[3]) for r in rows)
    lines = []
    sep = f"{'-' * w0}-+-{'-' * w1}-+-{'-' * w2}-+-{'-' * w3}"
    for i, (m, a, b, d) in enumerate(rows):
        lines.append(f"{m.ljust(w0)} | {a.ljust(w1)} | {b.ljust(w2)} | {d.ljust(w3)}")
        if i == 0:
            lines.append(sep)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Diff two Digest JSON files.")
    ap.add_argument("file_a", type=Path)
    ap.add_argument("file_b", type=Path)
    ap.add_argument("--label-a", default=None)
    ap.add_argument("--label-b", default=None)
    args = ap.parse_args()

    la = args.label_a or args.file_a.name
    lb = args.label_b or args.file_b.name
    a = load_digest(args.file_a)
    b = load_digest(args.file_b)

    items_a, items_b = all_items(a), all_items(b)
    urls_a, urls_b = set(all_urls(items_a)), set(all_urls(items_b))
    hosts_a = {h for h in (host_of(u) for u in urls_a) if h}
    hosts_b = {h for h in (host_of(u) for u in urls_b) if h}
    uj, ui, uu = jaccard(urls_a, urls_b)
    hj, hi, hu = jaccard(hosts_a, hosts_b)

    head_a, head_b = str(a.get("headline", "")), str(b.get("headline", ""))
    dr_a, dr_b = str(a.get("date_range", "")), str(b.get("date_range", ""))

    rows: list[tuple[str, str, str, str]] = [
        ("metric", la, lb, "delta"),
        ("headline", truncate(head_a, 48), truncate(head_b, 48),
         "same" if head_a == head_b else "differ"),
        ("date_range", truncate(dr_a, 48), truncate(dr_b, 48),
         "same" if dr_a == dr_b else "differ"),
        ("section count", str(len(a.get("sections") or [])),
         str(len(b.get("sections") or [])),
         f"{len(b.get('sections') or []) - len(a.get('sections') or []):+d}"),
        ("total items", str(len(items_a)), str(len(items_b)),
         f"{len(items_b) - len(items_a):+d}"),
        ("unique source urls", str(len(urls_a)), str(len(urls_b)),
         f"{len(urls_b) - len(urls_a):+d}"),
        ("distinct hosts", str(len(hosts_a)), str(len(hosts_b)),
         f"{len(hosts_b) - len(hosts_a):+d}"),
        ("url jaccard", "", "", f"{uj:.2f} ({ui} shared / {uu} total)"),
        ("host jaccard", "", "", f"{hj:.2f} ({hi} shared / {hu} total)"),
        ("mean relevance", mean_relevance(items_a), mean_relevance(items_b), ""),
    ]
    print("Scalar metrics")
    print(render_table(rows))
    print()

    # item_type distribution
    tc_a, tc_b = item_type_counts(items_a), item_type_counts(items_b)
    all_types = sorted(set(tc_a) | set(tc_b))
    type_rows: list[tuple[str, str, str, str]] = [("item_type", la, lb, "delta")]
    for t in all_types:
        ca, cb = tc_a.get(t, 0), tc_b.get(t, 0)
        type_rows.append((t, str(ca), str(cb), f"{cb - ca:+d}"))
    print("Item type distribution")
    if len(type_rows) > 1:
        print(render_table(type_rows))
    else:
        print("(no items on either side)")
    print()

    # Section titles stacked
    titles_a, titles_b = section_titles(a), section_titles(b)
    print(f"Section titles: {la}")
    for i, t in enumerate(titles_a, 1):
        print(f"  {i}. {t}")
    if not titles_a:
        print("  (none)")
    print()
    print(f"Section titles: {lb}")
    for i, t in enumerate(titles_b, 1):
        print(f"  {i}. {t}")
    if not titles_b:
        print("  (none)")


if __name__ == "__main__":
    main()
