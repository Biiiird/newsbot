import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import BadRequest

from newsbot import approval_bot, media, publisher
from newsbot.db import DB, SCHEMA, now
from newsbot.ingest import store_item
from newsbot.llm import Assessment
from newsbot.processor import process_once

TRUMP = {"kind": "person", "name": "Donald Trump", "url": "https://img/trump.jpg"}
ZEL = {"kind": "person", "name": "Volodymyr Zelenskyy", "url": "https://img/zelenskyy.jpg"}


def _post(db, headline="JUST IN: 🇺🇦 Trump meets Zelenskyy", images=(TRUMP, ZEL)):
    cid = db.add_item({"source": "ERR News", "source_type": "rss", "trust": "high", "title": headline,
                       "hash": headline, "norm": "a b c"}, None)[1]
    pid = db.create_post(cid, headline, "ERR News", "https://err.ee/1", "approved", 8, "low", "other",
                         list(images))
    return db.get_post(pid)


def _mock_client(monkeypatch, handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(media, "_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(wrapped)))
    return calls


# ---------- LLM output ----------
def test_assessment_cleans_people_and_countries():
    a = Assessment.from_dict({"headline": "x", "people": [" Donald Trump ", "Donald Trump", "", "A", "B"],
                              "countries": ["ua", "UK", "Russia", 7, "UA", "PL", "DE"]})
    assert a.people == ["Donald Trump", "A"]
    assert a.countries == ["UA", "GB", "PL"]
    missing = Assessment.from_dict({"headline": "x", "people": "Trump", "countries": None})
    assert missing.people == [] and missing.countries == []


# ---------- lookup ----------
def _wiki(pages, files=None):
    """Fake Wikipedia API: article lookups by title, then Commons metadata for "File:" titles."""
    files = files or {}

    def handler(r):
        title = r.url.params["titles"]
        if title.startswith("File:"):
            meta = files.get(title[5:], {})
            return httpx.Response(200, json={"query": {"pages": [
                {"title": title, "imageinfo": [{"extmetadata": {k: {"value": v} for k, v in meta.items()}}]}]}})
        return httpx.Response(200, json={"query": {"pages": [pages[title]]}})
    return handler


def _page(title, url, width=1600, height=2000, file="Portrait.jpg"):
    return {"title": title, "thumbnail": {"source": url}, "original": {"width": width, "height": height},
            "pageimage": file}


@pytest.mark.asyncio
async def test_resolver_portraits(monkeypatch):
    pages = {
        "Donald Trump": _page("Donald Trump", "https://img/trump.jpg", file="Trump_portrait.jpg"),
        "Mercury": {"title": "Mercury", "pageprops": {"disambiguation": ""}},
        "Nobody Atall": {"title": "Nobody Atall", "missing": True},
    }
    calls = _mock_client(monkeypatch, _wiki(pages))
    r = media.MediaResolver(max_images=2)

    # ambiguous and missing names are skipped; only max_images names are looked up
    assert await r.resolve(["Donald Trump", "Mercury", "Nobody Atall"]) == [TRUMP]
    assert len(calls) == 3  # Trump page + its file metadata, Mercury page
    assert await r.resolve([]) == []  # not about a person: no pictures

    assert (await r.resolve(["donald trump"]))[0]["url"] == "https://img/trump.jpg"
    assert len(calls) == 3  # cached
    assert await r.resolve(["Mercury"]) == []


@pytest.mark.asyncio
async def test_resolver_skips_low_quality_portraits(monkeypatch):
    pages = {
        "Small": _page("Small", "https://img/small.jpg", width=371, height=500),
        "Still": _page("Still", "https://img/still.png", width=619, height=748, file="Politician_2026.png"),
        "Named": _page("Named", "https://img/named.png", file="Named_Screenshot.png"),
        "Credit": _page("Credit", "https://img/credit.jpg", file="Credit.jpg"),
        "Good": _page("Good", "https://img/good.jpg", width=602, height=784, file="Good_Official_Portrait_(cropped).jpg"),
    }
    files = {
        "Politician_2026.png": {"Categories": "Female politicians of Morocco|Still images of YouTube videos"},
        "Credit.jpg": {"Credit": '<a href="https://www.youtube.com/watch?v=x">https://www.youtube.com/watch?v=x</a>'},
        "Good_Official_Portrait_(cropped).jpg": {"Categories": "Official portraits|Extracted images",
                                                 "Credit": "La Moncloa"},
    }
    _mock_client(monkeypatch, _wiki(pages, files))
    r = media.MediaResolver(max_images=5, min_side=600)
    assert await r.resolve(["Small", "Still", "Named", "Credit", "Good"]) == [
        {"kind": "person", "name": "Good", "url": "https://img/good.jpg"}]
    assert await media.MediaResolver(min_side=300).resolve(["Small"]) == [
        {"kind": "person", "name": "Small", "url": "https://img/small.jpg"}]


@pytest.mark.asyncio
async def test_resolver_survives_wikipedia_errors(monkeypatch):
    _mock_client(monkeypatch, lambda r: httpx.Response(503))
    assert await media.MediaResolver().resolve(["Donald Trump"]) == []


@pytest.mark.asyncio
async def test_download_skips_bad_images(monkeypatch):
    responses = {"/ok.png": httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"}),
                 "/html": httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
                 "/404": httpx.Response(404)}
    _mock_client(monkeypatch, lambda r: responses[r.url.path])
    items = [{"kind": "person", "url": f"https://x{p}"} for p in ("/404", "/ok.png", "/html")]
    assert await media.download(items) == [b"PNG"]


# ---------- storage ----------
def test_media_stored_on_post_and_old_db_migrated(db, tmp_path):
    p = _post(db)
    assert media.media_list(p) == [TRUMP, ZEL]
    assert media.media_list(_post(db, headline="no pictures", images=())) == []

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("\n".join(line for line in SCHEMA.splitlines() if "media" not in line))
    assert "media" not in {r[1] for r in old.execute("PRAGMA table_info(posts)")}
    old.close()
    assert "media" in {r["name"] for r in DB(str(path)).q("PRAGMA table_info(posts)")}


@pytest.mark.asyncio
async def test_processor_attaches_images(db, settings, high):
    store_item(db, settings, high, "Trump and Zelenskyy meet at the UN general assembly in New York",
               url="u1", published_at=now())

    class LLM:
        async def assess(self, item):
            return Assessment(True, 8, "low", "politics_diplomacy", "JUST IN: 🇺🇦 Trump meets Zelenskyy", "r",
                              people=["Donald Trump"], countries=["UA"])

    class Resolver:
        async def resolve(self, people):
            assert people == ["Donald Trump"]
            return [TRUMP]

    await process_once(db, settings, LLM(), resolver=Resolver())
    assert media.media_list(db.posts_by_status("pending_approval")[0]) == [TRUMP]


def test_draft_lists_images(db):
    text = approval_bot.draft_text(db, _post(db))
    assert '🖼 Images: <a href="https://img/trump.jpg">Donald Trump</a>, ' in text
    assert ">Volodymyr Zelenskyy</a>" in text


# ---------- publishing ----------
class FakeBot:
    def __init__(self, reject_photos=False):
        self.calls, self.reject_photos = [], reject_photos

    async def _call(self, name, **kw):
        self.calls.append((name, kw))
        if self.reject_photos and name != "send_message":
            raise BadRequest("Wrong file identifier/http url specified")
        return SimpleNamespace(message_id=7)

    async def send_message(self, **kw):
        return await self._call("send_message", **kw)

    async def send_photo(self, **kw):
        return await self._call("send_photo", **kw)

    async def send_media_group(self, **kw):
        await self._call("send_media_group", **kw)
        return [SimpleNamespace(message_id=8), SimpleNamespace(message_id=9)]


@pytest.fixture
def downloads(monkeypatch):
    async def fake_download(items, timeout=15.0):
        return [m["url"].encode() for m in items]

    monkeypatch.setattr(media, "download", fake_download)


@pytest.mark.asyncio
async def test_telegram_album_with_caption(db, downloads):
    bot = FakeBot()
    assert await publisher.TelegramTarget(bot, "@chan", True).publish(_post(db)) == "8"
    (name, kw), = bot.calls
    assert name == "send_media_group" and kw["chat_id"] == "@chan"
    first, second = kw["media"]
    assert first.media.input_file_content == b"https://img/trump.jpg"
    assert first.caption.startswith("<b>JUST IN:</b> 🇺🇦 Trump meets Zelenskyy") and "Source:" in first.caption
    assert first.parse_mode == "HTML" and second.caption is None


@pytest.mark.asyncio
async def test_telegram_single_photo_and_text_only(db, downloads):
    bot = FakeBot()
    target = publisher.TelegramTarget(bot, "@chan", True)
    await target.publish(_post(db, headline="one image", images=(ZEL,)))
    await target.publish(_post(db, headline="no images", images=()))
    assert [(n, kw.get("photo")) for n, kw in bot.calls] == [
        ("send_photo", b"https://img/zelenskyy.jpg"), ("send_message", None)]
    assert bot.calls[0][1]["caption"].startswith("one image")


@pytest.mark.asyncio
async def test_telegram_falls_back_to_text(db, downloads):
    long_post = _post(db, headline="x" * 1010)  # + source line > 1024
    bot = FakeBot()
    await publisher.TelegramTarget(bot, "@chan", True).publish(long_post)
    assert [n for n, _ in bot.calls] == ["send_message"]  # too long for a caption

    bot = FakeBot(reject_photos=True)
    assert await publisher.TelegramTarget(bot, "@chan", True).publish(_post(db, headline="short")) == "7"
    assert [n for n, _ in bot.calls] == ["send_media_group", "send_message"]


def test_caption_fits_counts_utf16(db):
    emoji = _post(db, headline="🇺🇦" * 256, images=())  # 256 flags = 1024 UTF-16 code units
    assert publisher.caption_fits(emoji, include_source=False)
    assert not publisher.caption_fits(emoji, include_source=True)


def test_dry_run_prints_images(db, capsys):
    import asyncio
    asyncio.run(publisher.ConsoleTarget().publish(_post(db)))
    out = capsys.readouterr().out
    assert "image: Donald Trump https://img/trump.jpg" in out and "image: Volodymyr Zelenskyy" in out
    assert json.loads(db.get_post(1)["media"])[0]["kind"] == "person"
