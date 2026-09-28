"""Pictures for posts: portraits of the people the story is about (the lead image of their
English Wikipedia article, freely licensed images only). Countries only appear as flag emojis in
the text, so a story without a public figure gets no pictures.

Images are looked up when the draft is created, so the admin can check them before publishing,
and downloaded when the post goes out. A failed lookup or download only means fewer pictures."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "newsbot/1.0 (Telegram news channel bot)"  # Wikimedia rejects requests without one
WIKI_API = "https://en.wikipedia.org/w/api.php"
MAX_IMAGE_BYTES = 5 * 1024 * 1024


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
    def __init__(self, max_images: int = 2, timeout: float = 10.0):
        self.max_images, self.timeout = max_images, timeout
        self._portraits: dict[str, str | None] = {}

    async def portrait(self, client: httpx.AsyncClient, name: str) -> str | None:
        key = name.casefold()
        if key not in self._portraits:
            r = await client.get(WIKI_API, params={
                "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
                "titles": name, "prop": "pageimages|pageprops", "piprop": "thumbnail",
                "pithumbsize": "800", "ppprop": "disambiguation"})
            r.raise_for_status()
            pages = r.json().get("query", {}).get("pages") or [{}]
            page = pages[0]
            url = None
            if not page.get("missing") and "disambiguation" not in page.get("pageprops", {}):
                url = page.get("thumbnail", {}).get("source")
            self._portraits[key] = url
        return self._portraits[key]

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
