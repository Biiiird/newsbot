import pytest

from newsbot.config import Settings, Source
from newsbot.db import DB


@pytest.fixture
def settings():
    s = Settings(db_path=":memory:", dry_run=True, llm_provider="mock", max_item_age_min=120,
                 min_post_interval_sec=0, max_posts_per_hour=100)
    s.sources = [
        Source(name="ERR News", kind="rss", url="https://news.err.ee/rss", trust="high"),
        Source(name="Meduza", kind="rss", url="https://meduza.io/rss/en/all", trust="medium"),
        Source(name="Some TG", kind="telegram", handle="@sometg", trust="low", lang="ru", state_media=True),
    ]
    return s


@pytest.fixture
def db():
    return DB(":memory:")


@pytest.fixture
def high(settings):
    return settings.sources[0]


@pytest.fixture
def medium(settings):
    return settings.sources[1]
