"""Runs every component in one process: RSS poller, Telegram reader, processor, approval bot, publisher,
video replies."""
from __future__ import annotations

import asyncio
import logging
import signal

from . import approval_bot, processor, publisher, video
from .config import Settings
from .db import DB
from .ingest import rss, telegram_reader
from .llm import llm_from_settings
from .publisher import Notify, OnPublished

log = logging.getLogger(__name__)
SHUTDOWN_GRACE_SEC = 10


async def stop_tasks(tasks: list[asyncio.Task], grace: float = SHUTDOWN_GRACE_SEC) -> None:
    """Give tasks `grace` seconds to finish what they are doing (e.g. a post being sent), then cancel.
    A cancelled story stays queued and is scored again on the next start."""
    _, pending = await asyncio.wait(tasks, timeout=grace)
    for t in pending:
        log.info("%s still busy after %ss, cancelling", t.get_name(), grace)
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class Pipeline:
    """The news workers (RSS, Telegram reader, processor, publisher, video replies), which the admin can
    stop and start from Telegram while the process keeps running. Stopped survives a restart."""

    def __init__(self, db: DB, settings: Settings, llm, targets: list, bot=None, notify: Notify | None = None,
                 on_published: OnPublished | None = None):
        self.db, self.settings, self.llm, self.targets, self.bot, self.notify = db, settings, llm, targets, bot, notify
        self.on_published = on_published
        self.tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return bool(self.tasks)

    def _create_tasks(self) -> list[asyncio.Task]:
        s, db, stop = self.settings, self.db, self._stop
        tasks = [
            asyncio.create_task(rss.run(db, s, stop), name="rss"),
            asyncio.create_task(processor.run(db, s, self.llm, stop), name="processor"),
            asyncio.create_task(publisher.run(db, s, self.targets, stop, self.notify, on_published=self.on_published), name="publisher"),
        ]
        if s.video_enabled:
            tasks.append(asyncio.create_task(video.run(db, s, self.llm, self.bot, stop), name="video"))
        if s.telegram_reader_enabled:
            tasks.append(asyncio.create_task(telegram_reader.run(db, s, stop), name="tg_reader"))
        return tasks

    async def start(self) -> bool:
        """Start the workers. Returns False if they were already running."""
        async with self._lock:
            self.db.set_state("paused", "0")
            if self.tasks:
                return False
            self._stop = asyncio.Event()
            self.tasks = self._create_tasks()
            log.info("pipeline started")
            return True

    async def stop(self, remember: bool = True) -> bool:
        """Stop the workers (a post being sent gets a few seconds to finish). Returns False if already
        stopped. With remember=False (process shutdown) the next start runs the pipeline again."""
        async with self._lock:
            if remember:
                self.db.set_state("paused", "1")
            if not self.tasks:
                return False
            self._stop.set()
            await stop_tasks(self.tasks)
            self.tasks = []
            log.info("pipeline stopped")
            return True


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
    on_published = None
    if app and settings.x_handoff:  # "Ready for X" message with Post on X / Copy text buttons
        async def on_published(post) -> None:
            await app.bot_data["send_x_handoff"](app.bot, post)
    pipeline = Pipeline(db, settings, llm, targets, app.bot if app else None, notify, on_published)
    tasks = []
    if app:
        app.bot_data["pipeline"] = pipeline
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

    if db.get_state("paused") == "1":
        log.info("pipeline was stopped from Telegram, not starting it (press Start in the control panel)")
    else:
        await pipeline.start()
    if app:  # after the pipeline, so the first panel shows whether it runs
        tasks.append(asyncio.create_task(approval_bot.run_draft_sender(app, db, settings, stop), name="drafts"))
        tasks.append(asyncio.create_task(approval_bot.run_panel(app, db, settings, stop), name="panel"))

    log.info("newsbot running (dry_run=%s, auto_post=%s). Ctrl+C to stop.", settings.dry_run, settings.auto_post)
    try:
        await stop.wait()
    finally:
        log.info("stopping...")
        stop.set()
        await pipeline.stop(remember=False)
        await stop_tasks(tasks)
        if app:
            await approval_bot.announce_offline(app, db, settings)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        log.info("stopped")
