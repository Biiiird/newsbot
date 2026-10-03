"""Pictures for posts: portraits of the people the story is about (the lead image of their
English Wikipedia article, freely licensed images only). Countries only appear as flag emojis in
the text, so a story without a public figure gets no pictures.

Only good pictures are used: the original file must be at least min_side pixels on its shorter side,
and video stills and screenshots (blurry frames grabbed from YouTube etc.) are skipped, judged by the
file's Commons categories, credit and name. A person without a good picture gets none.

Images are looked up when the draft is created, so the admin can check them before publishing,
and downloaded when the post goes out. A failed lookup or download only means fewer pictures."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Wikimedia rejects requests without a User-Agent and throttles ones without contact info
USER_AGENT = "newsbot/1.0 (Telegram news channel bot; https://github.com/Biiiird/newsbot)"
WIKI_API = "https://en.wikipedia.org/w/api.php"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
THUMB_SIZE = 1280  # Telegram shows photos at up to 1280px
# Commons categories / credits / file names of frames grabbed from video, e.g. the category
# "Still images of YouTube videos" or a youtube.com credit link
LOW_QUALITY = re.compile(
    r"youtube|youtu\.be|vimeo|video|screen ?shot|screengrab|screen ?capture|screencap|\bstills?\b|\bframe\b",
    re.IGNORECASE)


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def media_list(post) -> list[dict[str, str]]:
    """The images stored on a post row ([] if none or unreadable)."""
    try:
        raw = post["media"]
    except (IndexError, KeyError):
        return []
    try:
        return [m for m in json.loads(raw or "[]") if m.get("url")]
    except (ValueError, AttributeError):
        return []


class MediaResolver:
    def __init__(self, max_images: int = 2, min_side: int = 600, timeout: float = 10.0):
        self.max_images, self.min_side, self.timeout = max_images, min_side, timeout
        self._portraits: dict[str, str | None] = {}

    async def portrait(self, client: httpx.AsyncClient, name: str) -> str | None:
        key = name.casefold()
        if key not in self._portraits:
            r = await client.get(WIKI_API, params={
                "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
                "titles": name, "prop": "pageimages|pageprops", "piprop": "thumbnail|original|name",
                "pithumbsize": str(THUMB_SIZE), "ppprop": "disambiguation"})
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages") or [{}]
            page = pages[0]
            url = None
            if not page.get("missing") and "disambiguation" not in page.get("pageprops", {}):
                url = page.get("thumbnail", {}).get("source")
            if url and not await self._good_quality(client, name, page):
                url = None
            self._portraits[key] = url
        return self._portraits[key]

    async def _good_quality(self, client: httpx.AsyncClient, name: str, page: dict[str, Any]) -> bool:
        original = page.get("original", {})
        side = min(original.get("width", 0), original.get("height", 0))
        if side < self.min_side:
            log.info("portrait of %r too small (%spx < %spx), skipped", name, side, self.min_side)
            return False
        file = page.get("pageimage", "")
        r = await client.get(WIKI_API, params={
            "action": "query", "format": "json", "formatversion": "2", "titles": f"File:{file}",
            "prop": "imageinfo", "iiprop": "extmetadata", "iiextmetadatafilter": "Categories|Credit"})
        r.raise_for_status()
        info = ((r.json().get("query", {}).get("pages") or [{}])[0].get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        text = " ".join([file.replace("_", " "), *(meta.get(k, {}).get("value", "") for k in ("Categories", "Credit"))])
        if m := LOW_QUALITY.search(text):
            log.info("portrait of %r looks like a video still or screenshot (%r), skipped", name, m.group())
            return False
        return True

    async def resolve(self, people: list[str]) -> list[dict[str, str]]:
        media: list[dict[str, str]] = []
        if people and self.max_images > 0:
            async with _client(self.timeout) as client:
                for name in people[:self.max_images]:
                    try:
                        url = await self.portrait(client, name)
                    except Exception as e:  # noqa: BLE001
                        log.warning("portrait lookup failed for %r: %s", name, e)
                        continue
                    if url:
                        media.append({"kind": "person", "name": name, "url": url})
                    else:
                        log.info("no Wikipedia portrait for %r", name)
        return media


async def download(media: list[dict[str, Any]], timeout: float = 15.0) -> list[bytes]:
    """Fetch the images, skipping any that fail."""
    out: list[bytes] = []
    if not media:
        return out
    async with _client(timeout) as client:
        for m in media:
            try:
                r = await client.get(m["url"])
                r.raise_for_status()
                if not r.headers.get("content-type", "").startswith("image/"):
                    raise ValueError(f"not an image ({r.headers.get('content-type')})")
                if len(r.content) > MAX_IMAGE_BYTES:
                    raise ValueError(f"too large ({len(r.content)} bytes)")
                out.append(r.content)
            except Exception as e:  # noqa: BLE001
                log.warning("could not download %s image %s: %s", m.get("kind"), m.get("url"), e)
    return out
