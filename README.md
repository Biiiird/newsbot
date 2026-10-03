# newsbot: automated news channel MVP

The pipeline goes: sources → dedup → Claude scores and rewrites → you approve in Telegram → it posts to a Telegram channel (and X, optionally).

```
RSS feeds ──┐
            ├─> dedup/cluster ─> Claude (score, risk, headline) ─┬─> dropped
Telegram ───┘        (SQLite)                                    ├─> pending ─> admin bot [Publish|Edit|Reject]
channels                                                         └─> auto-approved          │
                                                                          publisher <───────┘
                                                                 (rate limits) ─> Telegram channel / X
```

Everything runs in one Python process on one SQLite file, so there's no Redis or Postgres to set up yet.

## 1. Try it locally in 2 minutes (no keys)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# in .env set: LLM_PROVIDER=mock
python -m newsbot once --max-age 1440   # fetch feeds, score with the mock, print drafts
python -m newsbot list                  # drafts waiting for approval
python -m newsbot approve 1
python -m newsbot run                   # full loop; DRY_RUN=true prints posts instead of posting
```

## 2. Turn on Claude

### Option A: your Claude Pro subscription (`LLM_PROVIDER=claude_cli`)

The bot calls Claude Code in headless mode (`claude -p`), so usage counts against your Pro limits instead of being billed per request.

1. Install Node.js 18+ and then Claude Code: `npm install -g @anthropic-ai/claude-code`
2. Run `claude` once on the same machine, type `/login`, sign in with your Claude account, then exit.
3. In `.env`, set `LLM_PROVIDER=claude_cli`.
4. Run `python -m newsbot once --max-age 1440`. Each story takes about 10–15 seconds.

Notes:
- The bot removes `ANTHROPIC_API_KEY` from the environment it gives `claude -p`, so those calls always use the subscription.
- When the Pro limit runs out, stories stay queued and the bot retries every 5 minutes. If you also set `ANTHROPIC_API_KEY`, it switches to the API until the limit resets.
- These calls share limits with your normal Claude chat. Anthropic has said it will change how subscriptions cover `claude -p`; see https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
- In Docker, Claude Code isn't in the image. For testing with the subscription, run the bot directly with Python.

### Option B: API key (`LLM_PROVIDER=anthropic`)

1. Create an API key at https://console.anthropic.com (this is separate from a Claude Pro subscription and is billed per use).
2. In `.env`, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=sk-ant-...`.
3. Run `python -m newsbot once --max-age 1440` to see real headlines and scores.

The default model is Haiku 4.5, which is cheap and fast. Each story costs one call of about 1k input tokens and 150 output tokens. To try better headlines, set `LLM_MODEL=claude-sonnet-5`.

## 3. Telegram channel + approval bot

1. In Telegram, open **@BotFather**, send `/newbot`, and copy the token into `TELEGRAM_BOT_TOKEN`.
2. Create a channel, then add the bot as an **admin** with permission to post. Put `@yourchannel` in `TELEGRAM_CHANNEL_ID`.
3. Start the bot with `python -m newsbot run`, open a private chat with it and send `/start`. It replies with your chat id, which goes in `ADMIN_CHAT_ID`. Then restart the bot.
4. Drafts now arrive in that chat with **✅ Publish / ✏️ Edit / ❌ Reject** buttons. Edit asks you for the new text, then queues it.
5. When you're happy with the drafts, set `DRY_RUN=false` to post for real.

Bot commands: `/pending` re-sends the waiting drafts, `/stats` shows counts, `/cancel` stops an edit.

**Pictures.** When a story is about public figures, the post gets their portraits, up to 2 (`MEDIA_MAX_IMAGES`), with the post text as the caption. Portraits are the person's Wikipedia photo, freely licensed images only. Low-quality pictures are skipped: originals under `MEDIA_MIN_SIDE` pixels (600 by default) on the shorter side, and video stills or screenshots. A person with no good picture gets none. Countries only appear as flag emojis in the text, so a story that isn't about a person has no pictures. Each draft lists its images as links so you can check them before publishing. Turn it off with `ATTACH_IMAGES=false`. Pictures go to Telegram only, not X.

## 4. Read Telegram channels (optional)

1. Get `api_id` and `api_hash` at https://my.telegram.org under "API development tools" and put them in `TG_API_ID` and `TG_API_HASH`.
2. Add channels under `telegram:` in `config/sources.yaml`.
3. Run `python -m newsbot telegram-login` once. It asks for a phone number and code, and the session is saved to `data/`. Use a separate account, not your personal one.

## 5. X (optional)

You need a developer account with a write-capable API tier (check current pricing on developer.x.com). Create an app with read+write permissions, generate user access tokens, fill in the `X_*` values in `.env` and set `X_ENABLED=true`. The source link goes in a reply, not the main post.

## 6. Deploy on a VPS

```bash
cp .env.example .env   # fill it in
docker compose up -d --build
docker compose logs -f
# one-time, only if you use the Telegram reader:
docker compose run --rm newsbot python -m newsbot telegram-login
```

## Routing rules (in `.env`)

| Setting | Default | Meaning |
|---|---|---|
| `MIN_IMPORTANCE` | 6 | Stories scored below this (0 to 10) are dropped |
| `AUTO_POST` | false | When true, a story posts without approval if it has score ≥ `AUTO_MIN_IMPORTANCE`, low risk and a `trust: high` source |
| `AUTO_REQUIRE_LOW_RISK` | true | Set to false to also auto-post stories Claude marks high-risk |
| `AUTO_REQUIRE_HIGH_TRUST` | true | Set to false to auto-post from sources of any trust level |
| `MAX_ITEM_AGE_MIN` | 120 | Older items are ignored, so the first run doesn't post old news |
| `MIN_POST_INTERVAL_SEC` / `MAX_POSTS_PER_HOUR` | 120 / 12 | Rate limits; the most important approved post goes first |
| `DEDUP_THRESHOLD` | 85 | Similarity at which two headlines count as the same story |

The house style and scoring rubric are in `SYSTEM_PROMPT` in `newsbot/llm.py`. That prompt is where to tune the channel's voice.

## Tests

```bash
pip install pytest pytest-asyncio
python -m pytest -q
```

The tests run offline. Telegram, Claude and the feeds are all faked.

## Known MVP limits / next steps

- Dedup is fuzzy text matching. It catches re-worded copies but not the same story told differently, or the same story in two languages. The next step is embeddings with pgvector.
- There is no image or chart generation yet.
- Engagement stats aren't pulled back into the database yet (`publish_log` stores the post ids needed to do that).
- SQLite is fine for a single process. Switch to Postgres when you split the workers.
