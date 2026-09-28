"""RSS/Atom poller."""
from __future__ import annotations

import asyncio
import calendar
import logging
from collections import Counter
from datetime import datetime, timezone

import feedparser
import httpx

from ..config import Settings, Source
from ..db import DB
from . import store_item

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (newsbot MVP; RSS reader)"}


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


def parse_feed(content: bytes | str) -> list[dict]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        out.append({
            "title": e.get("title", ""),
            "body": e.get("summary", "") or e.get("description", ""),
            "url": e.get("link"),
            "published_at": _entry_time(e),
        })
    return out


async def fetch_source(client: httpx.AsyncClient, source: Source) -> list[dict]:
    resp = await client.get(source.url, headers=HEADERS, follow_redirects=True, timeout=20)
    resp.raise_for_status()
    return parse_feed(resp.content)


async def poll_once(db: DB, settings: Settings, client: httpx.AsyncClient | None = None) -> Counter:
    counts: Counter = Counter()
    own_client = client is None
    client = client or httpx.AsyncClient()
    try:
        results = await asyncio.gather(
            *(fetch_source(client, s) for s in settings.rss_sources), return_exceptions=True)
        for source, entries in zip(settings.rss_sources, results):
            if isinstance(entries, Exception):
                log.warning("RSS %s failed: %s", source.name, entries)
                counts["error"] += 1
                continue
            for e in entries:
                counts[store_item(db, settings, source, e["title"], e["body"], e["url"], e["published_at"])] += 1
    finally:
        if own_client:
            await client.aclose()
    return counts


async def run(db: DB, settings: Settings, stop: asyncio.Event) -> None:
    log.info("RSS poller: %d feeds every %ss", len(settings.rss_sources), settings.rss_poll_sec)
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            counts = await poll_once(db, settings, client)
            if counts.get("new"):
                log.info("RSS cycle: %s", dict(counts))
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.rss_poll_sec)
            except asyncio.TimeoutError:
                pass
