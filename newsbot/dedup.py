"""Duplicate detection.

Two layers:
1. exact: hash of the normalized headline (same story re-posted by the same feed, or copied verbatim)
2. near-duplicate: fuzzy token similarity against stories seen in the last N hours

The fuzzy layer only catches similar wording. Stories about the same event worded differently
are caught later by Claude, which sees the recent posts and sets duplicate_of (processor.py).
Upgrade path: embeddings (e.g. pgvector) so that
"Estonia expels Russian diplomat" and "Tallinn orders envoy to leave" land in one cluster,
across languages too.
"""
from __future__ import annotations

import hashlib
import re
from typing import Iterable

from rapidfuzz import fuzz

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "at", "by", "with", "from", "as",
    "is", "are", "was", "were", "be", "has", "have", "had", "it", "its", "that", "this", "after",
    "over", "into", "says", "said", "say", "will", "would", "could", "new", "report", "reports",
    "breaking", "just", "live", "update", "updates", "video", "photo",
    "и", "в", "во", "на", "с", "со", "по", "к", "о", "об", "от", "за", "для", "из", "что", "как",
}
_WORD = re.compile(r"\w+", re.UNICODE)


def normalize(text: str) -> str:
    words = [w for w in _WORD.findall(text.lower()) if w not in STOPWORDS and len(w) > 1]
    return " ".join(words)


def text_hash(norm: str) -> str:
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def find_cluster(norm: str, candidates: Iterable, threshold: int) -> int | None:
    """Return the id of the most similar recent cluster, or None.
    candidates: rows/objects with .id/.norm or ['id']/['norm']."""
    tokens = norm.split()
    if len(tokens) < 3:  # too short to compare safely
        return None
    # short headlines need a stricter match, subset matches are easy for them
    limit = threshold if len(tokens) >= 6 else max(threshold, 92)
    best_id, best_score = None, 0.0
    for c in candidates:
        cid, cnorm = (c["id"], c["norm"])
        if len(cnorm.split()) < 3:
            continue
        score = min(fuzz.token_set_ratio(norm, cnorm), fuzz.token_sort_ratio(norm, cnorm) + 15)
        if score >= limit and score > best_score:
            best_id, best_score = cid, score
    return best_id
