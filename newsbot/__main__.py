"""Command line.

  python -m newsbot run              start everything (Ctrl+C to stop)
  python -m newsbot once             one RSS fetch + scoring pass, prints the drafts (no posting)
  python -m newsbot list [status]    show posts (default: pending_approval)
  python -m newsbot approve <id>     approve a draft from the terminal
  python -m newsbot reject <id>      reject a draft
  python -m newsbot stats            counts
  python -m newsbot telegram-login   one-time login for the Telegram channel reader
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import load_settings
from .db import DB


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="newsbot")
    p.add_argument("--env", default=".env", help="path to .env file")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    op = sub.add_parser("once")
    op.add_argument("--max-age", type=int, default=None,
                    help="minutes; override MAX_ITEM_AGE_MIN to see older items while testing")
    lp = sub.add_parser("list")
    lp.add_argument("status", nargs="?", default="pending_approval")
    for name in ("approve", "reject"):
        sp = sub.add_parser(name)
        sp.add_argument("id", type=int)
    sub.add_parser("stats")
    sub.add_parser("telegram-login")
    args = p.parse_args(argv)

    settings = load_settings(args.env)
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "run":
        from .runner import run_all
        asyncio.run(run_all(settings))

    elif args.cmd == "once":
        if args.max_age is not None:
            settings.max_item_age_min = args.max_age
        asyncio.run(_once(settings))

    elif args.cmd == "list":
        db = DB(settings.db_path)
        rows = db.posts_by_status(args.status, 100)
        if not rows:
            print(f"no posts with status {args.status}")
        for r in rows:
            print(f"#{r['id']:<4} [{r['importance']}/{r['risk']:<4}] {r['headline']}\n       {r['source_name']}: {r['source_url']}")

    elif args.cmd in ("approve", "reject"):
        from .approval_bot import apply_decision
        ok, msg = apply_decision(DB(settings.db_path), args.id, args.cmd)
        print(msg)
        sys.exit(0 if ok else 1)

    elif args.cmd == "stats":
        print(DB(settings.db_path).stats())

    elif args.cmd == "telegram-login":
        if not (settings.tg_api_id and settings.tg_api_hash):
            sys.exit("Set TG_API_ID and TG_API_HASH in .env first (https://my.telegram.org)")
        from .ingest.telegram_reader import login
        asyncio.run(login(settings))


async def _once(settings) -> None:
    from .ingest import rss
    from .llm import llm_from_settings
    from .processor import process_once, resolver_from_settings

    db = DB(settings.db_path)
    llm = llm_from_settings(settings)
    resolver = resolver_from_settings(settings)
    counts = await rss.poll_once(db, settings)
    print(f"ingest: {dict(counts)}")
    total = {}
    while True:
        c = await process_once(db, settings, llm, limit=10, resolver=resolver)
        if not c:
            break
        if c.get("waiting_for_limit"):
            print("Claude usage limit reached: remaining stories stay queued for the next run")
            break
        for k, v in c.items():
            total[k] = total.get(k, 0) + v
    print(f"processed: {total}")
    for status in ("approved", "pending_approval"):
        for r in db.posts_by_status(status, 100):
            print(f"  #{r['id']:<4} {status:<16} [{r['importance']}/{r['risk']}] {r['headline']}")


if __name__ == "__main__":
    main()
