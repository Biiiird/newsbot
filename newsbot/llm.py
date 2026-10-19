"""Scoring + rewriting with Claude.

One API call per new story. Claude is forced to answer through a tool, so the output is
always structured JSON (no parsing of free text). A second, small call (match_video) checks
whether an official video shows the event a post is about, see video.py.
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
defence, elections and changes of government, sanctions, trade, energy, and also major markets, \
crypto and big tech news.

For each incoming item decide whether it is worth posting and write the post.

Scoring (importance 0-10):
- 9-10: major breaking events: war escalation, heads of state decisions, major attacks, market-moving central bank or \
sanctions news, market crashes or surges, emergency central bank moves
- 7-8: significant government, military, security, diplomatic, trade, energy or economic news with international relevance, \
from any region. Also: Fed/ECB/major central bank decisions, Bitcoin at a new all-time high or moving more than 5% in a day, \
major institutional or government crypto adoption (BlackRock, JPMorgan, a national government), big tech decisions with \
wide impact (Elon Musk, Nvidia, Apple, Microsoft, Google, OpenAI, Anthropic)
- 5-6: notable national politics and security news from any country (elections, government crises, \
political prosecutions, protests with political impact, diplomatic incidents, military appointments), \
smaller regional conflicts
- 0-4: local crime, accidents and disasters without political or international impact, human-interest, \
culture, sport, weather, health (unless an international emergency), opinion, analysis pieces, features, ads, \
routine company crypto purchases, token burns, small price moves and price predictions \
-> set is_news=false if it is not news at all
Be strict: the channel posts only the most influential stories of the day. Ask whether people \
outside the country involved will care. Most items are 5-7. Give 8 only to news with clear \
international impact (a government or leader changing course on a war, a major deal or sanctions, \
a significant attack or military move, a market-moving decision), a few times a day at most. \
Domestic reviews, inquiries, statements without new action, follow-ups and reactions are 6 or lower.

Risk:
- "high" if the item involves casualties, military claims, attacks, accusations against named people, \
unverified or single-source claims, or comes from a low-trust or state-media source
- "low" otherwise

Headline rules (house style):
- Start with "JUST IN:" for fresh breaking items or "BREAKING:" only for importance 9-10; otherwise no prefix
- Then the flag emoji of each country in the countries field, main country first \
(e.g. "🇺🇸🇺🇦"), then the news
- Easy to read for anyone at a glance: usually one short sentence, at most two, under 200 characters \
total. Say who did what, and the key number if there is one. Leave out secondary details
- Simple everyday words a general audience knows, no jargon or diplomatic language. Short titles \
before names, e.g. "President Trump", "Russia's Putin", "Iran's Foreign Minister Araghchi"
- Active voice and present tense for news, e.g. "Israel strikes Beirut." rather than "Strikes were carried out on Beirut by Israel."
- No hashtags, no links, no opinion, no speculation
- Keep exact prices and amounts as the item gives them, with $ and the ticker if it uses one, \
e.g. "Bitcoin reclaims $83,000." or "Strategy buys 267 $BTC worth $22 million."
- Quote with straight double quotes "like this", never single or curly quotes
- Never use semicolons. End the sentence with a period and start a new one instead
- Use ONLY facts stated in the item. Do not add background, context, motives or explanations \
("amid tensions", "signalling...", "a NATO member") that the item does not say
- Never name the news outlet that reported the item ("Al Jazeera reports", "according to Reuters"). \
Attribute claims that are not confirmed facts to whoever makes them, e.g. "Ukraine says ...", \
"Hamas claims ...", "Witnesses say ..."
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

Video: if the news is something a named official said or did on camera at a specific occasion \
(a speech, address, press conference, briefing, interview, summit statement or televised meeting, \
e.g. by a head of state or government, a minister or a spokesperson), set video_moment to a short \
description naming the speaker and the occasion, e.g. "Vladimir Putin speech at the Valdai Discussion \
Club" or "Marco Rubio press conference in Panama City". Otherwise (attacks, reports, decisions \
announced in writing, anything without a filmed occasion) use "".

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
                "technology", "crypto", "society", "other"]},
            "headline": {"type": "string", "description": "The post text in house style."},
            "reason": {"type": "string", "description": "One short sentence explaining the score."},
            "people": {"type": "array", "items": {"type": "string"},
                       "description": "Up to 2 public figures, as English Wikipedia article titles."},
            "countries": {"type": "array", "items": {"type": "string"},
                          "description": "Up to 3 ISO 3166-1 alpha-2 country codes, main country first."},
            "duplicate_of": {"type": "integer",
                             "description": "Number of the earlier post about the same event, or 0."},
            "video_moment": {"type": "string",
                             "description": "Speaker and filmed occasion the news is about, or empty."},
        },
        "required": ["is_news", "importance", "risk", "category", "headline", "reason", "people", "countries",
                     "duplicate_of", "video_moment"],
    },
}

MATCH_PROMPT = """You check whether an official video shows the event a news post is about.

You get the post, the filmed moment it refers to, and a numbered list of videos recently uploaded \
by official channels (channel, upload time, title, description). Answer with the number of the video \
that shows that same occasion: the same speaker at the same speech, briefing, interview or meeting. \
A video of the same person at a different event, or a clip about the same topic by someone else, \
is not a match. If no video clearly matches, or you are unsure, answer 0.

Always answer by calling the submit_match tool."""

MATCH_TOOL = {
    "name": "submit_match",
    "description": "Submit which video, if any, shows the moment from the post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "video": {"type": "integer", "description": "Number of the matching video, or 0 for none."},
            "reason": {"type": "string", "description": "One short sentence."},
        },
        "required": ["video", "reason"],
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
    video_moment: str = ""

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
            video_moment=str(d.get("video_moment") or "").strip()[:200],
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

    async def match_video(self, post: dict[str, Any], videos: list[dict[str, Any]]) -> int:
        """1-based index of the video that shows the post's moment, or 0."""


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


def build_match_message(post: dict[str, Any], videos: list[dict[str, Any]]) -> str:
    parts = [f"Post: {post['headline']}", f"Filmed moment: {post['video_moment']}", "", "Videos:"]
    for i, v in enumerate(videos, 1):
        desc = (v.get("description") or "").strip()
        parts.append(f"{i}. [{v['source']}, uploaded {v.get('published_at') or 'unknown'}] {v['title']}"
                     + (f"\n   {desc[:400]}" if desc else ""))
    return "\n".join(parts)


def match_index(d: dict[str, Any], count: int) -> int:
    n = _int_or_zero(d.get("video"))
    return n if n <= count else 0


class ClaudeLLM:
    def __init__(self, api_key: str, model: str):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic(api_key=api_key, max_retries=3)
        self.model = model

    async def _call(self, system: str, tool: dict[str, Any], user: str) -> dict[str, Any]:
        msg = await self.client.messages.create(
            model=self.model,
            max_tokens=600,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}],
        )
        for block in msg.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError(f"No tool call in Claude response: {msg.content!r}")

    async def assess(self, item: dict[str, Any]) -> Assessment:
        return Assessment.from_dict(await self._call(SYSTEM_PROMPT, TOOL, build_user_message(item)))

    async def match_video(self, post: dict[str, Any], videos: list[dict[str, Any]]) -> int:
        return match_index(await self._call(MATCH_PROMPT, MATCH_TOOL, build_match_message(post, videos)),
                           len(videos))


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

    async def match_video(self, post: dict[str, Any], videos: list[dict[str, Any]]) -> int:
        return 0  # the mock can't judge videos, so it never attaches one


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
        self.match_schema = json.dumps(MATCH_TOOL["input_schema"])

    def _env(self) -> dict[str, str]:
        import os
        env = dict(os.environ)
        # If an API key is in the environment, Claude Code would bill the API instead of the subscription.
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
            env.pop(k, None)
        return env

    async def assess(self, item: dict[str, Any]) -> Assessment:
        return Assessment.from_dict(await self._run(
            SYSTEM_PROMPT.replace("Always answer by calling the submit_assessment tool.",
                                  "Answer with the structured output."),
            self.schema, build_user_message(item)))

    async def match_video(self, post: dict[str, Any], videos: list[dict[str, Any]]) -> int:
        return match_index(await self._run(
            MATCH_PROMPT.replace("Always answer by calling the submit_match tool.",
                                 "Answer with the structured output."),
            self.match_schema, build_match_message(post, videos)), len(videos))

    async def _run(self, system: str, schema: str, user: str) -> dict[str, Any]:
        import asyncio
        import os
        os.makedirs(self.workdir, exist_ok=True)
        args = [self.cli, "-p", "--output-format", "json", "--model", self.model, "--tools", "",
                "--no-session-persistence", "--system-prompt", system, "--json-schema", schema]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=self.workdir, env=self._env())
        except FileNotFoundError:
            raise RuntimeError(f"Claude Code CLI not found ({self.cli}). Install: npm install -g @anthropic-ai/claude-code")
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(user.encode()), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude -p timed out after {self.timeout}s")
        except asyncio.CancelledError:  # shutting down: don't leave claude running
            proc.kill()
            raise

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
        return structured


class FallbackLLM:
    """Try the subscription first; on failure use the API. After hitting the usage limit,
    skip the subscription for `cooldown` seconds."""

    def __init__(self, primary: LLM, fallback: LLM | None, cooldown: int = 900):
        self.primary, self.fallback, self.cooldown = primary, fallback, cooldown
        self._skip_until = 0.0

    async def assess(self, item: dict[str, Any]) -> Assessment:
        return await self._call("assess", item)

    async def match_video(self, post: dict[str, Any], videos: list[dict[str, Any]]) -> int:
        return await self._call("match_video", post, videos)

    async def _call(self, method: str, *args: Any) -> Any:
        import time
        if time.monotonic() >= self._skip_until:
            try:
                return await getattr(self.primary, method)(*args)
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
        return await getattr(self.fallback, method)(*args)


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
