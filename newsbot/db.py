"""SQLite storage. One small file, no server needed.

Post lifecycle (posts.status):
    pending_approval -> approved -> published
                     -> rejected
    approved -> failed (publishing error)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    source_type   TEXT NOT NULL,
    trust         TEXT NOT NULL,
    lang          TEXT,
    state_media   INTEGER DEFAULT 0,
    title         TEXT NOT NULL,
    body          TEXT,
    url           TEXT,
    hash          TEXT NOT NULL UNIQUE,
    norm          TEXT NOT NULL,
    cluster_id    INTEGER REFERENCES clusters(id),
    published_at  TEXT,
    seen_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);

CREATE TABLE IF NOT EXISTS clusters (
    id            INTEGER PRIMARY KEY,
    lead_item_id  INTEGER,
    norm          TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    item_count    INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'new',   -- new | scored | dropped | stale | error
    importance    INTEGER,
    risk          TEXT,
    category      TEXT,
    reason        TEXT
);
CREATE INDEX IF NOT EXISTS idx_clusters_status ON clusters(status);
CREATE INDEX IF NOT EXISTS idx_clusters_last_seen ON clusters(last_seen);

CREATE TABLE IF NOT EXISTS posts (
    id                INTEGER PRIMARY KEY,
    cluster_id        INTEGER NOT NULL UNIQUE REFERENCES clusters(id),
    headline          TEXT NOT NULL,
    source_name       TEXT,
    source_url        TEXT,
    status            TEXT NOT NULL,
    importance        INTEGER,
    risk              TEXT,
    category          TEXT,
    media             TEXT,                      -- JSON list of images, see media.py
    retry_after       TEXT,                      -- after a network error: don't retry before this
    admin_message_id  INTEGER,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    published_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);

CREATE TABLE IF NOT EXISTS publish_log (
    id           INTEGER PRIMARY KEY,
    post_id      INTEGER NOT NULL REFERENCES posts(id),
    platform     TEXT NOT NULL,
    external_id  TEXT,
    ok           INTEGER NOT NULL,
    error        TEXT,
    created_at   TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if "media" not in cols:  # databases created before images were added
            self.conn.execute("ALTER TABLE posts ADD COLUMN media TEXT")
        if "retry_after" not in cols:
            self.conn.execute("ALTER TABLE posts ADD COLUMN retry_after TEXT")

    # ---------- generic ----------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def exec(self, sql: str, args: tuple = ()) -> int:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur.lastrowid

    # ---------- items / clusters ----------
    def hash_exists(self, h: str) -> bool:
        return self.one("SELECT 1 FROM items WHERE hash = ?", (h,)) is not None

    def recent_clusters(self, hours: int) -> list[sqlite3.Row]:
        since = iso(now() - timedelta(hours=hours))
        return self.q("SELECT id, norm FROM clusters WHERE last_seen >= ? ORDER BY id DESC", (since,))

    def add_item(self, item: dict[str, Any], cluster_id: int | None) -> tuple[int, int, bool]:
        """Insert an item. Creates a new cluster when cluster_id is None.
        Returns (item_id, cluster_id, is_new_cluster)."""
        ts = iso()
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO items (source, source_type, trust, lang, state_media, title, body, url,
                                  hash, norm, published_at, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item["source"], item["source_type"], item["trust"], item.get("lang"),
             int(bool(item.get("state_media"))), item["title"], item.get("body"), item.get("url"),
             item["hash"], item["norm"], item.get("published_at"), ts),
        )
        item_id = cur.lastrowid
        is_new = cluster_id is None
        if is_new:
            cur.execute(
                "INSERT INTO clusters (lead_item_id, norm, first_seen, last_seen) VALUES (?,?,?,?)",
                (item_id, item["norm"], ts, ts),
            )
            cluster_id = cur.lastrowid
        else:
            cur.execute(
                "UPDATE clusters SET item_count = item_count + 1, last_seen = ? WHERE id = ?",
                (ts, cluster_id),
            )
        cur.execute("UPDATE items SET cluster_id = ? WHERE id = ?", (cluster_id, item_id))
        self.conn.commit()
        return item_id, cluster_id, is_new

    def new_clusters(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.q(
            """SELECT c.id AS cluster_id, c.item_count, i.*
               FROM clusters c JOIN items i ON i.id = c.lead_item_id
               WHERE c.status = 'new' ORDER BY c.id LIMIT ?""",
            (limit,),
        )

    def set_cluster(self, cluster_id: int, status: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        sql = f"UPDATE clusters SET status = ?{', ' + cols if cols else ''} WHERE id = ?"
        self.exec(sql, (status, *fields.values(), cluster_id))

    def cluster_sources(self, cluster_id: int) -> list[str]:
        return [r["source"] for r in self.q(
            "SELECT DISTINCT source FROM items WHERE cluster_id = ?", (cluster_id,))]

    # ---------- posts ----------
    def create_post(self, cluster_id: int, headline: str, source_name: str, source_url: str | None,
                    status: str, importance: int, risk: str, category: str,
                    media: list[dict[str, str]] | None = None) -> int:
        ts = iso()
        return self.exec(
            """INSERT INTO posts (cluster_id, headline, source_name, source_url, status,
                                  importance, risk, category, media, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (cluster_id, headline, source_name, source_url, status, importance, risk, category,
             json.dumps(media) if media else None, ts, ts),
        )

    def get_post(self, post_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM posts WHERE id = ?", (post_id,))

    def update_post(self, post_id: int, **fields: Any) -> None:
        fields["updated_at"] = iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.exec(f"UPDATE posts SET {cols} WHERE id = ?", (*fields.values(), post_id))

    def posts_by_status(self, status: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM posts WHERE status = ? ORDER BY id LIMIT ?", (status, limit))

    def recent_posts(self, hours: int, limit: int = 40) -> list[sqlite3.Row]:
        """Posts created in the last `hours` (any status but failed), newest first."""
        return self.q(
            "SELECT id, headline FROM posts WHERE created_at >= ? AND status != 'failed' ORDER BY id DESC LIMIT ?",
            (iso(now() - timedelta(hours=hours)), limit))

    def unsent_drafts(self) -> list[sqlite3.Row]:
        return self.q(
            "SELECT * FROM posts WHERE status = 'pending_approval' AND admin_message_id IS NULL ORDER BY id")

    def next_approved(self) -> sqlite3.Row | None:
        # most important first, then oldest; posts waiting to retry after a network error are skipped
        return self.one(
            """SELECT * FROM posts WHERE status = 'approved' AND (retry_after IS NULL OR retry_after <= ?)
               ORDER BY importance DESC, id LIMIT 1""", (iso(),))

    def failed_attempts(self, post_id: int) -> int:
        return int(self.one("SELECT COUNT(*) AS n FROM publish_log WHERE post_id = ? AND ok = 0",
                            (post_id,))["n"])

    def last_published_at(self) -> datetime | None:
        row = self.one("SELECT MAX(published_at) AS t FROM posts WHERE status = 'published'")
        return parse_iso(row["t"]) if row else None

    def published_since(self, since: datetime) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM posts WHERE status = 'published' AND published_at >= ?",
                       (iso(since),))
        return int(row["n"])

    def log_publish(self, post_id: int, platform: str, ok: bool, external_id: str | None = None,
                    error: str | None = None) -> None:
        self.exec(
            "INSERT INTO publish_log (post_id, platform, external_id, ok, error, created_at) VALUES (?,?,?,?,?,?)",
            (post_id, platform, external_id, int(ok), error, iso()),
        )

    def stats(self) -> dict[str, Any]:
        return {
            "items": self.one("SELECT COUNT(*) n FROM items")["n"],
            "clusters": {r["status"]: r["n"] for r in self.q(
                "SELECT status, COUNT(*) n FROM clusters GROUP BY status")},
            "posts": {r["status"]: r["n"] for r in self.q(
                "SELECT status, COUNT(*) n FROM posts GROUP BY status")},
        }
