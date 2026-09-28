"""Real-time reader for public Telegram channels (Telethon, logs in as a normal user account).

Bots can't read channels they aren't admins of, so this uses a user session.
One-time setup: `python -m newsbot telegram-login` (asks for phone number + code).
Tip: use a separate account/number, not your personal one.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import Settings, Source
from ..db import DB
from . import store_item, to_utc

log = logging.getLogger(__name__)


def split_message(text: str) -> tuple[str, str]:
    """Telegram posts have no title: use the first line (or first sentence) as the headline."""
    text = text.strip()
    first_line = text.split("\n", 1)[0].strip()
    if len(first_line) > 220:
        cut = first_line.find(". ", 40, 220)
        first_line = first_line[: cut + 1] if cut > 0 else first_line[:220]
    return first_line, text


def make_client(settings: Settings):
    from telethon import TelegramClient
    return TelegramClient(settings.tg_session, settings.tg_api_id, settings.tg_api_hash)


async def login(settings: Settings) -> None:
    client = make_client(settings)
    await client.start()  # interactive: phone, code, 2FA password
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username}). Session saved to {settings.tg_session}.session")
    await client.disconnect()


async def run(db: DB, settings: Settings, stop: asyncio.Event) -> None:
    from telethon import events

    client = make_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        log.error("Telegram reader not logged in. Run: python -m newsbot telegram-login")
        return

    by_handle: dict[str, Source] = {}
    entities = []
    for s in settings.telegram_sources:
        try:
            ent = await client.get_entity(s.handle)
            entities.append(ent)
            by_handle[str(ent.id)] = s
        except Exception as e:  # noqa: BLE001
            log.warning("Can't resolve Telegram channel %s: %s", s.handle, e)

    @client.on(events.NewMessage(chats=entities))
    async def on_message(event):
        source = by_handle.get(str(event.chat_id).replace("-100", "", 1)) or by_handle.get(str(event.chat_id))
        text = event.message.message or ""
        if not source or len(text) < 25:
            return
        title, body = split_message(text)
        chat = await event.get_chat()
        url = f"https://t.me/{chat.username}/{event.message.id}" if getattr(chat, "username", None) else None
        store_item(db, settings, source, title, body, url, to_utc(event.message.date))

    log.info("Telegram reader listening to %d channels", len(entities))
    await stop.wait()
    await client.disconnect()
