from datetime import timedelta
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
