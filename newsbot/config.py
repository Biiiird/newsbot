"""Settings from environment (.env) and the sources list (YAML)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class Source:
    name: str
    kind: str  # "rss" | "telegram"
    trust: str = "medium"  # high | medium | low
    lang: str = "en"
    url: str = ""  # rss
    handle: str = ""  # telegram
    state_media: bool = False


@dataclass
class Settings:
    db_path: str = "data/newsbot.db"
    sources_file: str = "config/sources.yaml"
    dry_run: bool = True
    log_level: str = "INFO"

    llm_provider: str = "anthropic"
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-4-5-20251001"
    claude_cli_path: str = "claude"
    claude_cli_model: str = "haiku"

    telegram_bot_token: str = ""
    telegram_channel_id: str = ""
    admin_chat_id: str = ""
    admin_user_ids: set[int] = field(default_factory=set)
    telegram_include_source: bool = True
    attach_images: bool = True
    media_max_images: int = 2
    media_min_side: int = 600

    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session: str = "data/reader"

    x_enabled: bool = False
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_secret: str = ""
    x_source_reply: bool = True

    min_importance: int = 6
    auto_post: bool = False
    auto_min_importance: int = 8
    auto_require_low_risk: bool = True
    auto_require_high_trust: bool = True
    max_item_age_min: int = 120

    rss_poll_sec: int = 60
    min_post_interval_sec: int = 120
    max_posts_per_hour: int = 12
    dedup_threshold: int = 85
    dedup_window_hours: int = 12

    sources: list[Source] = field(default_factory=list)

    @property
    def rss_sources(self) -> list[Source]:
        return [s for s in self.sources if s.kind == "rss"]

    @property
    def telegram_sources(self) -> list[Source]:
        return [s for s in self.sources if s.kind == "telegram"]

    @property
    def telegram_reader_enabled(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash and self.telegram_sources)

    def source_by_name(self, name: str) -> Source | None:
        return next((s for s in self.sources if s.name == name), None)


def load_sources(path: str) -> list[Source]:
    p = Path(path)
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: list[Source] = []
    for entry in data.get("rss") or []:
        out.append(Source(
            name=entry["name"], kind="rss", url=entry["url"],
            trust=entry.get("trust", "medium"), lang=entry.get("lang", "en"),
            state_media=bool(entry.get("state_media", False)),
        ))
    for entry in data.get("telegram") or []:
        handle = entry["handle"].strip()
        out.append(Source(
            name=entry.get("name") or handle, kind="telegram", handle=handle,
            trust=entry.get("trust", "low"), lang=entry.get("lang", "ru"),
            state_media=bool(entry.get("state_media", False)),
        ))
    return out


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file, override=False)

    admin_chat = _str("ADMIN_CHAT_ID")
    admin_ids = {int(x) for x in _str("ADMIN_USER_IDS").replace(" ", "").split(",") if x}
    if not admin_ids and admin_chat.lstrip("-").isdigit() and not admin_chat.startswith("-"):
        admin_ids = {int(admin_chat)}  # a private chat id equals the user id

    s = Settings(
        db_path=_str("DB_PATH", "data/newsbot.db"),
        sources_file=_str("SOURCES_FILE", "config/sources.yaml"),
        dry_run=_bool("DRY_RUN", True),
        log_level=_str("LOG_LEVEL", "INFO"),
        llm_provider=_str("LLM_PROVIDER", "anthropic").lower(),
        anthropic_api_key=_str("ANTHROPIC_API_KEY"),
        llm_model=_str("LLM_MODEL", "claude-haiku-4-5-20251001"),
        claude_cli_path=_str("CLAUDE_CLI_PATH", "claude"),
        claude_cli_model=_str("CLAUDE_CLI_MODEL", "haiku"),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_channel_id=_str("TELEGRAM_CHANNEL_ID"),
        admin_chat_id=admin_chat,
        admin_user_ids=admin_ids,
        telegram_include_source=_bool("TELEGRAM_INCLUDE_SOURCE", True),
        attach_images=_bool("ATTACH_IMAGES", True),
        media_max_images=_int("MEDIA_MAX_IMAGES", 2),
        media_min_side=_int("MEDIA_MIN_SIDE", 600),
        tg_api_id=_int("TG_API_ID", 0),
        tg_api_hash=_str("TG_API_HASH"),
        tg_session=_str("TG_SESSION", "data/reader"),
        x_enabled=_bool("X_ENABLED", False),
        x_api_key=_str("X_API_KEY"),
        x_api_secret=_str("X_API_SECRET"),
        x_access_token=_str("X_ACCESS_TOKEN"),
        x_access_secret=_str("X_ACCESS_SECRET"),
        x_source_reply=_bool("X_SOURCE_REPLY", True),
        min_importance=_int("MIN_IMPORTANCE", 6),
        auto_post=_bool("AUTO_POST", False),
        auto_min_importance=_int("AUTO_MIN_IMPORTANCE", 8),
        auto_require_low_risk=_bool("AUTO_REQUIRE_LOW_RISK", True),
        auto_require_high_trust=_bool("AUTO_REQUIRE_HIGH_TRUST", True),
        max_item_age_min=_int("MAX_ITEM_AGE_MIN", 120),
        rss_poll_sec=_int("RSS_POLL_SEC", 60),
        min_post_interval_sec=_int("MIN_POST_INTERVAL_SEC", 120),
        max_posts_per_hour=_int("MAX_POSTS_PER_HOUR", 12),
        dedup_threshold=_int("DEDUP_THRESHOLD", 85),
        dedup_window_hours=_int("DEDUP_WINDOW_HOURS", 12),
    )
    s.sources = load_sources(s.sources_file)
    return s
