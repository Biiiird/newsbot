"""Takes new stories, asks the LLM to score + rewrite them, then routes each one:
drop / send to approval / auto-approve."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter

from .config import Settings
from .db import DB
from .llm import LLM, Assessment, LimitReached
from .media import MediaResolver

log = logging.getLogger(__name__)


def resolver_from_settings(settings: Settings) -> MediaResolver | None:
    if not settings.attach_images:
        return None
    return MediaResolver(settings.media_max_images)


def route(a: Assessment, trust: str, settings: Settings) -> str:
    """Return 'dropped', 'pending_approval' or 'approved'."""
    if not a.is_news or a.importance < settings.min_importance or not a.headline:
        return "dropped"
    if (settings.auto_post and a.importance >= settings.auto_min_importance
            and (a.risk == "low" or not settings.auto_require_low_risk)
            and (trust == "high" or not settings.auto_require_high_trust)):
        return "approved"
    return "pending_approval"


async def process_once(db: DB, settings: Settings, llm: LLM, limit: int = 10,
                       resolver: MediaResolver | None = None) -> Counter:
    counts: Counter = Counter()
    for row in db.new_clusters(limit):
        item = dict(row)
        cid = item["cluster_id"]
        recent = [dict(p) for p in db.recent_posts(settings.dedup_window_hours)]
        item["recent_posts"] = recent
        try:
            a = await llm.assess(item)
        except LimitReached:
            # keep the story as 'new' so it is retried once the limit resets
            counts["waiting_for_limit"] += 1
            break
        except Exception as e:  # noqa: BLE001
            log.exception("LLM failed for cluster %s", cid)
            db.set_cluster(cid, "error", reason=str(e)[:500])
            counts["error"] += 1
            continue

        if a.duplicate_of and any(p["id"] == a.duplicate_of for p in recent):
            # same event as an earlier post, worded too differently for the fuzzy dedup
            db.set_cluster(cid, "dropped", importance=a.importance, risk=a.risk, category=a.category,
                           reason=f"duplicate of post #{a.duplicate_of}: {a.reason}"[:500])
            log.info("duplicate of #%s, dropped: %s", a.duplicate_of, item["title"])
            counts["duplicate"] += 1
            continue

        decision = route(a, item["trust"], settings)
        db.set_cluster(cid, "dropped" if decision == "dropped" else "scored",
                       importance=a.importance, risk=a.risk, category=a.category, reason=a.reason)
        if decision != "dropped":
            media = await resolver.resolve(a.people) if resolver else []
            db.create_post(cid, a.headline, item["source"], item.get("url"), decision,
                           a.importance, a.risk, a.category, media)
            log.info("%s (%s/%s): %s%s", decision, a.importance, a.risk, a.headline,
                     f" [images: {', '.join(m['name'] for m in media)}]" if media else "")
        counts[decision] += 1
    return counts


async def run(db: DB, settings: Settings, llm: LLM, stop: asyncio.Event, interval: float = 5) -> None:
    names = {"mock": "mock", "claude_cli": f"claude -p ({settings.claude_cli_model}, subscription)"}
    log.info("Processor running (model: %s)", names.get(settings.llm_provider, settings.llm_model))
    resolver = resolver_from_settings(settings)
    while not stop.is_set():
        counts = await process_once(db, settings, llm, resolver=resolver)
        wait = interval
        if counts.get("waiting_for_limit"):
            wait = 300  # subscription limit hit: stories stay queued, retry in 5 minutes
            log.warning("Usage limit reached, stories wait until it resets (retry in 5 min)")
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
