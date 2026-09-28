"""Shared ingest step: every source adapter hands raw text to `store_item`."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from html import unescape

from ..config import Settings, Source
from ..db import DB, iso, now
from ..dedup import find_cluster, normalize, text_hash

log = logging.getLogger(__name__)
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def clean(text: str | None) -> str:
    if not text:
        return ""
    return _SPACES.sub(" ", unescape(_TAGS.sub(" ", text))).strip()


def store_item(db: DB, settings: Settings, source: Source, title: str, body: str = "",
               url: str | None = None, published_at: datetime | None = None) -> str:
    """Normalize, dedup and save one item.
    Returns 'duplicate', 'joined' (added to an existing story), 'new' or 'stale'."""
    title = clean(title)[:300]
    body = clean(body)[:4000]
    if not title:
        return "empty"
    norm = normalize(title)
    if not norm:
        return "empty"
    # per-source hash: the same headline from another outlet still joins the story (counts as confirmation)
    h = text_hash(f"{source.name}|{norm}|{url or ''}" if source.kind == "telegram" else f"{source.name}|{norm}")
    if db.hash_exists(h):
        return "duplicate"

    cluster_id = find_cluster(norm, db.recent_clusters(settings.dedup_window_hours), settings.dedup_threshold)
    _, cluster_id, is_new = db.add_item({
        "source": source.name, "source_type": source.kind, "trust": source.trust, "lang": source.lang,
        "state_media": source.state_media, "title": title, "body": body, "url": url,
        "hash": h, "norm": norm, "published_at": iso(published_at) if published_at else None,
    }, cluster_id)

    if not is_new:
        log.debug("joined cluster %s: %s", cluster_id, title)
        return "joined"

    if published_at and published_at < now() - timedelta(minutes=settings.max_item_age_min):
        db.set_cluster(cluster_id, "stale")
        return "stale"
    log.info("new story [%s] %s", source.name, title)
    return "new"


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
