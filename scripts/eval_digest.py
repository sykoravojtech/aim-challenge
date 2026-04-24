"""Eval harness — Phase 6A.

Scores a Digest JSON against two axes:
  1. Recall/precision vs evals/golden.jsonl (hand-labelled should_appear pairs).
  2. LLM-as-judge — gpt-4o-mini rates each digest item on
     {relevance, specificity, non_duplication} 1–5 with a justification.

Writes data/evals/run_<ts>.json so trend is inspectable and so
scripts/compare_digests.py has a number to attach to any ablation.

Usage:
  uv run python scripts/eval_digest.py --digest data/compare/phase4_full.json \\
      --aim cee-founder-media
  uv run python scripts/eval_digest.py --digest data/compare/phase4_full.json \\
      --aim cee-founder-media --label phase4_full
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from openai import OpenAI  # noqa: E402

from pipeline import storage  # noqa: E402
from pipeline._util import LLMShapeError, safe_llm_json  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eval")

GOLDEN_PATH = ROOT / "evals" / "golden.jsonl"
EVALS_DIR = ROOT / "data" / "evals"


def load_digest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if isinstance(data.get("sections"), list):
        return data
    for v in data.values():
        if isinstance(v, dict) and isinstance(v.get("sections"), list):
            return v
    raise SystemExit(f"{path}: no 'sections' list found")


def load_golden(aim_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in GOLDEN_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("aim_id") == aim_id:
            rows.append(row)
    if not rows:
        raise SystemExit(f"no golden rows for aim {aim_id!r}")
    return rows


def all_items(digest: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in digest.get("sections") or []:
        for it in (s or {}).get("items") or []:
            if isinstance(it, dict):
                out.append(it)
    return out


def score_recall(digest: dict[str, Any], golden: list[dict[str, Any]]) -> dict[str, Any]:
    items = all_items(digest)
    digest_urls: set[str] = set()
    for it in items:
        for u in it.get("source_urls") or []:
            if isinstance(u, str):
                digest_urls.add(u)

    positives = {g["source_url"] for g in golden if g.get("should_appear")}
    negatives = {g["source_url"] for g in golden if not g.get("should_appear")}

    pos_hits = positives & digest_urls
    neg_hits = negatives & digest_urls

    recall = len(pos_hits) / len(positives) if positives else 0.0
    # Precision on labelled set: of digest items whose URL is labelled,
    # what fraction are positives? Unlabelled URLs excluded — they aren't wrong.
    labelled_in_digest = (positives | negatives) & digest_urls
    precision_labelled = (
        len(pos_hits) / len(labelled_in_digest) if labelled_in_digest else None
    )

    return {
        "positives_total": len(positives),
        "positives_hit": len(pos_hits),
        "positives_missed": sorted(positives - pos_hits),
        "positives_hit_urls": sorted(pos_hits),
        "negatives_total": len(negatives),
        "negatives_hit": len(neg_hits),
        "negatives_hit_urls": sorted(neg_hits),
        "recall_at_k": round(recall, 3),
        "precision_on_labelled": (
            round(precision_labelled, 3) if precision_labelled is not None else None
        ),
        "digest_item_count": len(items),
        "digest_unique_urls": len(digest_urls),
    }


JUDGE_SYSTEM = (
    "You are a strict evaluator of market-intelligence digest items. "
    "For each item you are given a structured Aim (the user's monitoring spec) "
    "and one digest item plus the titles of every other item in the same digest "
    "(for non-duplication judgement). Score on three axes, 1–5 integers:\n"
    "  relevance — does the item match the Aim's entities/regions/update_types?\n"
    "  specificity — is the body concrete (names, numbers, dates) vs. generic?\n"
    "  non_duplication — is this item meaningfully distinct from the other titles?\n"
    "Return strict JSON: {\"relevance\": int, \"specificity\": int, "
    "\"non_duplication\": int, \"justification\": str}. "
    "Keep justification under 30 words. The literal word JSON must appear in your thinking."
)


def judge_item(
    oai: OpenAI,
    aim: dict[str, Any],
    item: dict[str, Any],
    other_titles: list[str],
) -> dict[str, Any]:
    aim_payload = {
        k: aim.get(k)
        for k in ("title", "summary", "monitored_entities", "regions", "update_types")
    }
    user_msg = json.dumps(
        {
            "aim": aim_payload,
            "item": {
                "title": item.get("title"),
                "body": item.get("body"),
                "item_type": item.get("item_type"),
                "source_urls": item.get("source_urls"),
            },
            "other_item_titles_in_digest": other_titles,
        },
        ensure_ascii=False,
    )
    resp = oai.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
    )
    raw = resp.choices[0].message.content or ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMShapeError(f"judge returned non-JSON: {e}; raw={raw[:160]!r}") from e
    out: dict[str, Any] = {}
    for k in ("relevance", "specificity", "non_duplication"):
        v = parsed.get(k)
        try:
            out[k] = max(1, min(5, int(v)))
        except (TypeError, ValueError):
            out[k] = None
    out["justification"] = str(parsed.get("justification", ""))[:400]
    return out


ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_DIM = "\033[2m"
ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"


def _color_for(score: int | None) -> str:
    if score is None:
        return ANSI_DIM
    if score >= 4:
        return ANSI_GREEN
    if score == 3:
        return ANSI_YELLOW
    return ANSI_RED


def _bar(value: float, vmax: float, width: int = 20) -> str:
    """Unicode block bar — works in terminals and renders fine in markdown."""
    if vmax <= 0:
        return " " * width
    frac = max(0.0, min(1.0, value / vmax))
    full = int(frac * width)
    # half-block for finer resolution
    remainder = (frac * width) - full
    half = "▌" if remainder >= 0.5 else ""
    if full >= width:
        return "█" * width
    return "█" * full + half + "░" * (width - full - len(half))


def render_terminal_bars(recall: dict[str, Any], agg: dict[str, Any] | None) -> str:
    lines = [f"\n{ANSI_BOLD}Scorecard{ANSI_RESET}"]
    r = recall["recall_at_k"]
    r_color = ANSI_GREEN if r >= 0.7 else ANSI_YELLOW if r >= 0.4 else ANSI_RED
    lines.append(
        f"  recall@k      {r_color}{_bar(r, 1.0)}{ANSI_RESET} {r:.2f}  "
        f"({recall['positives_hit']}/{recall['positives_total']} pos, "
        f"{recall['negatives_hit']}/{recall['negatives_total']} neg)"
    )
    if agg:
        for axis, label in (
            ("relevance", "relevance    "),
            ("specificity", "specificity  "),
            ("non_duplication", "non-duplic.  "),
        ):
            v = agg.get(axis + "_mean")
            if v is None:
                lines.append(f"  {label} {ANSI_DIM}(no scores){ANSI_RESET}")
                continue
            c = _color_for(round(v))
            lines.append(f"  {label} {c}{_bar(v, 5.0)}{ANSI_RESET} {v:.2f} / 5")
    return "\n".join(lines)


def render_markdown(summary: dict[str, Any]) -> str:
    recall = summary["recall"]
    agg = summary.get("judge_aggregate")
    per_item = summary.get("judge_per_item") or []

    lines: list[str] = []
    lines.append(f"# Eval run — `{summary['label']}` / `{summary['aim_id']}`")
    lines.append("")
    lines.append(
        "One eval pass over a captured Digest. Two independent signals: "
        "**recall/precision** against a hand-labelled golden set "
        "(`evals/golden.jsonl`), and an **LLM-as-judge** pass where "
        "`gpt-4o-mini` scores each digest item 1–5 on three axes. "
        "Treat the two as complementary — recall tells you *which* items are "
        "surfaced, the judge tells you *how good* the surfaced items are."
    )
    lines.append("")
    lines.append(f"- **Digest evaluated:** `{summary['digest_path']}`")
    lines.append(f"- **Captured at:** {summary['captured_at']}")
    lines.append("")
    lines.append("## Scorecard")
    lines.append("")
    lines.append(
        "Headline numbers. Bars are 0→max (1.0 for recall/precision, 5 for "
        "judge axes) so you can eyeball without reading the digit."
    )
    lines.append("")
    lines.append("- **recall@k** — of the URLs I labelled `should_appear=true`, what "
                 "fraction actually appear in this digest. Measures the *generate* "
                 "stage's selection, not just retrieval (the chunk pool contains "
                 "more positives than the digest has slots).")
    lines.append("- **precision (labelled only)** — of digest URLs that also appear "
                 "in the golden set (either label), what fraction are positives. "
                 "Unlabelled URLs are ignored — they aren't scored as wrong. "
                 "Drops below 1.0 iff a labelled *negative* leaks into the digest.")
    lines.append("- **relevance** — judge score: does the item match the Aim's "
                 "entities / regions / update_types?")
    lines.append("- **specificity** — judge score: is the body concrete (names, "
                 "numbers, dates) or generic filler?")
    lines.append("- **non-duplication** — judge score: is this item meaningfully "
                 "distinct from the other items in the same digest?")
    lines.append("")
    lines.append("| metric | bar | value | detail |")
    lines.append("|---|---|---|---|")
    r = recall["recall_at_k"]
    lines.append(
        f"| recall@k | `{_bar(r, 1.0)}` | {r:.2f} | "
        f"{recall['positives_hit']}/{recall['positives_total']} pos hit, "
        f"{recall['negatives_hit']}/{recall['negatives_total']} neg hit |"
    )
    prec = recall.get("precision_on_labelled")
    if prec is not None:
        lines.append(
            f"| precision (labelled only) | `{_bar(prec, 1.0)}` | {prec:.2f} | "
            f"of digest URLs present in the golden set |"
        )
    if agg:
        for axis, label in (
            ("relevance", "relevance"),
            ("specificity", "specificity"),
            ("non_duplication", "non-duplication"),
        ):
            v = agg.get(axis + "_mean")
            if v is None:
                continue
            lines.append(
                f"| {label} | `{_bar(v, 5.0)}` | {v:.2f} / 5 | n={agg[axis + '_n']} |"
            )
    lines.append("")

    pos_missed = recall.get("positives_missed") or []
    neg_hit = recall.get("negatives_hit_urls") or []
    if pos_missed or neg_hit:
        lines.append("## Golden-set diff")
        lines.append("")
        lines.append(
            "Which specific labelled URLs drove the recall/precision numbers above. "
            "**Missed positives** are candidates the retrieve→rerank→generate path "
            "failed to surface — the most actionable list for tuning rerank prompts "
            "or weighting. **Leaked negatives** are hand-labelled off-topic URLs "
            "that made it into the digest — if this list is non-empty, the filter "
            "stack has a hole."
        )
        lines.append("")
        if pos_missed:
            lines.append("**Positives missed** (in golden, absent from digest):")
            for u in pos_missed:
                lines.append(f"- {u}")
            lines.append("")
        if neg_hit:
            lines.append("**Negatives leaked** (labelled off-topic, appeared in digest):")
            for u in neg_hit:
                lines.append(f"- {u}")
            lines.append("")

    if per_item:
        lines.append("## Per-item judge scores")
        lines.append("")
        lines.append(
            "One row per digest item. Each score is `gpt-4o-mini` rating the item "
            "1–5 on the axis (✅ ≥4 · 🟡 3 · 🔴 ≤2). The `note` is the judge's own "
            "one-sentence justification — useful for spotting systematic failure "
            "modes (e.g. every low-relevance note flags the same missing entity)."
        )
        lines.append("")
        lines.append("| # | title | type | rel | spec | nondup | note |")
        lines.append("|---|---|---|---|---|---|---|")

        def _cell(v: int | None) -> str:
            if v is None:
                return "—"
            mark = "✅" if v >= 4 else "🟡" if v == 3 else "🔴"
            return f"{mark} {v}"

        for i, it in enumerate(per_item):
            title = str(it.get("title") or "").replace("|", "\\|")[:70]
            note = str(it.get("justification") or "").replace("|", "\\|").replace("\n", " ")[:120]
            lines.append(
                f"| {i} | {title} | {it.get('item_type', '—')} | "
                f"{_cell(it.get('relevance'))} | {_cell(it.get('specificity'))} | "
                f"{_cell(it.get('non_duplication'))} | {note} |"
            )
        lines.append("")

    return "\n".join(lines)


def aggregate(per_item: list[dict[str, Any]]) -> dict[str, Any]:
    axes = ("relevance", "specificity", "non_duplication")
    agg: dict[str, Any] = {}
    for axis in axes:
        vals = [j[axis] for j in per_item if isinstance(j.get(axis), int)]
        agg[axis + "_mean"] = round(sum(vals) / len(vals), 2) if vals else None
        agg[axis + "_n"] = len(vals)
    # item_type mix for quick eyeball
    types = Counter(j.get("item_type", "unknown") for j in per_item)
    agg["item_type_mix"] = dict(types)
    return agg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--digest", type=Path, required=True,
                    help="path to a captured Digest JSON")
    ap.add_argument("--aim", required=True, help="aim_id to evaluate against")
    ap.add_argument("--label", default=None,
                    help="short name for this run (defaults to digest filename stem)")
    ap.add_argument("--skip-judge", action="store_true",
                    help="skip the LLM-as-judge pass (recall only)")
    args = ap.parse_args()

    aim = storage.get_aim(args.aim)
    if aim is None:
        raise SystemExit(f"aim {args.aim!r} not found under data/aims/")
    aim_dict = aim if isinstance(aim, dict) else aim.model_dump()

    digest = load_digest(args.digest)
    golden = load_golden(args.aim)
    items = all_items(digest)

    recall_block = score_recall(digest, golden)
    log.info(
        "recall@k=%.2f  pos %d/%d hit  neg %d/%d hit",
        recall_block["recall_at_k"],
        recall_block["positives_hit"],
        recall_block["positives_total"],
        recall_block["negatives_hit"],
        recall_block["negatives_total"],
    )

    per_item: list[dict[str, Any]] = []
    if not args.skip_judge and items:
        oai = OpenAI()
        titles = [str(it.get("title", "")) for it in items]
        for i, it in enumerate(items):
            others = [t for j, t in enumerate(titles) if j != i]
            try:
                scores = judge_item(oai, aim_dict, it, others)
            except LLMShapeError as e:
                log.warning("judge failed on item %d: %s", i, e)
                scores = {
                    "relevance": None,
                    "specificity": None,
                    "non_duplication": None,
                    "justification": f"judge_error: {e}",
                }
            per_item.append(
                {
                    "title": it.get("title"),
                    "item_type": it.get("item_type"),
                    "source_urls": it.get("source_urls"),
                    **scores,
                }
            )
            if scores.get("relevance") is not None:
                cr = _color_for(scores["relevance"])
                cs = _color_for(scores["specificity"])
                cn = _color_for(scores["non_duplication"])
                log.info(
                    "  [%d] rel=%s%s%s spec=%s%s%s nondup=%s%s%s — %s",
                    i,
                    cr, scores["relevance"], ANSI_RESET,
                    cs, scores["specificity"], ANSI_RESET,
                    cn, scores["non_duplication"], ANSI_RESET,
                    str(it.get("title"))[:60],
                )

    summary = {
        "label": args.label or args.digest.stem,
        "aim_id": args.aim,
        "digest_path": str(args.digest.relative_to(ROOT)) if args.digest.is_absolute() else str(args.digest),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "recall": recall_block,
        "judge_aggregate": aggregate(per_item) if per_item else None,
        "judge_per_item": per_item,
    }

    EVALS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVALS_DIR / f"run_{ts}_{args.aim}_{summary['label']}.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    log.info("wrote %s", out.relative_to(ROOT))

    md_path = out.with_suffix(".md")
    md_path.write_text(render_markdown(summary))
    log.info("wrote %s", md_path.relative_to(ROOT))

    agg = summary["judge_aggregate"]
    print()
    print(f"{ANSI_BOLD}== eval: {summary['label']} / {args.aim} =={ANSI_RESET}")
    print(render_terminal_bars(recall_block, agg))


if __name__ == "__main__":
    main()
