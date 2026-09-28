"""Scoring + rewriting with Claude.

One API call per new story. Claude is forced to answer through a tool, so the output is
always structured JSON (no parsing of free text).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the editor of a fast English-language news channel on Telegram and X covering \
world geopolitics in every region (Europe including Russia/Ukraine and the Baltic and Nordic states, \
the Middle East, Asia-Pacific, Africa and the Americas): wars and conflicts, diplomacy, security and \
defence, elections and changes of government, sanctions, trade, energy and markets.

For each incoming item decide whether it is worth posting and write the post.

Scoring (importance 0-10):
- 9-10: major breaking events: war escalation, heads of state decisions, major attacks, market-moving central bank or sanctions news
- 7-8: significant government, military, security, diplomatic, trade, energy or economic news with international relevance, from any region
- 5-6: notable national politics and security news from any country (elections, government crises, \
political prosecutions, protests with political impact, diplomatic incidents, military appointments), \
smaller regional conflicts
- 0-4: local crime, accidents and disasters without political or international impact, human-interest, \
culture, sport, weather, health (unless an international emergency), opinion, analysis pieces, features, ads \
-> set is_news=false if it is not news at all

Risk:
- "high" if the item involves casualties, military claims, attacks, accusations against named people, \
unverified or single-source claims, or comes from a low-trust or state-media source
- "low" otherwise

Headline rules (house style):
- Start with "JUST IN:" for fresh breaking items or "BREAKING:" only for importance 9-10; otherwise no prefix
- Then the flag emoji of each country in the countries field, main country first \
(e.g. "🇺🇸🇺🇦"), then the news
- One or two short factual sentences, under 260 characters total, plain English
- No hashtags, no links, no opinion, no speculation
- Quote with straight double quotes "like this", never single or curly quotes
- Never use semicolons. End the sentence with a period and start a new one instead
- Use ONLY facts stated in the item. Do not add background, context, motives or explanations \
("amid tensions", "signalling...", "a NATO member") that the item does not say
- Attribute claims that are not confirmed facts: "according to <source>", "<country> claims"
- If the source is state media, say so, e.g. "Russian state media claims ..."
- Translate non-English items into English

Pictures (attached to the post):
- people: up to 2 well-known public figures the story is about, i.e. whose actions, decisions \
or statements are the news (heads of state or government, ministers, party leaders, well-known \
CEOs), each as the exact title of their English Wikipedia article, e.g. "Donald Trump", \
"Volodymyr Zelenskyy". Leave it empty when the story is about a country, institution or event \
rather than a person, or a person is only mentioned in passing. Never private individuals, \
victims or minors.
- countries: ISO 3166-1 alpha-2 codes of the countries the post mentions, main country first, \
at most 3, e.g. ["UA", "RU"]. Use "EU" for the European Union. Empty if none.

Duplicates: the message may list posts made in the last hours. If the item reports the same event \
as one of them, even worded differently, from another angle or with a small new detail, set \
duplicate_of to that post's number. Use 0 if it is a different event, or if it is a major new \
development that deserves its own post (e.g. a much higher death toll or an official confirmation).

Always answer by calling the submit_assessment tool."""

TOOL = {
    "name": "submit_assessment",
    "description": "Submit the editorial assessment and the post text for one news item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_news": {"type": "boolean", "description": "False for opinion, features, ads, sport, weather, etc."},
            "importance": {"type": "integer", "minimum": 0, "maximum": 10},
            "risk": {"type": "string", "enum": ["low", "high"]},
            "category": {"type": "string", "enum": [
                "war_security", "politics_diplomacy", "economy_markets", "energy", "sanctions",
                "technology", "society", "other"]},
            "headline": {"type": "string", "description": "The post text in house style."},
            "reason": {"type": "string", "description": "One short sentence explaining the score."},
            "people": {"type": "array", "items": {"type": "string"},
                       "description": "Up to 2 public figures, as English Wikipedia article titles."},
            "countries": {"type": "array", "items": {"type": "string"},
                          "description": "Up to 3 ISO 3166-1 alpha-2 country codes, main country first."},
            "duplicate_of": {"type": "integer",
                             "description": "Number of the earlier post about the same event, or 0."},
        },
        "required": ["is_news", "importance", "risk", "category", "headline", "reason", "people", "countries",
                     "duplicate_of"],
    },
}


@dataclass
class Assessment:
    is_news: bool
    importance: int
    risk: str
    category: str
    headline: str
    reason: str
    people: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    duplicate_of: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Assessment":
        return cls(
            is_news=bool(d.get("is_news", True)),
            importance=max(0, min(10, int(d.get("importance", 0)))),
            risk="high" if d.get("risk") == "high" else "low",
            category=str(d.get("category") or "other"),
            headline=tidy_headline(str(d.get("headline") or "")),
            reason=str(d.get("reason") or "").strip(),
            people=_unique(str(x).strip() for x in _as_list(d.get("people")))[:2],
            countries=_unique(COUNTRY_ALIASES.get(c, c) for c in (str(x).strip().upper() for x in _as_list(
                d.get("countries"))) if re.fullmatch(r"[A-Z]{2}", c))[:3],
            duplicate_of=_int_or_zero(d.get("duplicate_of")),
        )


def tidy_headline(text: str) -> str:
    """House style the model sometimes misses: double quotes only, no semicolons."""
    text = re.sub(r"[“”„]", '"', text)
    text = re.sub(r"‘([^‘’\n]+)’", r'"\1"', text)
    text = re.sub(r"(?<![\w'])'([^'\n]+)'(?![\w'])", r'"\1"', text)  # 'x' but not Iran's
    text = re.sub(r"\s*;\s*(\S)", lambda m: ". " + m.group(1).upper(), text)
    return re.sub(r"\s*;\s*$", ".", text.strip())


COUNTRY_ALIASES = {"UK": "GB", "EL": "GR"}  # common non-ISO codes


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _unique(values) -> list[str]:
    out: list[str] = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


class LLM(Protocol):
    async def assess(self, item: dict[str, Any]) -> Assessment: ...


def build_user_message(item: dict[str, Any]) -> str:
    parts = [
        f"Source: {item['source']} (trust: {item['trust']}"
        + (", STATE MEDIA" if item.get("state_media") else "") + ")",
        f"Language: {item.get('lang') or 'unknown'}",
        f"Reported by {item.get('item_count', 1)} source(s) so far",
        f"Title: {item['title']}",
    ]
    body = (item.get("body") or "").strip()
    if body and body != item["title"]:
        parts.append(f"Text: {body[:2500]}")
    recent = item.get("recent_posts") or []
    if recent:
        parts.append("\nPosts already made in the last hours (for duplicate_of):")
        parts += [f"#{p['id']}: {p['headline']}" for p in recent]
    return "\n".join(parts)


class ClaudeLLM:
    def __init__(self, api_key: str, model: str):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic(api_key=api_key, max_retries=3)
        self.model = model

    async def assess(self, item: dict[str, Any]) -> Assessment:
        msg = await self.client.messages.create(
            model=self.model,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "submit_assessment"},
            messages=[{"role": "user", "content": build_user_message(item)}],
        )
        for block in msg.content:
            if block.type == "tool_use":
                return Assessment.from_dict(block.input)
        raise RuntimeError(f"No tool call in Claude response: {msg.content!r}")


class MockLLM:
    """Keyword heuristic, for trying the pipeline without an API key."""

    HOT = ["attack", "missile", "drone", "war", "troops", "sanction", "nato", "killed", "explosion",
           "president", "minister", "election", "central bank", "rate", "oil", "gas", "cable",
           "border", "airspace", "military", "strike", "tariff", "summit"]
    RISKY = ["killed", "dead", "attack", "strike", "claims", "missile", "drone", "explosion"]

    async def assess(self, item: dict[str, Any]) -> Assessment:
        text = f"{item['title']} {item.get('body') or ''}".lower()
        hits = sum(1 for w in self.HOT if w in text)
        importance = min(10, 3 + 2 * hits)
        risky = any(w in text for w in self.RISKY) or item.get("trust") != "high"
        title = re.sub(r"\s+", " ", item["title"]).strip()
        prefix = "JUST IN: " if importance >= 7 else ""
        return Assessment(
            is_news=True, importance=importance, risk="high" if risky else "low",
            category="other", headline=f"{prefix}{title}"[:260],
            reason=f"mock: {hits} keyword hits",
        )


class LimitReached(RuntimeError):
    """The Claude subscription's usage limit is used up for now."""


class ClaudeCLILLM:
    """Runs `claude -p` (Claude Code headless mode) so calls count against your Claude Pro/Max
    usage limits instead of API billing. Needs Claude Code installed and logged in once
    with `claude` -> /login on this machine."""

    LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "limit will reset", "out of extra usage")

    def __init__(self, cli_path: str = "claude", model: str = "haiku", timeout: int = 120, workdir: str = "data"):
        import json
        import shutil
        self._json = json
        self.cli = shutil.which(cli_path) or cli_path
        self.model, self.timeout, self.workdir = model, timeout, workdir
        self.schema = json.dumps(TOOL["input_schema"])

    def _env(self) -> dict[str, str]:
        import os
        env = dict(os.environ)
        # If an API key is in the environment, Claude Code would bill the API instead of the subscription.
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            env.pop(k, None)
        return env

    async def assess(self, item: dict[str, Any]) -> Assessment:
        import asyncio
        import os
        os.makedirs(self.workdir, exist_ok=True)
        args = [self.cli, "-p", "--output-format", "json", "--model", self.model, "--tools", "",
                "--no-session-persistence", "--system-prompt", SYSTEM_PROMPT.replace(
                    "Always answer by calling the submit_assessment tool.", "Answer with the structured output."),
                "--json-schema", self.schema]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=self.workdir, env=self._env())
        except FileNotFoundError:
            raise RuntimeError(f"Claude Code CLI not found ({self.cli}). Install: npm install -g @anthropic-ai/claude-code")
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(build_user_message(item).encode()), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude -p timed out after {self.timeout}s")

        text = out.decode(errors="replace").strip()
        try:
            data = self._json.loads(text)
        except ValueError:
            data = {"is_error": True, "result": text or err.decode(errors="replace")}
        structured = data.get("structured_output")
        if data.get("is_error") or not isinstance(structured, dict):
            msg = str(data.get("result") or err.decode(errors="replace") or "no output")[:300]
            if any(w in msg.lower() for w in self.LIMIT_WORDS):
                raise LimitReached(msg)
            raise RuntimeError(f"claude -p failed: {msg}")
        return Assessment.from_dict(structured)


class FallbackLLM:
    """Try the subscription first; on failure use the API. After hitting the usage limit,
    skip the subscription for `cooldown` seconds."""

    def __init__(self, primary: LLM, fallback: LLM | None, cooldown: int = 900):
        self.primary, self.fallback, self.cooldown = primary, fallback, cooldown
        self._skip_until = 0.0

    async def assess(self, item: dict[str, Any]) -> Assessment:
        import time
        if time.monotonic() >= self._skip_until:
            try:
                return await self.primary.assess(item)
            except LimitReached as e:
                self._skip_until = time.monotonic() + self.cooldown
                log.warning("Subscription limit reached (%s). %s", e,
                            "Using the API for now." if self.fallback else f"Pausing {self.cooldown // 60} min.")
                if not self.fallback:
                    raise
            except Exception as e:  # noqa: BLE001
                if not self.fallback:
                    raise
                log.warning("claude -p failed (%s), using the API for this item", e)
        elif not self.fallback:
            raise LimitReached("subscription limit cooldown")
        return await self.fallback.assess(item)


def llm_from_settings(s) -> LLM:
    import os
    return make_llm(s.llm_provider, s.anthropic_api_key, s.llm_model, s.claude_cli_path, s.claude_cli_model,
                    workdir=os.path.dirname(os.path.abspath(s.db_path)) if s.db_path != ":memory:" else "data")


def make_llm(provider: str, api_key: str, model: str, cli_path: str = "claude",
             cli_model: str = "haiku", workdir: str = "data") -> LLM:
    if provider == "mock":
        return MockLLM()
    if provider == "claude_cli":
        fallback = ClaudeLLM(api_key, model) if api_key else None
        return FallbackLLM(ClaudeCLILLM(cli_path, cli_model, workdir=workdir), fallback)
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY is empty. Set it in .env, or use LLM_PROVIDER=claude_cli "
                         "(your Claude subscription) or LLM_PROVIDER=mock to test.")
    return ClaudeLLM(api_key, model)
