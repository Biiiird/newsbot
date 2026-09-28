"""Publishes approved posts to Telegram and X, respecting rate limits.
With DRY_RUN=true, posts are only printed to the console."""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import timedelta
from typing import Awaitable, Callable, Protocol

from . import media
from .config import Settings
from .db import DB, iso, now

log = logging.getLogger(__name__)
Notify = Callable[[str], Awaitable[None]]
CAPTION_LIMIT = 1024  # Telegram's limit for photo captions (plain messages allow 4096)
RETRY_DELAYS = (30, 60, 120, 240, 480)  # seconds to wait after each network failure, then give up


def is_transient(e: Exception) -> bool:
    """Network trouble or flood control: worth retrying. Anything else (bad request, no rights) is not."""
    try:
        from telegram.error import BadRequest, NetworkError, RetryAfter
        if isinstance(e, RetryAfter) or (isinstance(e, NetworkError) and not isinstance(e, BadRequest)):
            return True
    except ImportError:
        pass
    import httpx
    return isinstance(e, (httpx.TransportError, ConnectionError, asyncio.TimeoutError))


class Target(Protocol):
    name: str

    async def publish(self, post) -> str | None:
        """Publish and return the platform's message id. Raise on failure."""


BOLD_PREFIXES = ("JUST IN:", "BREAKING:")


def headline_html(headline: str) -> str:
    """Escaped headline with the JUST IN: / BREAKING: prefix in bold."""
    for prefix in BOLD_PREFIXES:
        if headline.startswith(prefix):
            return f"<b>{prefix}</b>{html.escape(headline[len(prefix):])}"
    return html.escape(headline)


def telegram_text(post, include_source: bool) -> str:
    text = headline_html(post["headline"])
    if include_source and post["source_url"]:
        text += f'\n\nSource: <a href="{html.escape(post["source_url"], quote=True)}">{html.escape(post["source_name"] or "link")}</a>'
    return text


def caption_fits(post, include_source: bool) -> bool:
    """Whether the visible text fits in a photo caption (Telegram counts UTF-16 code units)."""
    visible = post["headline"]
    if include_source and post["source_url"]:
        visible += f"\n\nSource: {post['source_name'] or 'link'}"
    return len(visible.encode("utf-16-le")) // 2 <= CAPTION_LIMIT


class ConsoleTarget:
    name = "dry_run"

    async def publish(self, post) -> str | None:
        images = "".join(f"\n  image: {m['name']} {m['url']}" for m in media.media_list(post))
        print(f"\n[DRY RUN] would publish post #{post['id']}:\n  {post['headline']}\n  source: {post['source_url']}{images}\n")
        return None


class TelegramTarget:
    name = "telegram"

    def __init__(self, bot, channel_id: str, include_source: bool):
        self.bot, self.channel_id, self.include_source = bot, channel_id, include_source

    async def publish(self, post) -> str | None:
        from telegram import LinkPreviewOptions
        from telegram.error import BadRequest
        text = telegram_text(post, self.include_source)
        images = media.media_list(post)
        if images and not caption_fits(post, self.include_source):
            log.info("post #%s is too long for a photo caption, posting without images", post["id"])
            images = []
        photos = await media.download(images)
        if photos:
            try:
                return await self._publish_photos(text, photos)
            except BadRequest as e:  # nothing was posted, so fall back to text
                log.warning("Telegram rejected the images for post #%s (%s), posting text only", post["id"], e)
        msg = await self.bot.send_message(
            chat_id=self.channel_id, text=text, parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True))
        return str(msg.message_id)

    async def _publish_photos(self, text: str, photos: list[bytes]) -> str:
        from telegram import InputMediaPhoto
        if len(photos) == 1:
            msg = await self.bot.send_photo(chat_id=self.channel_id, photo=photos[0], caption=text,
                                            parse_mode="HTML")
            return str(msg.message_id)
        # an album shows its caption when the caption is on the first photo only
        album = [InputMediaPhoto(data, caption=text, parse_mode="HTML") if i == 0 else InputMediaPhoto(data)
                 for i, data in enumerate(photos)]
        msgs = await self.bot.send_media_group(chat_id=self.channel_id, media=album)
        return str(msgs[0].message_id)


class XTarget:
    name = "x"

    def __init__(self, settings: Settings):
        import tweepy
        self.client = tweepy.Client(
            consumer_key=settings.x_api_key, consumer_secret=settings.x_api_secret,
            access_token=settings.x_access_token, access_token_secret=settings.x_access_secret)
        self.source_reply = settings.x_source_reply

    async def publish(self, post) -> str | None:
        resp = await asyncio.to_thread(self.client.create_tweet, text=post["headline"][:280])
        tweet_id = str(resp.data["id"])
        if self.source_reply and post["source_url"]:
            try:
                await asyncio.to_thread(
                    self.client.create_tweet, text=f"Source: {post['source_url']}",
                    in_reply_to_tweet_id=tweet_id)
            except Exception as e:  # noqa: BLE001  the main post is out, don't fail it
                log.warning("X source reply failed: %s", e)
        return tweet_id


def build_targets(settings: Settings, bot=None) -> list[Target]:
    if settings.dry_run:
        return [ConsoleTarget()]
    targets: list[Target] = []
    if bot is not None and settings.telegram_channel_id:
        targets.append(TelegramTarget(bot, settings.telegram_channel_id, settings.telegram_include_source))
    if settings.x_enabled:
        targets.append(XTarget(settings))
    if not targets:
        log.warning("DRY_RUN=false but no targets configured, falling back to console output")
        targets.append(ConsoleTarget())
    return targets


def wait_seconds(db: DB, settings: Settings) -> float:
    """How long until the rate limits allow the next post (0 = now)."""
    last = db.last_published_at()
    if last:
        gap = (last + timedelta(seconds=settings.min_post_interval_sec) - now()).total_seconds()
        if gap > 0:
            return gap
    if db.published_since(now() - timedelta(hours=1)) >= settings.max_posts_per_hour:
        return 60.0
    return 0.0


async def publish_next(db: DB, settings: Settings, targets: list[Target], notify: Notify | None = None) -> bool:
    """Publish the next approved post if rate limits allow. Returns True if something was published."""
    post = db.next_approved()
    if post is None or wait_seconds(db, settings) > 0:
        return False

    errors, transient = [], True
    for t in targets:
        try:
            ext_id = await t.publish(post)
            db.log_publish(post["id"], t.name, True, ext_id)
        except Exception as e:  # noqa: BLE001
            log.exception("publish to %s failed", t.name)
            db.log_publish(post["id"], t.name, False, error=str(e)[:500])
            errors.append(f"{t.name}: {e}")
            transient = transient and is_transient(e)

    if len(errors) == len(targets):
        attempt = db.failed_attempts(post["id"]) // len(targets)  # failures so far, this one included
        if transient and attempt <= len(RETRY_DELAYS):
            delay = RETRY_DELAYS[attempt - 1]
            db.update_post(post["id"], retry_after=iso(now() + timedelta(seconds=delay)),
                           error="; ".join(errors)[:1000])
            log.warning("post #%s: network error, retry %s/%s in %ss", post["id"], attempt,
                        len(RETRY_DELAYS), delay)
            return False
        db.update_post(post["id"], status="failed", error="; ".join(errors)[:1000])
        if notify:
            await notify(f"⚠️ Post #{post['id']} failed to publish:\n{'; '.join(errors)[:500]}")
        return False

    db.update_post(post["id"], status="published", published_at=iso(), retry_after=None,
                   error="; ".join(errors)[:1000] if errors else None)
    log.info("published #%s to %s", post["id"], ", ".join(t.name for t in targets))
    if notify and errors:
        await notify(f"⚠️ Post #{post['id']} published, but some targets failed:\n{'; '.join(errors)[:500]}")
    return True


async def run(db: DB, settings: Settings, targets: list[Target], stop: asyncio.Event,
              notify: Notify | None = None, interval: float = 5) -> None:
    log.info("Publisher running -> %s", ", ".join(t.name for t in targets))
    while not stop.is_set():
        try:
            await publish_next(db, settings, targets, notify)
        except Exception:  # noqa: BLE001
            log.exception("publisher loop error")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
