"""Telegram bot for the admin: sends drafts with Publish / Edit / Reject buttons.

Commands in the private chat with the bot:
  /start    show your chat id (put it in ADMIN_CHAT_ID)
  /stats    counts of stories and posts
  /pending  re-send drafts still waiting for a decision
  /cancel   stop editing
"""
from __future__ import annotations

import asyncio
import html
import logging

from . import media
from .publisher import headline_html
from .config import Settings
from .db import DB

log = logging.getLogger(__name__)

ACTIONS = {"ap": "approve", "rj": "reject", "ed": "edit"}


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
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
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

    async def send_draft(bot, post) -> None:
        msg = await bot.send_message(settings.admin_chat_id, draft_text(db, post), parse_mode="HTML",
                                     reply_markup=keyboard(post["id"]), link_preview_options=no_preview)
        db.update_post(post["id"], admin_message_id=msg.message_id)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        u = update.effective_user
        await update.message.reply_text(
            f"Your chat id: {update.effective_chat.id}\nYour user id: {u.id}\n"
            + ("You are an admin." if is_admin(update) else "Not an admin: put this id in ADMIN_CHAT_ID in .env"))

    async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if is_admin(update):
            s = db.stats()
            await update.message.reply_text(
                f"Items: {s['items']}\nStories: {s['clusters']}\nPosts: {s['posts']}")

    async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_admin(update):
            return
        posts = db.posts_by_status("pending_approval", 20)
        if not posts:
            await update.message.reply_text("No drafts waiting.")
        for p in posts:
            await send_draft(context.bot, p)

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

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_admin(update):
            return
        post_id = context.user_data.get("editing")
        if not post_id:
            await update.message.reply_text("Use the buttons under a draft. /pending shows waiting drafts.")
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
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(ap|rj|ed):\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.bot_data["send_draft"] = send_draft
    return app


async def run_draft_sender(app, db: DB, settings: Settings, stop: asyncio.Event, interval: float = 3) -> None:
    """Pushes new drafts into the admin chat."""
    send_draft = app.bot_data["send_draft"]
    while not stop.is_set():
        for post in db.unsent_drafts():
            try:
                await send_draft(app.bot, post)
            except Exception:  # noqa: BLE001
                log.exception("could not send draft #%s to admin chat", post["id"])
                break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
