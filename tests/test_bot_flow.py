"""Drives the approval bot through python-telegram-bot with a fake Telegram HTTP layer."""
import json

import pytest
from telegram import Update
from telegram.request import BaseRequest

from newsbot import approval_bot

ADMIN = 4242
CHAT = {"id": ADMIN, "type": "private", "first_name": "Admin"}
USER = {"id": ADMIN, "is_bot": False, "first_name": "Admin"}


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls = []
        self.msg_id = 100

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @property
    def read_timeout(self):
        return 1

    async def do_request(self, url, method, request_data=None, **kw):
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Bot", "username": "test_bot"}
        elif name in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            self.msg_id += 1
            result = {"message_id": self.msg_id, "date": 0, "chat": CHAT, "text": params.get("text", "")}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def sent(self, name):
        return [p for n, p in self.calls if n == name]


@pytest.fixture
async def bot_env(settings, db):
    settings.telegram_bot_token = "123456:TEST"
    settings.admin_chat_id = str(ADMIN)
    settings.admin_user_ids = {ADMIN}
    fake = FakeTelegram()
    app = approval_bot.build_app(settings, db, request=fake)
    await app.initialize()
    yield app, fake
    await app.shutdown()


def _draft(db, headline="JUST IN: 🇪🇪 Draft text"):
    cid = db.add_item({"source": "ERR News", "source_type": "rss", "trust": "high", "title": headline,
                       "hash": headline, "norm": "a b c"}, None)[1]
    return db.create_post(cid, headline, "ERR News", "https://err.ee/1", "pending_approval", 8, "high", "war_security")


def _callback(app, data, user=USER, message_id=101):
    return Update.de_json({"update_id": 1, "callback_query": {
        "id": "cb1", "from": user, "chat_instance": "x", "data": data,
        "message": {"message_id": message_id, "date": 0, "chat": CHAT, "text": "📝 Draft"}}}, app.bot)


def _text(app, text):
    msg = {"message_id": 500, "date": 0, "chat": CHAT, "from": USER, "text": text}
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.de_json({"update_id": 2, "message": msg}, app.bot)


async def test_draft_sent_then_approved(bot_env, db, settings):
    app, fake = bot_env
    pid = _draft(db)
    await app.bot_data["send_draft"](app.bot, db.get_post(pid))
    sent = fake.sent("sendMessage")[0]
    assert "Draft #" in sent["text"] and "ap:" in json.dumps(sent["reply_markup"])
    assert db.get_post(pid)["admin_message_id"] == 101

    await app.process_update(_callback(app, f"ap:{pid}"))
    assert db.get_post(pid)["status"] == "approved"
    assert "Approved" in fake.sent("editMessageText")[0]["text"]


async def test_non_admin_cannot_approve(bot_env, db):
    app, fake = bot_env
    pid = _draft(db)
    await app.process_update(_callback(app, f"ap:{pid}", user={"id": 999, "is_bot": False, "first_name": "X"}))
    assert db.get_post(pid)["status"] == "pending_approval"
    assert fake.sent("answerCallbackQuery")[0]["text"] == "Not allowed"


async def test_edit_flow(bot_env, db):
    app, fake = bot_env
    pid = _draft(db)
    await app.process_update(_callback(app, f"ed:{pid}"))
    assert "Send the new text" in fake.sent("sendMessage")[0]["text"]
    await app.process_update(_text(app, "BREAKING: 🇪🇪 Edited headline"))
    post = db.get_post(pid)
    assert post["status"] == "approved" and post["headline"] == "BREAKING: 🇪🇪 Edited headline"
    assert "Edited" in fake.sent("sendMessage")[-1]["text"]


async def test_reject_and_start(bot_env, db):
    app, fake = bot_env
    pid = _draft(db)
    await app.process_update(_callback(app, f"rj:{pid}"))
    assert db.get_post(pid)["status"] == "rejected"
    await app.process_update(_text(app, "/start"))
    assert "Your chat id: 4242" in fake.sent("sendMessage")[-1]["text"]
