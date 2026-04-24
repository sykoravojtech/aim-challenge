"""Unit tests for collapse_chunks_by_article — the per-article-cap logic
regressed once (6G: single-chunk collapse starved long CEE articles) so it
gets a targeted test."""
from __future__ import annotations

from pipeline.retrieval import collapse_chunks_by_article


def _mk(article_id: str, score: float, chunk_index: int = 0, **extra):
    return {
        "article_id": article_id,
        "score": score,
        "chunk_index": chunk_index,
        **extra,
    }


def test_empty_input():
    assert collapse_chunks_by_article([], top_k=10) == []


def test_drops_chunks_without_article_id():
    chunks = [
        _mk("a", 0.9),
        {"score": 0.95},  # no article_id
    ]
    out = collapse_chunks_by_article(chunks, top_k=10)
    assert len(out) == 1
    assert out[0]["article_id"] == "a"


def test_single_chunk_per_article_default_cap():
    # Default per_article_cap=1: one chunk per article, best score wins.
    chunks = [
        _mk("a", 0.5, chunk_index=0),
        _mk("a", 0.9, chunk_index=1),
        _mk("a", 0.7, chunk_index=2),
        _mk("b", 0.6, chunk_index=0),
    ]
    out = collapse_chunks_by_article(chunks, top_k=10)
    assert len(out) == 2
    # Article order: a (best=0.9) before b (best=0.6)
    assert out[0]["article_id"] == "a"
    assert out[0]["score"] == 0.9
    assert out[1]["article_id"] == "b"


def test_per_article_cap_keeps_top_n_per_article():
    chunks = [
        _mk("a", 0.5, chunk_index=0),
        _mk("a", 0.9, chunk_index=1),
        _mk("a", 0.7, chunk_index=2),
        _mk("a", 0.3, chunk_index=3),
        _mk("b", 0.8, chunk_index=0),
        _mk("b", 0.6, chunk_index=1),
    ]
    out = collapse_chunks_by_article(chunks, top_k=10, per_article_cap=3)
    # a: top 3 by score (0.9, 0.7, 0.5), b: both (0.8, 0.6). Total 5.
    assert len(out) == 5
    a_chunks = [c for c in out if c["article_id"] == "a"]
    assert len(a_chunks) == 3
    assert [c["score"] for c in a_chunks] == [0.9, 0.7, 0.5]
    b_chunks = [c for c in out if c["article_id"] == "b"]
    assert [c["score"] for c in b_chunks] == [0.8, 0.6]


def test_top_k_limits_articles_not_chunks():
    # 5 articles × 2 chunks each, top_k=3, per_article_cap=2 → 6 chunks, 3 articles.
    chunks = []
    scores = [0.9, 0.8, 0.7, 0.6, 0.5]
    for i, s in enumerate(scores):
        chunks.append(_mk(f"art{i}", s, chunk_index=0))
        chunks.append(_mk(f"art{i}", s - 0.05, chunk_index=1))
    out = collapse_chunks_by_article(chunks, top_k=3, per_article_cap=2)
    assert len(out) == 6
    article_ids = {c["article_id"] for c in out}
    assert article_ids == {"art0", "art1", "art2"}


def test_sort_key_switches_ranking_basis():
    # Once rerank has run, chunks carry rerank_score. Collapse should use it.
    chunks = [
        _mk("a", 0.9, chunk_index=0, rerank_score=2),
        _mk("b", 0.5, chunk_index=0, rerank_score=9),
        _mk("a", 0.8, chunk_index=1, rerank_score=4),
    ]
    out = collapse_chunks_by_article(
        chunks, top_k=10, per_article_cap=1, sort_key="rerank_score"
    )
    # b wins (rerank_score=9) over a (rerank_score=4).
    assert out[0]["article_id"] == "b"
    assert out[1]["article_id"] == "a"
    assert out[1]["rerank_score"] == 4  # best rerank chunk of 'a' won


def test_missing_score_treated_as_zero():
    chunks = [
        _mk("a", 0.5),
        {"article_id": "b", "chunk_index": 0},  # no score
    ]
    out = collapse_chunks_by_article(chunks, top_k=10)
    assert len(out) == 2
    assert out[0]["article_id"] == "a"  # 0.5 > 0
