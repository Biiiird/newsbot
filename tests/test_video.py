from datetime import timedelta
from types import SimpleNamespace

import pytest

from newsbot import approval_bot, video
from newsbot.config import Source, load_sources
from newsbot.db import now
from newsbot.ingest import store_item
from newsbot.llm import Assessment, FallbackLLM, LimitReached, build_match_message, match_index
from newsbot.processor import process_once

WH = Source(name="The White House", kind="video", url="https://yt/wh", trust="high")
KREMLIN = Source(name="Kremlin", kind="video", url="https://kremlin/video", trust="high")


@pytest.fixture
def vsettings(settings):
    settings.sources += [WH, KREMLIN]
    return settings


def _published_post(db, moment="Vladimir Putin speech at the Valdai Discussion Club", headline=None, tg_id="55"):
    headline = headline or "JUST IN: 🇷🇺 Putin says Russia is open to talks."
    cid = db.add_item({"source": "BBC World", "source_type": "rss", "trust": "high", "title": headline,
                       "hash": headline, "norm": headline.lower()}, None)[1]
    pid = db.create_post(cid, headline, "BBC World", "https://bbc/1", "published", 8, "low",
                         "politics_diplomacy", video_moment=moment)
    if tg_id:
        db.log_publish(pid, "telegram", True, tg_id)
    return pid


def _video(db, title, description="", source="Kremlin", url=None, age=timedelta(hours=1)):
    url = url or f"https://v/{abs(hash(title))}"
    db.add_video(source, title, description, url, now() - age)
    return db.one("SELECT * FROM videos WHERE url = ?", (url,))


class FakeLLM:
    def __init__(self, pick=1, exc=None):
        self.pick, self.exc, self.calls = pick, exc, []

    async def match_video(self, post, videos):
        self.calls.append([v["title"] for v in videos])
        if self.exc:
            raise self.exc
        return self.pick


# ---------- config / scoring ----------
def test_video_sources_loaded(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("rss: []\nvideo:\n  - name: The White House\n    url: https://yt/wh\n")
    (src,) = load_sources(str(path))
    assert (src.kind, src.name, src.url) == ("video", "The White House", "https://yt/wh")


def test_video_enabled_needs_sources(settings, vsettings):
    assert vsettings.video_enabled
    vsettings.video_replies = False
    assert not vsettings.video_enabled


def test_assessment_video_moment():
    assert Assessment.from_dict({"headline": "x", "video_moment": "  Putin speech  "}).video_moment == "Putin speech"
    assert Assessment.from_dict({"headline": "x"}).video_moment == ""


async def test_processor_saves_video_moment(db, settings, high):
    store_item(db, settings, high, "Putin addresses the Valdai club in Sochi", url="u1", published_at=now())

    class LLM:
        async def assess(self, item):
            return Assessment(True, 8, "low", "politics_diplomacy", "JUST IN: 🇷🇺 Putin speaks", "r",
                              video_moment="Vladimir Putin speech at Valdai")

    await process_once(db, settings, LLM())
    assert db.posts_by_status("pending_approval")[0]["video_moment"] == "Vladimir Putin speech at Valdai"


def test_match_message_and_index():
    text = build_match_message({"headline": "H", "video_moment": "M"},
                               [{"source": "Kremlin", "title": "T1", "description": "D1", "published_at": "x"},
                                {"source": "WH", "title": "T2", "description": ""}])
    assert "Filmed moment: M" in text and "1. [Kremlin, uploaded x] T1\n   D1" in text and "2. [WH" in text
    assert match_index({"video": 2}, 2) == 2
    assert match_index({"video": 3}, 2) == 0  # out of range
    assert match_index({"video": "x"}, 2) == 0


async def test_fallback_llm_matches_with_api():
    class Primary:
        async def match_video(self, post, videos):
            raise LimitReached("limit")

    class Api:
        async def match_video(self, post, videos):
            return 1

    assert await FallbackLLM(Primary(), Api()).match_video({}, [{}]) == 1


# ---------- polling ----------
async def test_poll_stores_new_recent_uploads(db, vsettings, monkeypatch):
    feeds = {
        "https://yt/wh": [
            {"title": "President Trump Delivers Remarks", "body": "<p>Full remarks</p>", "url": "https://yt/1",
             "published_at": now() - timedelta(hours=1)},
            {"title": "Old video", "body": "", "url": "https://yt/old", "published_at": now() - timedelta(days=5)},
            {"title": "No date", "body": "", "url": "https://yt/nodate", "published_at": None},
        ],
    }

    async def fake_fetch(client, source):
        if source.url not in feeds:
            raise RuntimeError("feed down")
        return feeds[source.url]

    monkeypatch.setattr(video, "fetch_source", fake_fetch)
    assert await video.poll_once(db, vsettings, None) == 1
    assert await video.poll_once(db, vsettings, None) == 0  # already known
    (v,) = db.q("SELECT * FROM videos")
    assert (v["source"], v["title"], v["description"]) == ("The White House", "President Trump Delivers Remarks",
                                                           "Full remarks")


# ---------- matching ----------
def test_keywords_skip_generic_words():
    assert video.keywords("Vladimir Putin speech at the Valdai Discussion Club") == {
        "vladimir", "putin", "valdai", "discussion", "club"}
    assert not video.keywords("President delivers remarks") & video.keywords("Secretary remarks at briefing")


async def test_match_creates_reply_for_approval(db, vsettings):
    pid = _published_post(db)
    _video(db, "Meeting of Valdai Discussion Club", "Vladimir Putin addressed the plenary session")
    _video(db, "Minimum Sentences on Fraudsters", "", source="The White House")  # unrelated: not shown
    _video(db, "Putin at Valdai last year", age=timedelta(hours=20))  # before the window
    llm = FakeLLM(pick=1)

    assert await video.match_once(db, vsettings, llm) == {"matched": 1}
    assert llm.calls == [["Meeting of Valdai Discussion Club"]]
    (reply,) = db.video_replies_by_status("pending_approval")
    assert reply["post_id"] == pid and db.get_video(reply["video_id"])["title"] == "Meeting of Valdai Discussion Club"

    assert await video.match_once(db, vsettings, llm) == {}  # the post has its video
    assert len(llm.calls) == 1


async def test_no_match_is_not_asked_again_until_new_uploads(db, vsettings):
    _published_post(db)
    _video(db, "Putin meets the Governor of Chelyabinsk Region")
    llm = FakeLLM(pick=0)
    assert await video.match_once(db, vsettings, llm) == {"no_match": 1}
    assert await video.match_once(db, vsettings, llm) == {}  # same upload not judged twice
    _video(db, "Putin speaks at Valdai Discussion Club plenary session")
    llm.pick = 1
    assert await video.match_once(db, vsettings, llm) == {"matched": 1}
    assert llm.calls[-1] == ["Putin speaks at Valdai Discussion Club plenary session"]


async def test_auto_post_and_posts_without_moment_or_too_old(db, vsettings):
    vsettings.video_auto_post = True
    _published_post(db)
    _published_post(db, moment=None, headline="Missile strike on Kharkiv")
    old = _published_post(db, headline="Putin at Valdai, old post")
    db.exec("UPDATE posts SET created_at = ? WHERE id = ?",
            ((now() - timedelta(hours=7)).isoformat(timespec="seconds"), old))
    _video(db, "Valdai Discussion Club plenary session")
    llm = FakeLLM(pick=1)
    assert await video.match_once(db, vsettings, llm) == {"matched": 1}
    assert len(llm.calls) == 1
    assert len(db.video_replies_by_status("approved")) == 1


async def test_limit_keeps_videos_unchecked(db, vsettings):
    _published_post(db)
    _video(db, "Valdai Discussion Club plenary session")
    assert await video.match_once(db, vsettings, FakeLLM(exc=LimitReached("limit"))) == {"waiting_for_limit": 1}
    assert await video.match_once(db, vsettings, FakeLLM(pick=1)) == {"matched": 1}


# ---------- publishing ----------
class FakeBot:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc

    async def send_message(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return SimpleNamespace(message_id=77)


def _approved_reply(db, tg_id="55"):
    n = db.one("SELECT COUNT(*) n FROM posts")["n"]
    pid = _published_post(db, headline=f"Putin speaks at Valdai ({n})", tg_id=tg_id)
    v = _video(db, "Meeting of Valdai <Discussion> Club", url=f"https://kremlin/8091{n}")
    return db.create_video_reply(pid, v["id"], "approved")


async def test_publish_reply_under_channel_post(db, vsettings):
    vsettings.dry_run, vsettings.telegram_channel_id = False, "@chan"
    rid = _approved_reply(db)
    bot = FakeBot()
    assert await video.publish_replies(db, vsettings, bot) == 1
    (kw,) = bot.calls
    assert kw["chat_id"] == "@chan" and kw["reply_parameters"].message_id == 55
    assert kw["link_preview_options"].url == "https://kremlin/80910"
    assert kw["text"] == ('▶️ <a href="https://kremlin/80910">Watch: Meeting of Valdai &lt;Discussion&gt; Club</a>'
                          ' (Kremlin)')
    reply = db.get_video_reply(rid)
    assert (reply["status"], reply["external_id"]) == ("published", "77")


async def test_publish_reply_errors(db, vsettings):
    import httpx
    from telegram.error import BadRequest
    vsettings.dry_run, vsettings.telegram_channel_id = False, "@chan"
    no_tg = _approved_reply(db, tg_id=None)
    await video.publish_replies(db, vsettings, FakeBot())
    assert db.get_video_reply(no_tg)["status"] == "failed"

    rid = _approved_reply(db)
    await video.publish_replies(db, vsettings, FakeBot(httpx.ConnectError("down")))
    assert db.get_video_reply(rid)["status"] == "approved"  # network trouble: retried later
    await video.publish_replies(db, vsettings, FakeBot(BadRequest("Message to reply not found")))
    assert db.get_video_reply(rid)["status"] == "failed"


async def test_dry_run_prints_reply(db, vsettings, capsys):
    rid = _approved_reply(db)
    assert await video.publish_replies(db, vsettings, FakeBot()) == 1
    assert "would reply to post" in capsys.readouterr().out
    assert db.get_video_reply(rid)["status"] == "published"


# ---------- approval ----------
def test_video_decisions(db):
    pid = _published_post(db)
    v = _video(db, "Valdai Discussion Club", url="https://kremlin/1?a=1&b=2")
    rid = db.create_video_reply(pid, v["id"], "pending_approval")
    text = approval_bot.video_draft_text(db, db.get_video_reply(rid))
    assert f"Video for post #{pid}" in text and 'href="https://kremlin/1?a=1&amp;b=2"' in text
    assert "Moment: Vladimir Putin speech" in text
    assert approval_bot.apply_video_decision(db, rid, "approve") == (True, "✅ Approved, will be posted as a reply.")
    assert approval_bot.apply_video_decision(db, rid, "reject") == (False, "Already approved.")
    assert approval_bot.apply_video_decision(db, 999, "approve")[0] is False
