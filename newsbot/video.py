"""Video replies: when a post is about a filmed moment (a speech, briefing, interview...), find the
official upload and post it as a reply under the news, with a playable link preview.

1. The scorer sets video_moment on posts about a filmed occasion (see llm.py).
2. Official channels' feeds (config/sources.yaml, video:) are polled every VIDEO_POLL_SEC.
3. For each published post with a moment, uploads from 12 hours before the post to
   VIDEO_WINDOW_HOURS after it that share a name or keyword with the moment are shown to Claude,
   which picks the one showing that same occasion, or none. Each post/video pair is judged once.
4. A match becomes a video reply: sent to the admin chat for approval (or approved directly with
   VIDEO_AUTO_POST=true), then posted as a reply to the channel post.

Official uploads usually appear 30 minutes to a few hours after the event, so the news never waits
for the video. Videos are linked, not re-uploaded: YouTube links play inside Telegram, and other
pages (kremlin.ru) show a large preview card."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections import Counter
from datetime import timedelta

import httpx

from .config import Settings
from .db import DB, now, parse_iso
from .ingest import clean
from .ingest.rss import fetch_source
from .llm import LLM, LimitReached
from .publisher import is_transient

log = logging.getLogger(__name__)
LOOKBACK = timedelta(hours=12)  # a live stream or speech can be uploaded before the news about it
MAX_VIDEO_AGE = timedelta(days=2)  # older uploads in a feed are never stored
MAX_CANDIDATES = 15  # videos shown to Claude in one check
STOPWORDS = {
    "about", "address", "after", "against", "amid", "briefing", "conference", "delivers", "during",
    "full", "from", "interview", "joins", "live", "meeting", "minister", "news", "official", "press",
    "president", "remarks", "says", "secretary", "speaks", "speech", "statement", "that", "their",
    "this", "visit", "with",
}


def keywords(text: str) -> set[str]:
    """Distinctive words (names, places, occasions) used to pick which videos are worth a Claude check."""
    return {w for w in re.findall(r"[^\W_]{4,}", text.casefold()) if w not in STOPWORDS}


def reply_html(video) -> str:
    url = html.escape(video["url"], quote=True)
    return f'▶️ <a href="{url}">Watch: {html.escape(video["title"])}</a> ({html.escape(video["source"])})'


# ---------- 1. collect uploads ----------
async def poll_once(db: DB, settings: Settings, client: httpx.AsyncClient) -> int:
    """Store new uploads from the official channels. Returns how many were new."""
    sources = settings.video_sources
    results = await asyncio.gather(*(fetch_source(client, s) for s in sources), return_exceptions=True)
    new = 0
    for source, entries in zip(sources, results):
        if isinstance(entries, Exception):
            log.warning("video feed %s failed: %s", source.name, entries)
            continue
        for e in entries:
            if not e["url"] or not e["published_at"] or e["published_at"] < now() - MAX_VIDEO_AGE:
                continue
            new += db.add_video(source.name, clean(e["title"])[:300], clean(e["body"])[:1000], e["url"],
                                e["published_at"])
    return new


# ---------- 2. match uploads to posts ----------
async def match_once(db: DB, settings: Settings, llm: LLM) -> Counter:
    counts: Counter = Counter()
    window = timedelta(hours=settings.video_window_hours)
    for post in db.posts_wanting_video(now() - window):
        created = parse_iso(post["created_at"])
        videos = db.unchecked_videos(post["id"], created - LOOKBACK, created + window)
        if not videos:
            continue
        words = keywords(post["video_moment"])
        related = [dict(v) for v in videos if keywords(f"{v['title']} {v['description'] or ''}") & words]
        candidates = related[:MAX_CANDIDATES]
        n = 0
        if candidates:
            try:
                n = await llm.match_video(dict(post), candidates)
            except LimitReached:
                counts["waiting_for_limit"] += 1
                break  # nothing marked as checked: retried once the limit resets
            except Exception:  # noqa: BLE001
                log.exception("video match failed for post #%s", post["id"])
                counts["error"] += 1
                continue
        # unrelated uploads and the ones Claude saw are done; candidates over the cap wait for the next round
        waiting = {v["id"] for v in related[MAX_CANDIDATES:]}
        db.mark_checked(post["id"], [v["id"] for v in videos if v["id"] not in waiting])
        if n:
            video = candidates[n - 1]
            status = "approved" if settings.video_auto_post else "pending_approval"
            db.create_video_reply(post["id"], video["id"], status)
            log.info("video for post #%s (%s): %s %s", post["id"], status, video["title"], video["url"])
            counts["matched"] += 1
        else:
            counts["no_match"] += 1
    return counts


# ---------- 3. publish approved replies ----------
async def publish_replies(db: DB, settings: Settings, bot=None) -> int:
    """Post approved video replies under their channel posts. Returns how many went out."""
    sent = 0
    for reply in db.video_replies_by_status("approved"):
        post, video = db.get_post(reply["post_id"]), db.get_video(reply["video_id"])
        if settings.dry_run or bot is None or not settings.telegram_channel_id:
            print(f"\n[DRY RUN] would reply to post #{post['id']} with video:\n  {video['title']}\n  {video['url']}\n")
            db.update_video_reply(reply["id"], status="published")
            sent += 1
            continue
        message_id = db.telegram_message_id(post["id"])
        if not message_id:
            db.update_video_reply(reply["id"], status="failed", error="the post has no Telegram message")
            continue
        from telegram import LinkPreviewOptions, ReplyParameters
        try:
            msg = await bot.send_message(
                chat_id=settings.telegram_channel_id, text=reply_html(video), parse_mode="HTML",
                reply_parameters=ReplyParameters(message_id=int(message_id), allow_sending_without_reply=True),
                link_preview_options=LinkPreviewOptions(url=video["url"], prefer_large_media=True))
        except Exception as e:  # noqa: BLE001
            if is_transient(e):
                log.warning("video reply #%s: network error, retrying later (%s)", reply["id"], e)
                break
            log.exception("video reply #%s failed", reply["id"])
            db.update_video_reply(reply["id"], status="failed", error=str(e)[:500])
            continue
        db.update_video_reply(reply["id"], status="published", external_id=str(msg.message_id))
        log.info("published video reply #%s under post #%s", reply["id"], post["id"])
        sent += 1
    return sent


async def run(db: DB, settings: Settings, llm: LLM, bot, stop: asyncio.Event, interval: float = 10) -> None:
    log.info("Video replies: %d channels every %ss (%s)", len(settings.video_sources), settings.video_poll_sec,
             "auto-post" if settings.video_auto_post else "with approval")
    last_poll = float("-inf")
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                if time.monotonic() - last_poll >= settings.video_poll_sec:
                    last_poll = time.monotonic()
                    new = await poll_once(db, settings, client)
                    counts = await match_once(db, settings, llm)
                    if new or counts:
                        log.info("video cycle: %s new uploads, %s", new, dict(counts))
                await publish_replies(db, settings, bot)
            except Exception:  # noqa: BLE001
                log.exception("video loop error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
