from datetime import datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from newsbot.db import now
from newsbot.dedup import find_cluster, normalize
from newsbot.ingest import clean, store_item
from newsbot.ingest import rss
from newsbot.ingest.telegram_reader import split_message


def test_normalize_drops_stopwords_and_punctuation():
    assert normalize("BREAKING: The Estonian PM says NATO will defend the Baltics!") == \
        "estonian pm nato defend baltics"
    assert normalize("Путин и Си провели встречу") == "путин си провели встречу"


def test_near_duplicates_cluster_but_different_stories_dont():
    a = normalize("Estonia expels Russian diplomat over embassy spying allegations")
    same = normalize("Estonia expels a Russian diplomat over spying allegations at embassy")
    other = normalize("Latvia raises interest in new air defence systems from Germany")
    cands = [{"id": 1, "norm": a}]
    assert find_cluster(same, cands, 85) == 1
    assert find_cluster(other, cands, 85) is None


def test_short_headlines_need_strict_match():
    cands = [{"id": 1, "norm": normalize("Oil prices rise sharply today")}]
    assert find_cluster(normalize("Oil prices fall sharply"), cands, 85) is None


def test_clean_html():
    assert clean("<p>Hello&nbsp;<b>world</b></p>\n\n") == "Hello world"


def test_store_item_new_duplicate_joined_stale(db, settings, high, medium):
    t = now()
    assert store_item(db, settings, high, "Estonia expels Russian diplomat over spying allegations",
                      url="u1", published_at=t) == "new"
    assert store_item(db, settings, high, "Estonia expels Russian diplomat over spying allegations",
                      url="u1", published_at=t) == "duplicate"
    assert store_item(db, settings, medium, "Estonia expels a Russian diplomat over spying allegations",
                      url="u2", published_at=t) == "joined"
    assert store_item(db, settings, high, "Finland closes border crossing with Russia again after incident",
                      published_at=t - timedelta(hours=5)) == "stale"
    assert store_item(db, settings, high, "   ") == "empty"

    stats = db.stats()
    assert stats["items"] == 3
    assert stats["clusters"] == {"new": 1, "stale": 1}
    cluster = db.new_clusters()[0]
    assert cluster["item_count"] == 2
    assert set(db.cluster_sources(cluster["cluster_id"])) == {"ERR News", "Meduza"}


def test_split_telegram_message():
    title, body = split_message("Short headline here\n\nLonger body text with details.")
    assert title == "Short headline here"
    assert body.endswith("details.")
    long = "A" * 50 + ". " + "B" * 300
    title, _ = split_message(long)
    assert title == "A" * 50 + "."


def _feed(items):
    entries = "".join(
        f"<item><title>{t}</title><link>{l}</link><description>&lt;p&gt;{d}&lt;/p&gt;</description>"
        f"<pubDate>{format_datetime(p)}</pubDate></item>" for t, l, d, p in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{entries}</channel></rss>'


@pytest.mark.asyncio
async def test_poll_once_with_mock_http(db, settings):
    t = now()
    feeds = {
        "https://news.err.ee/rss": _feed([
            ("NATO jets scrambled after Russian aircraft violates Estonian airspace", "https://err/1", "Details", t),
            ("Tallinn weather: sunny week ahead for the capital", "https://err/2", "Sun", t - timedelta(days=2)),
        ]),
    }

    def handler(request: httpx.Request):
        url = str(request.url)
        if url in feeds:
            return httpx.Response(200, text=feeds[url])
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        counts = await rss.poll_once(db, settings, client)
    assert counts == {"new": 1, "stale": 1, "error": 1}
    row = db.new_clusters()[0]
    assert row["title"].startswith("NATO jets")
    assert row["body"] == "Details"
    assert row["url"] == "https://err/1"


def test_split_message_strips_aggregator_wrapping():
    title, body = split_message('JUST IN: 🇺🇸🇨🇳 Trump administration bans DeepSeek on government devices.\n\n@BRICSNews')
    assert title == "Trump administration bans DeepSeek on government devices."
    assert "@BRICSNews" not in body
    title, _ = split_message("BREAKING: Bitcoin reclaims $83,000\n\n@WatcherGuru")
    assert title == "Bitcoin reclaims $83,000"


def test_due_sources_respects_every_sec():
    from newsbot.config import Source
    from types import SimpleNamespace
    fast = Source(name="fast", kind="rss", url="https://a")
    slow = Source(name="slow", kind="rss", url="https://b", every_sec=180)
    s = SimpleNamespace(rss_sources=[fast, slow])
    assert rss.due_sources(s, {}, 1000) == [fast, slow]
    assert rss.due_sources(s, {"fast": 990, "slow": 990}, 1000) == [fast]  # slow polled 10s ago
    assert rss.due_sources(s, {"fast": 990, "slow": 800}, 1000) == [fast, slow]


@pytest.mark.asyncio
async def test_google_titles_and_clock_offset(db, settings):
    from newsbot.config import Source
    t = now()
    google = Source(name="Reuters", kind="rss", trust="high",
                    url="https://news.google.com/rss/search?q=source:Reuters")
    beijing = Source(name="Global Times", kind="rss", url="https://gt/rss", clock_offset_hours=-8)
    feeds = {
        google.url: _feed([("Fed holds rates steady as inflation cools - Reuters", "https://g/1", "x", t)]),
        beijing.url: _feed([("China and Iran sign energy deal", "https://gt/1", "x", t + timedelta(hours=8))]),
    }
    handler = lambda request: httpx.Response(200, text=feeds[str(request.url)])  # noqa: E731
    settings.sources = [google, beijing]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await rss.poll_once(db, settings, client) == {"new": 2}
    titles = {r["title"]: r["published_at"] for r in db.new_clusters()}
    assert "Fed holds rates steady as inflation cools" in titles
    gt = titles["China and Iran sign energy deal"]
    assert abs((datetime.fromisoformat(gt) - t).total_seconds()) < 5  # shifted back to real time


def test_future_dates_cannot_dodge_the_age_filter(db, settings):
    from newsbot.config import Source
    src = Source(name="Odd clock", kind="rss")
    store_item(db, settings, src, "Some story with a date far in the future", "", "https://x/1",
               now() + timedelta(hours=8))
    stored = db.new_clusters()[0]["published_at"]
    assert datetime.fromisoformat(stored) <= now() + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_new_feed_undated_backlog_is_not_news(db, settings):
    from newsbot.config import Source
    src = Source(name="Undated", kind="rss", url="https://u/rss")
    items = ["Iran and China sign a new trade agreement", "Tehran hosts regional security conference"]
    body = "".join(f"<item><title>{t}</title><link>https://u/{i}</link></item>" for i, t in enumerate(items))

    def handler(request):
        return httpx.Response(200, text=f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>')

    settings.sources = [src]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await rss.poll_once(db, settings, client) == {"stale": 2}  # first poll: backlog
        items.append("Iran announces new oil export deal with India")
        body += "<item><title>Iran announces new oil export deal with India</title><link>https://u/9</link></item>"
        assert await rss.poll_once(db, settings, client) == {"duplicate": 2, "new": 1}  # later: undated is new
