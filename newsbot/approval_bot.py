"""Telegram bot for the admin: sends drafts with Publish / Edit / Reject buttons, and video replies
(see video.py) with Publish / Reject buttons.

Also keeps a control panel in the admin chat with Start / Stop buttons for the news pipeline. The
panel's "last check" time is refreshed every minute, so a time that stops moving means the server is down.

Commands in the private chat with the bot:
  /start    show your chat id (put it in ADMIN_CHAT_ID)
  /panel    control panel: start / stop the bot, server status
  /stats    counts of stories and posts
  /pending  re-send drafts still waiting for a decision
  /cancel   stop editing
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta

from . import media
from .publisher import headline_html, x_intent_url, x_length
from .config import Settings
from .db import DB, now

log = logging.getLogger(__name__)

ACTIONS = {"ap": "approve", "rj": "reject", "ed": "edit"}
PANEL_REFRESH_SEC = 60
VIDEO_ACTIONS = {"va": "approve", "vr": "reject"}


# ---------- pure logic (tested without Telegram) ----------
def apply_decision(db: DB, post_id: int, action: str, new_text: str | None = None) -> tuple[bool, str]:
    post = db.get_post(post_id)
    if post is None:
        return False, "Draft not found."
    if post["status"] != "pending_approval":
        return False, f"Already {post['status']}."
    if action == "approve":
        db.update_post(post_id, status="approved")
        return True, "✅ Approved, queued for publishing."
    if action == "reject":
        db.update_post(post_id, status="rejected")
        return True, "❌ Rejected."
    if action == "edit":
        text = (new_text or "").strip()
        if not text:
            return False, "Empty text, nothing changed."
        if len(text) > 1000:
            return False, "Too long (max 1000 characters)."
        db.update_post(post_id, headline=text, status="approved")
        return True, "✏️ Edited and queued for publishing."
    return False, "Unknown action."


def apply_video_decision(db: DB, reply_id: int, action: str) -> tuple[bool, str]:
    reply = db.get_video_reply(reply_id)
    if reply is None:
        return False, "Video not found."
    if reply["status"] != "pending_approval":
        return False, f"Already {reply['status']}."
    if action == "approve":
        db.update_video_reply(reply_id, status="approved")
        return True, "✅ Approved, will be posted as a reply."
    if action == "reject":
        db.update_video_reply(reply_id, status="rejected")
        return True, "❌ Rejected."
    return False, "Unknown action."


def video_draft_text(db: DB, reply) -> str:
    post, video = db.get_post(reply["post_id"]), db.get_video(reply["video_id"])
    return "\n".join([
        f"🎬 <b>Video for post #{post['id']}</b> · {html.escape(video['source'])}",
        "",
        headline_html(post["headline"]),
        "",
        f"Moment: {html.escape(post['video_moment'] or '')}",
        f'Video: <a href="{html.escape(video["url"], quote=True)}">{html.escape(video["title"])}</a>',
        "",
        "Publish posts it as a reply under the channel post.",
    ])


def clock(dt: datetime | None = None) -> str:
    """Server local time, e.g. "14 Oct 22:07"."""
    return (dt or now()).astimezone().strftime("%d %b %H:%M")


def panel_text(db: DB, running: bool, online_since: datetime) -> str:
    posts = db.stats()["posts"]
    bot = "▶️ running" if running else "⏸ stopped, nothing is fetched or posted"
    return "\n".join([
        "🤖 <b>newsbot control</b>",
        "",
        f"Server: 🟢 online since {clock(online_since)}",
        f"Bot: {bot}",
        f"Queue: {posts.get('approved', 0)} approved · {posts.get('pending_approval', 0)} drafts waiting",
        f"Published in the last hour: {db.published_since(now() - timedelta(hours=1))}",
        "",
        f"<i>Last check: {clock()}. If this time stops updating, the server is offline "
        "and the buttons won't respond.</i>",
    ])


def offline_text() -> str:
    return "\n".join([
        "🤖 <b>newsbot control</b>",
        "",
        f"Server: 🔴 offline since {clock()}",
        "",
        "The bot can't be started from Telegram until the server runs again "
        "(<code>python -m newsbot run</code> on the computer).",
    ])


X_MAX = 280
COPY_MAX = 256  # Telegram's limit for a copy-to-clipboard button


def x_handoff_text(post) -> str:
    lines = ["📤 <b>Ready for X</b>", "", html.escape(post["headline"])]
    over = x_length(post["headline"]) - X_MAX
    if over > 0:
        lines += ["", f"⚠️ {over} characters over X's {X_MAX} limit, shorten it before posting."]
    return "\n".join(lines)


def draft_text(db: DB, post) -> str:
    sources = db.cluster_sources(post["cluster_id"])
    risk = "⚠️ HIGH" if post["risk"] == "high" else "low"
    lines = [
        f"📝 <b>Draft #{post['id']}</b> · score {post['importance']}/10 · risk {risk} · {html.escape(post['category'] or '')}",
        f"Sources ({len(sources)}): {html.escape(', '.join(sources))}",
        "",
        headline_html(post["headline"]),
    ]
    images = media.media_list(post)
    if images:
        links = ", ".join(f'<a href="{html.escape(m["url"], quote=True)}">{html.escape(m["name"])}</a>'
                          for m in images)
        lines += ["", f"🖼 Images: {links}"]
    if post["source_url"]:
        lines += ["", f'<a href="{html.escape(post["source_url"], quote=True)}">Open original</a>']
    return "\n".join(lines)


# ---------- Telegram wiring ----------
def build_app(settings: Settings, db: DB, request=None):
    from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
    from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                              MessageHandler, filters)

    no_preview = LinkPreviewOptions(is_disabled=True)

    def is_admin(update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id in settings.admin_user_ids)

    def keyboard(post_id: int):
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Publish", callback_data=f"ap:{post_id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"ed:{post_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"rj:{post_id}"),
        ]])

    def panel_keyboard(running: bool):
        toggle = (InlineKeyboardButton("⏸ Stop bot", callback_data="ctl:stop") if running
                  else InlineKeyboardButton("▶️ Start bot", callback_data="ctl:start"))
        return InlineKeyboardMarkup([[toggle, InlineKeyboardButton("🔄 Refresh", callback_data="ctl:refresh")]])

    def render_panel():
        pipeline = app.bot_data.get("pipeline")
        running = bool(pipeline and pipeline.running)
        since = app.bot_data.setdefault("online_since", now())
        return panel_text(db, running, since), panel_keyboard(running)

    async def send_panel(bot) -> None:
        text, markup = render_panel()
        msg = await bot.send_message(settings.admin_chat_id, text, parse_mode="HTML", reply_markup=markup)
        db.set_state("panel_message_id", str(msg.message_id))

    async def refresh_panel(bot, message_id: int | None = None) -> None:
        """Re-draw the panel in place (the latest one unless message_id is given)."""
        message_id = message_id or int(db.get_state("panel_message_id") or 0)
        if not message_id:
            return
        text, markup = render_panel()
        try:
            await bot.edit_message_text(text, settings.admin_chat_id, message_id, parse_mode="HTML",
                                        reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            if "not modified" not in str(e).lower():  # same text within the same minute is fine
                raise

    async def send_x_handoff(bot, post) -> None:
        """After a post goes out on Telegram: the same text with buttons to post it on X by hand."""
        buttons = [InlineKeyboardButton("𝕏 Post on X", url=x_intent_url(post["headline"]))]
        if len(post["headline"]) <= COPY_MAX:  # longer text: long-press the message to copy instead
            buttons.append(InlineKeyboardButton("📋 Copy text", copy_text=CopyTextButton(post["headline"])))
        await bot.send_message(settings.admin_chat_id, x_handoff_text(post), parse_mode="HTML",
                               reply_markup=InlineKeyboardMarkup([buttons]), link_preview_options=no_preview)

    async def send_draft(bot, post) -> None:
        msg = await bot.send_message(settings.admin_chat_id, draft_text(db, post), parse_mode="HTML",
                                     reply_markup=keyboard(post["id"]), link_preview_options=no_preview)
        db.update_post(post["id"], admin_message_id=msg.message_id)

    async def send_video_draft(bot, reply) -> None:
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Publish", callback_data=f"va:{reply['id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"vr:{reply['id']}"),
        ]])
        video = db.get_video(reply["video_id"])
        msg = await bot.send_message(settings.admin_chat_id, video_draft_text(db, reply), parse_mode="HTML",
                                     reply_markup=buttons,  # the preview lets you watch it before deciding
                                     link_preview_options=LinkPreviewOptions(url=video["url"], prefer_large_media=True))
        db.update_video_reply(reply["id"], admin_message_id=msg.message_id)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        await update.message.reply_text(
            f"Your chat id: {update.effective_chat.id}\nYour user id: {u.id}\n"
            + ("You are an admin." if is_admin(update) else "Not an admin: put this id in ADMIN_CHAT_ID in .env"))
        if is_admin(update):
            await send_panel(context.bot)

    async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if is_admin(update):
            await send_panel(context.bot)

    async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if is_admin(update):
            s = db.stats()
            await update.message.reply_text(
                f"Items: {s['items']}\nStories: {s['clusters']}\nPosts: {s['posts']}\nVideo replies: {s['video_replies']}")

    async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_admin(update):
            return
        posts = db.posts_by_status("pending_approval", 20)
        videos = db.video_replies_by_status("pending_approval")
        if not posts and not videos:
            await update.message.reply_text("No drafts waiting.")
        for p in posts:
            await send_draft(context.bot, p)
        for r in videos:
            await send_video_draft(context.bot, r)

    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data.pop("editing", None)
        await update.message.reply_text("Cancelled.")

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not is_admin(update):
            await q.answer("Not allowed", show_alert=True)
            return
        code, _, raw_id = (q.data or "").partition(":")
        action, post_id = ACTIONS.get(code), int(raw_id or 0)
        if action == "edit":
            post = db.get_post(post_id)
            if not post or post["status"] != "pending_approval":
                await q.answer("Draft is no longer pending", show_alert=True)
                return
            context.user_data["editing"] = post_id
            await q.answer()
            await q.message.reply_text(
                f"Send the new text for draft #{post_id} (/cancel to stop). Current text:")
            await q.message.reply_text(post["headline"])
            return
        ok, result = apply_decision(db, post_id, action or "")
        await q.answer(result)
        if ok:
            await q.edit_message_text(q.message.text_html + f"\n\n<b>{result}</b>", parse_mode="HTML",
                                      link_preview_options=no_preview)

    async def on_video_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not is_admin(update):
            await q.answer("Not allowed", show_alert=True)
            return
        code, _, raw_id = (q.data or "").partition(":")
        ok, result = apply_video_decision(db, int(raw_id or 0), VIDEO_ACTIONS.get(code, ""))
        await q.answer(result)
        if ok:
            await q.edit_message_text(q.message.text_html + f"\n\n<b>{result}</b>", parse_mode="HTML")

    async def on_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if not is_admin(update):
            await q.answer("Not allowed", show_alert=True)
            return
        action = (q.data or "").partition(":")[2]
        pipeline = app.bot_data.get("pipeline")
        if action in ("start", "stop") and pipeline is None:
            await q.answer("The pipeline isn't available in this process", show_alert=True)
            return
        result = None
        if action == "start":
            await q.answer("Starting…")
            result = "▶️ Bot started." if await pipeline.start() else "Already running."
        elif action == "stop":
            await q.answer("Stopping…")  # can take a few seconds if a post is being sent
            result = ("⏸ Bot stopped. Nothing will be fetched or posted until you press Start."
                      if await pipeline.stop() else "Already stopped.")
        else:
            await q.answer("Updated")
        db.set_state("panel_message_id", str(q.message.message_id))
        await refresh_panel(context.bot, q.message.message_id)
        if result:
            await context.bot.send_message(settings.admin_chat_id, result)

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_admin(update):
            return
        post_id = context.user_data.get("editing")
        if not post_id:
            await update.message.reply_text(
                "Use the buttons under a draft. /pending shows waiting drafts, /panel starts or stops the bot.")
            return
        ok, result = apply_decision(db, post_id, "edit", update.message.text)
        if ok:
            context.user_data.pop("editing", None)
            post = db.get_post(post_id)
            if post["admin_message_id"]:
                try:
                    await context.bot.edit_message_reply_markup(settings.admin_chat_id, post["admin_message_id"])
                except Exception:  # noqa: BLE001  old message may be gone
                    pass
        await update.message.reply_text(result)

    builder = Application.builder().token(settings.telegram_bot_token)
    if request is not None:  # tests inject a fake HTTP layer
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(ap|rj|ed):\d+$"))
    app.add_handler(CallbackQueryHandler(on_video_button, pattern=r"^(va|vr):\d+$"))
    app.add_handler(CallbackQueryHandler(on_control, pattern=r"^ctl:(start|stop|refresh)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.bot_data["send_draft"] = send_draft
    app.bot_data["send_video_draft"] = send_video_draft
    app.bot_data["send_panel"] = send_panel
    app.bot_data["send_x_handoff"] = send_x_handoff
    app.bot_data["refresh_panel"] = refresh_panel
    return app


async def run_draft_sender(app, db: DB, settings: Settings, stop: asyncio.Event, interval: float = 3) -> None:
    """Pushes new drafts and video replies into the admin chat."""
    send_draft, send_video_draft = app.bot_data["send_draft"], app.bot_data["send_video_draft"]
    while not stop.is_set():
        for post in db.unsent_drafts():
            try:
                await send_draft(app.bot, post)
            except Exception:  # noqa: BLE001
                log.exception("could not send draft #%s to admin chat", post["id"])
                break
        for reply in db.unsent_video_drafts():
            try:
                await send_video_draft(app.bot, reply)
            except Exception:  # noqa: BLE001
                log.exception("could not send video reply #%s to admin chat", reply["id"])
                break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def run_panel(app, db: DB, settings: Settings, stop: asyncio.Event,
                    interval: float = PANEL_REFRESH_SEC) -> None:
    """Posts a fresh control panel when the server comes online, then keeps its "last check" time current."""
    from telegram import BotCommand
    try:
        await app.bot.set_my_commands([
            BotCommand("panel", "Start / stop the bot, server status"),
            BotCommand("pending", "Re-send drafts waiting for a decision"),
            BotCommand("stats", "Counts of stories and posts"),
        ])
        await app.bot_data["send_panel"](app.bot)
    except Exception:  # noqa: BLE001
        log.exception("could not send the control panel")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            await app.bot_data["refresh_panel"](app.bot)
        except Exception as e:  # noqa: BLE001  panel deleted, network blip: try again next time
            log.debug("panel refresh failed: %s", e)


async def announce_offline(app, db: DB, settings: Settings) -> None:
    """On shutdown: mark the panel offline (removing its buttons) and send a notification."""
    message_id = int(db.get_state("panel_message_id") or 0)
    try:
        if message_id:
            await app.bot.edit_message_text(offline_text(), settings.admin_chat_id, message_id, parse_mode="HTML")
        await app.bot.send_message(settings.admin_chat_id,
                                   f"🔴 newsbot server shut down at {clock()}. Start/Stop won't work until "
                                   "it runs again on the computer.")
    except Exception:  # noqa: BLE001
        log.exception("could not announce shutdown in the admin chat")
