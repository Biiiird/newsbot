"""claude -p provider, tested with a fake `claude` executable."""
import json
import stat
import sys

import pytest

from newsbot.db import now
from newsbot.ingest import store_item
from newsbot.llm import Assessment, ClaudeCLILLM, FallbackLLM, LimitReached
from newsbot.processor import process_once

FAKE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
open(os.environ["FAKE_LOG"], "w").write(json.dumps({{"args": args, "prompt": prompt,
    "has_key": "ANTHROPIC_API_KEY" in os.environ}}))
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "ok":
    print(json.dumps({{"type": "result", "is_error": False, "result": "...",
        "structured_output": {{"is_news": True, "importance": 8, "risk": "low", "category": "energy",
                               "headline": "JUST IN: 🇪🇪 Test", "reason": "r"}}}}))
elif mode == "limit":
    print(json.dumps({{"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1790000000"}}))
elif mode == "garbage":
    print("Invalid API key · Please run /login")
'''


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    path = tmp_path / "claude"
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    return str(path), log, tmp_path


ITEM = {"source": "ERR News", "trust": "high", "title": "Estonia to build new LNG terminal", "body": "b"}


async def test_cli_success_uses_subscription_not_api_key(fake_cli, monkeypatch):
    path, log, tmp = fake_cli
    monkeypatch.setenv("FAKE_MODE", "ok")
    a = await ClaudeCLILLM(path, "haiku", workdir=str(tmp / "work")).assess(ITEM)
    assert a.importance == 8 and a.headline == "JUST IN: 🇪🇪 Test"
    call = json.loads(log.read_text())
    assert call["has_key"] is False  # API key stripped, so the subscription is used
    assert "-p" in call["args"] and "--json-schema" in call["args"] and "haiku" in call["args"]
    assert "Estonia to build new LNG terminal" in call["prompt"]


async def test_cli_limit_and_errors(fake_cli, monkeypatch):
    path, _, tmp = fake_cli
    llm = ClaudeCLILLM(path, workdir=str(tmp))
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(LimitReached):
        await llm.assess(ITEM)
    monkeypatch.setenv("FAKE_MODE", "garbage")
    with pytest.raises(RuntimeError, match="login"):
        await llm.assess(ITEM)
    with pytest.raises(RuntimeError, match="not found"):
        await ClaudeCLILLM(str(tmp / "nope")).assess(ITEM)


class Stub:
    def __init__(self, exc=None, name="stub"):
        self.exc, self.calls, self.name = exc, 0, name

    async def assess(self, item):
        self.calls += 1
        if self.exc:
            raise self.exc
        return Assessment(True, 7, "low", "other", self.name, "")


async def test_fallback_after_limit_with_cooldown():
    primary, api = Stub(LimitReached("limit")), Stub(name="api")
    llm = FallbackLLM(primary, api, cooldown=900)
    assert (await llm.assess(ITEM)).headline == "api"
    assert (await llm.assess(ITEM)).headline == "api"
    assert primary.calls == 1  # skipped during cooldown


async def test_no_fallback_raises_limit_and_keeps_story_queued(db, settings, high):
    store_item(db, settings, high, "Estonia to build new LNG terminal in Paldiski port", published_at=now())
    llm = FallbackLLM(Stub(LimitReached("limit")), None)
    counts = await process_once(db, settings, llm)
    assert counts == {"waiting_for_limit": 1}
    assert len(db.new_clusters()) == 1  # still waiting, not marked as error
    with pytest.raises(LimitReached):  # cooldown active
        await llm.assess(ITEM)


async def test_other_cli_error_falls_back_per_item():
    llm = FallbackLLM(Stub(RuntimeError("timeout")), Stub(name="api"))
    assert (await llm.assess(ITEM)).headline == "api"
