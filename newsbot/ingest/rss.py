"""RSS/Atom poller."""
from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from ..config import Settings, Source
from ..db import DB
from . import store_item

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (newsbot MVP; RSS reader)"}
_OUTLET_SUFFIX = re.compile(r"\s+[-–]\s+[^-–]{2,40}$")  # Google News titles end in " - Reuters"


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
    entries = parse_feed(resp.content)
    for e in entries:
        if "news.google.com" in source.url:
            e["title"] = _OUTLET_SUFFIX.sub("", e["title"])
        if source.clock_offset_hours and e["published_at"]:
            e["published_at"] += timedelta(hours=source.clock_offset_hours)
    return entries


def due_sources(settings: Settings, last_fetch: dict[str, float], at: float) -> list[Source]:
    """Feeds to poll now: all of them, except those with every_sec that were polled too recently."""
    return [s for s in settings.rss_sources
            if not s.every_sec or at - last_fetch.get(s.name, float("-inf")) >= s.every_sec]


async def poll_once(db: DB, settings: Settings, client: httpx.AsyncClient | None = None,
                    last_fetch: dict[str, float] | None = None) -> Counter:
    counts: Counter = Counter()
    own_client = client is None
    client = client or httpx.AsyncClient()
    last_fetch = {} if last_fetch is None else last_fetch
    sources = due_sources(settings, last_fetch, time.monotonic())
    for s in sources:
        last_fetch[s.name] = time.monotonic()
    try:
        results = await asyncio.gather(
            *(fetch_source(client, s) for s in sources), return_exceptions=True)
        for source, entries in zip(sources, results):
            if isinstance(entries, Exception):
                log.warning("RSS %s failed: %s", source.name, entries)
                counts["error"] += 1
                continue
            first_poll = not db.source_seen(source.name)
            for e in entries:
                if first_poll and e["published_at"] is None:
                    e["published_at"] = datetime.min.replace(tzinfo=timezone.utc)  # a new feed's undated backlog
                counts[store_item(db, settings, source, e["title"], e["body"], e["url"], e["published_at"])] += 1
    finally:
        if own_client:
            await client.aclose()
    return counts


async def run(db: DB, settings: Settings, stop: asyncio.Event) -> None:
    log.info("RSS poller: %d feeds every %ss", len(settings.rss_sources), settings.rss_poll_sec)
    last_fetch: dict[str, float] = {}
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            counts = await poll_once(db, settings, client, last_fetch)
            if counts.get("new"):
                log.info("RSS cycle: %s", dict(counts))
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.rss_poll_sec)
            except asyncio.TimeoutError:
                pass
