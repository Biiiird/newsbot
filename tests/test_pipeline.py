from types import SimpleNamespace

import pytest

from newsbot import approval_bot, publisher
from newsbot.config import load_settings
from newsbot.db import now
from newsbot.ingest import store_item
from newsbot.llm import Assessment, ClaudeLLM, MockLLM, build_user_message
from newsbot.processor import process_once, route


def A(**kw):
    base = dict(is_news=True, importance=8, risk="low", category="politics_diplomacy",
                headline="JUST IN: 🇪🇪 Something happened", reason="r")
    base.update(kw)
    return Assessment(**base)


# ---------- routing ----------
def test_route(settings):
    assert route(A(importance=3), "high", settings) == "dropped"
    assert route(A(is_news=False), "high", settings) == "dropped"
    assert route(A(headline=""), "high", settings) == "dropped"
    assert route(A(), "high", settings) == "pending_approval"  # AUTO_POST off by default
    settings.auto_post = True
    assert route(A(), "high", settings) == "approved"
    assert route(A(risk="high"), "high", settings) == "pending_approval"
    assert route(A(), "medium", settings) == "pending_approval"
    assert route(A(importance=7), "high", settings) == "pending_approval"
    settings.auto_require_low_risk = settings.auto_require_high_trust = False
    assert route(A(risk="high"), "low", settings) == "approved"
    assert route(A(importance=7), "low", settings) == "pending_approval"


# ---------- processor ----------
class FakeLLM:
    def __init__(self, results):
        self.results, self.seen = list(results), []

    async def assess(self, item):
        self.seen.append(item)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.mark.asyncio
async def test_process_once(db, settings, high, medium):
    settings.auto_post = True
    t = now()
    store_item(db, settings, high, "Estonian government approves record defence budget for next year", url="u1", published_at=t)
    store_item(db, settings, medium, "Russian drone strike reported near Kharkiv overnight, officials say", url="u2", published_at=t)
    store_item(db, settings, high, "Tallinn zoo welcomes baby red panda to its family", url="u3", published_at=t)
    store_item(db, settings, high, "Something the model will fail on for sure today", url="u4", published_at=t)

    llm = FakeLLM([A(importance=9), A(importance=8, risk="high"), A(importance=2), RuntimeError("boom")])
    counts = await process_once(db, settings, llm)
    assert counts == {"approved": 1, "pending_approval": 1, "dropped": 1, "error": 1}
    assert db.new_clusters() == []
    assert llm.seen[0]["trust"] == "high" and llm.seen[0]["item_count"] == 1
    assert len(db.posts_by_status("approved")) == 1
    assert db.stats()["clusters"] == {"scored": 2, "dropped": 1, "error": 1}


@pytest.mark.asyncio
async def test_mock_llm():
    a = await MockLLM().assess({"title": "NATO says missile strike hit border area", "trust": "high"})
    assert a.importance >= 7 and a.risk == "high" and a.headline.startswith("JUST IN:")


@pytest.mark.asyncio
async def test_processor_drops_same_event_worded_differently(db, settings, high, medium):
    t = now()
    store_item(db, settings, high, "Russian drone strike sets Academy of Sciences building on fire in Kyiv",
               url="u1", published_at=t)
    store_item(db, settings, medium, "People escape historic Kyiv building hit by Russian drone",
               url="u2", published_at=t)
    store_item(db, settings, medium, "Poland to ease rules for shooting down drones entering from Russia",
               url="u3", published_at=t)
    llm = FakeLLM([A(headline="first"), A(headline="again", duplicate_of=1), A(headline="other", duplicate_of=99)])
    counts = await process_once(db, settings, llm)
    assert counts == {"pending_approval": 2, "duplicate": 1}
    assert llm.seen[0]["recent_posts"] == []
    assert llm.seen[1]["recent_posts"] == [{"id": 1, "headline": "first"}]
    assert [p["headline"] for p in db.posts_by_status("pending_approval")] == ["first", "other"]  # 99: unknown id
    assert "duplicate of post #1" in db.one("SELECT reason FROM clusters WHERE status = 'dropped'")["reason"]


def test_tidy_headline():
    from newsbot.llm import tidy_headline
    assert tidy_headline("🇫🇷 Le Pen hails ‘milestone’; RN gains seats") == '🇫🇷 Le Pen hails "milestone". RN gains seats'
    assert tidy_headline("Iran's FM calls talks 'dead' and “over”") == 'Iran\'s FM calls talks "dead" and "over"'
    assert tidy_headline("Ukrainians' morale; strikes continue;") == "Ukrainians' morale. Strikes continue."
    assert A(headline="x").headline == "x"  # direct construction is untouched
    from newsbot.llm import Assessment
    assert Assessment.from_dict({"headline": " a; b "}).headline == "a. B"


def test_headline_html_bolds_prefix():
    assert publisher.headline_html("JUST IN: 🇺🇦 A & B") == "<b>JUST IN:</b> 🇺🇦 A &amp; B"
    assert publisher.headline_html("BREAKING: x") == "<b>BREAKING:</b> x"
    assert publisher.headline_html("🇺🇦 JUST IN: <x>") == "🇺🇦 JUST IN: &lt;x&gt;"


def test_user_message_mentions_state_media():
    msg = build_user_message({"source": "RIA", "trust": "low", "state_media": 1, "lang": "ru",
                              "title": "Заголовок", "body": "Текст новости", "item_count": 2})
    assert "STATE MEDIA" in msg and "2 source(s)" in msg and "Текст новости" in msg
    assert "already made" not in msg
    msg = build_user_message({"source": "BBC", "trust": "high", "title": "t",
                              "recent_posts": [{"id": 7, "headline": "🇺🇦 Drone hits Kyiv"}]})
    assert "#7: 🇺🇦 Drone hits Kyiv" in msg


@pytest.mark.asyncio
async def test_claude_llm_parses_tool_call():
    llm = ClaudeLLM.__new__(ClaudeLLM)
    llm.model = "claude-haiku-4-5-20251001"
    captured = {}

    async def create(**kw):
        captured.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={
            "is_news": True, "importance": 12, "risk": "weird", "category": "energy",
            "headline": "  JUST IN: 🇱🇻 Latvia ... ", "reason": "why", "duplicate_of": "3"})])

    llm.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    a = await llm.assess({"source": "LSM", "trust": "high", "title": "t", "body": ""})
    assert a.importance == 10 and a.risk == "low" and a.headline == "JUST IN: 🇱🇻 Latvia ..."
    assert a.duplicate_of == 3
    assert captured["tool_choice"] == {"type": "tool", "name": "submit_assessment"}


# ---------- approval ----------
def _post(db, status="pending_approval", importance=7, headline="JUST IN: test <b>&"):
    cid = db.add_item({"source": "ERR News", "source_type": "rss", "trust": "high", "title": headline,
                       "hash": f"h{importance}{status}{headline}", "norm": "x y z"}, None)[1]
    return db.create_post(cid, headline, "ERR News", "https://err.ee/1?a=1&b=2", status, importance, "low", "other")


def test_apply_decision(db):
    p1, p2, p3 = _post(db, headline="one"), _post(db, headline="two"), _post(db, headline="three")
    assert approval_bot.apply_decision(db, p1, "approve")[0]
    assert db.get_post(p1)["status"] == "approved"
    assert approval_bot.apply_decision(db, p1, "reject") == (False, "Already approved.")
    assert approval_bot.apply_decision(db, p2, "reject")[0]
    assert approval_bot.apply_decision(db, p3, "edit", "   ")[0] is False
    ok, _ = approval_bot.apply_decision(db, p3, "edit", "JUST IN: 🇪🇪 fixed text")
    assert ok and db.get_post(p3)["headline"] == "JUST IN: 🇪🇪 fixed text"
    assert approval_bot.apply_decision(db, 999, "approve")[0] is False


def test_draft_text_escapes_html(db):
    p = db.get_post(_post(db))
    text = approval_bot.draft_text(db, p)
    assert "&lt;b&gt;&amp;" in text and "Draft #" in text and "&amp;b=2" in text


def test_build_app_registers_handlers(settings, db):
    settings.telegram_bot_token = "123456:TEST-token"
    settings.admin_chat_id = "42"
    app = approval_bot.build_app(settings, db)
    assert sum(len(h) for h in app.handlers.values()) == 7


# ---------- publisher ----------
class Recorder:
    def __init__(self, name="rec", fail=False):
        self.name, self.fail, self.posts = name, fail, []

    async def publish(self, post):
        if self.fail:
            raise RuntimeError("api down")
        self.posts.append(post["headline"])
        return "ext-1"


@pytest.mark.asyncio
async def test_publisher_order_and_rate_limit(db, settings):
    low, high = _post(db, "approved", 7, "low one"), _post(db, "approved", 9, "high one")
    rec = Recorder()
    assert await publisher.publish_next(db, settings, [rec])
    assert rec.posts == ["high one"]  # most important first
    settings.min_post_interval_sec = 600
    assert not await publisher.publish_next(db, settings, [rec])  # too soon
    assert publisher.wait_seconds(db, settings) > 500
    settings.min_post_interval_sec = 0
    settings.max_posts_per_hour = 1
    assert not await publisher.publish_next(db, settings, [rec])  # hourly cap
    settings.max_posts_per_hour = 10
    assert await publisher.publish_next(db, settings, [rec])
    assert db.get_post(low)["status"] == "published" and db.get_post(high)["published_at"]


@pytest.mark.asyncio
async def test_publisher_failures(db, settings):
    p = _post(db, "approved", 8, "x")
    notes = []

    async def notify(t):
        notes.append(t)

    assert not await publisher.publish_next(db, settings, [Recorder(fail=True)], notify)
    assert db.get_post(p)["status"] == "failed" and "api down" in notes[0]

    p2 = _post(db, "approved", 8, "y")
    assert await publisher.publish_next(db, settings, [Recorder(), Recorder("x", fail=True)], notify)
    assert db.get_post(p2)["status"] == "published" and "some targets failed" in notes[1]
    assert len(db.q("SELECT * FROM publish_log")) == 3


def test_telegram_text_and_targets(db, settings):
    p = db.get_post(_post(db))
    t = publisher.telegram_text(p, True)
    assert t.startswith("<b>JUST IN:</b> test &lt;b&gt;&amp;") and 'href="https://err.ee/1?a=1&amp;b=2"' in t
    assert "Source" not in publisher.telegram_text(p, False)
    assert [x.name for x in publisher.build_targets(settings)] == ["dry_run"]
    settings.dry_run = False
    settings.telegram_channel_id = "@chan"
    assert [x.name for x in publisher.build_targets(settings, bot=object())] == ["telegram"]


def test_load_settings(tmp_path, monkeypatch):
    for k in ("ADMIN_CHAT_ID", "ADMIN_USER_IDS", "DRY_RUN", "AUTO_POST", "SOURCES_FILE"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "s.yaml").write_text(
        "rss:\n  - {name: A, url: 'https://a/rss', trust: high}\n"
        "telegram:\n  - {handle: '@b', trust: low, state_media: true}\n")
    env = tmp_path / ".env"
    env.write_text(f"SOURCES_FILE={tmp_path / 's.yaml'}\nADMIN_CHAT_ID=12345\nDRY_RUN=false\nAUTO_POST=yes\n")
    s = load_settings(str(env))
    assert s.admin_user_ids == {12345} and s.dry_run is False and s.auto_post is True
    assert [x.name for x in s.rss_sources] == ["A"]
    assert s.telegram_sources[0].name == "@b" and s.telegram_sources[0].state_media
    assert not s.telegram_reader_enabled
