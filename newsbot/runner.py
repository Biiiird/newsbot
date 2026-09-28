"""Runs every component in one process: RSS poller, Telegram reader, processor, approval bot, publisher."""
from __future__ import annotations

import asyncio
import logging
import signal

from . import approval_bot, processor, publisher
from .config import Settings
from .db import DB
from .ingest import rss, telegram_reader
from .llm import llm_from_settings

log = logging.getLogger(__name__)


async def run_all(settings: Settings) -> None:
    db = DB(settings.db_path)
    llm = llm_from_settings(settings)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    app = None
    notify = None
    if settings.telegram_bot_token and settings.admin_chat_id:
        app = approval_bot.build_app(settings, db)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        async def _notify(text: str) -> None:
            try:
                await app.bot.send_message(settings.admin_chat_id, text)
            except Exception:  # noqa: BLE001
                log.exception("admin notify failed")
        notify = _notify
    else:
        log.warning("No TELEGRAM_BOT_TOKEN/ADMIN_CHAT_ID: approve drafts from the terminal "
                    "with `python -m newsbot list` and `python -m newsbot approve <id>`")

    targets = publisher.build_targets(settings, app.bot if app else None)
    tasks = [
        asyncio.create_task(rss.run(db, settings, stop), name="rss"),
        asyncio.create_task(processor.run(db, settings, llm, stop), name="processor"),
        asyncio.create_task(publisher.run(db, settings, targets, stop, notify), name="publisher"),
    ]
    if app:
        tasks.append(asyncio.create_task(approval_bot.run_draft_sender(app, db, settings, stop), name="drafts"))
    if settings.telegram_reader_enabled:
        tasks.append(asyncio.create_task(telegram_reader.run(db, settings, stop), name="tg_reader"))

    log.info("newsbot running (dry_run=%s, auto_post=%s). Ctrl+C to stop.", settings.dry_run, settings.auto_post)
    try:
        await stop.wait()
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if app:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        log.info("stopped")
