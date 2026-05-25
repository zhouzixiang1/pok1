#!/usr/bin/env python3
from __future__ import print_function

import argparse
import os
import sys
from types import SimpleNamespace


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from botzone_upload_match import (  # noqa: E402
    BotzoneClient,
    BotzoneError,
    DEFAULT_BASE_URL,
    TEXAS_HOLDEM_2P_GAME_ID,
    command_upload,
    fetch_bot_detail,
    parse_bot_list,
)


# CONFIG: edit this section for account, cookie, bot source, and upload target.
CONFIG = {
    "base_url": DEFAULT_BASE_URL,
    "game_id": TEXAS_HOLDEM_2P_GAME_ID,
    "extension": "py36",
    "uploads": [
        {
            "enabled": True,
            "email": "tydfxt1@tydfxt.top",
            "password": "123456",
            "cookie_file": "temp/botzone_tydfxt1_cookies.txt",
            "source": "bots/bot18/main.py",
            "bot_name": "bot18",
            "description": "bot18 match-control",
            "join_rank_on_create": True,
        },
        {
            "enabled": True,
            "email": "tydfxt2@tydfxt.top",
            "password": "123456",
            "cookie_file": "temp/botzone_tydfxt2_cookies.txt",
            "source": "bots/bot19/main.py",
            "bot_name": "bot19",
            "description": "bot19 static knowledge",
            "join_rank_on_create": True,
        },
        {
            "enabled": False,
            "email": "tydfxt3@tydfxt.top",
            "password": "123456",
            "cookie_file": "temp/botzone_tydfxt3_cookies.txt",
            "source": "bots/bot18/main.py",
            "bot_name": "bot18_test",
            "description": "reserved test bot",
            "join_rank_on_create": True,
        },
    ],
}


def abs_project_path(path):
    if os.path.isabs(path):
        return path
    return os.path.join(PROJECT_DIR, path)


def build_upload_args(item, execute, yes, verbose):
    return SimpleNamespace(
        email=item["email"],
        password=item["password"],
        cookie_file=abs_project_path(item["cookie_file"]),
        source=abs_project_path(item["source"]),
        bot_name=item.get("bot_name"),
        bot_id=item.get("bot_id"),
        description=item.get("description"),
        game_id=item.get("game_id", CONFIG["game_id"]),
        extension=item.get("extension", CONFIG["extension"]),
        create_new=bool(item.get("_create_new", False)),
        join_rank=bool(item.get("_join_rank", False)),
        keep_running=bool(item.get("keep_running", False)),
        simpleio=bool(item.get("simpleio", False)),
        opensource=bool(item.get("opensource", False)),
        rank_match=bool(item.get("rank_match", False)),
        opponent_bot_id=item.get("opponent_bot_id"),
        opponent_name=item.get("opponent_name"),
        execute=execute,
        yes=yes,
        verbose=verbose,
    )


def choose_existing_bot(client, bot_name, game_id):
    matches = [
        bot for bot in parse_bot_list(client.get_text("/mybots"))
        if bot.get("name") == bot_name and bot.get("game_id") == game_id
    ]
    if not matches:
        return None
    enriched = []
    for bot in matches:
        try:
            detail = fetch_bot_detail(client, bot["id"])
            bot = dict(bot)
            bot["_ranked"] = bool(detail.get("ranked"))
            bot["_version_count"] = len(detail.get("versions") or [])
        except Exception:
            bot = dict(bot)
            bot["_ranked"] = False
            bot["_version_count"] = 0
        enriched.append(bot)
    enriched.sort(key=lambda bot: (not bot.get("_ranked"), -bot.get("_version_count", 0), bot.get("id", "")))
    if len(enriched) > 1:
        print(
            "WARN: found {} bots named {!r}; using {} (ranked={}, versions={})".format(
                len(enriched),
                bot_name,
                enriched[0].get("id"),
                enriched[0].get("_ranked"),
                enriched[0].get("_version_count"),
            ),
            file=sys.stderr,
        )
    return enriched[0]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Upload configured bots across multiple Botzone accounts.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--execute", action="store_true", help="perform uploads; without this, dry-run only")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompts")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    exit_code = 0
    for index, item in enumerate(CONFIG["uploads"], 1):
        if not item.get("enabled", False):
            print("[{}/{}] SKIP disabled: {}".format(index, len(CONFIG["uploads"]), item.get("email")))
            continue
        upload_args = build_upload_args(item, args.execute, args.yes, args.verbose)
        client = BotzoneClient(CONFIG["base_url"], upload_args.cookie_file, args.verbose)
        print("[{}/{}] {} -> {} from {}".format(
            index,
            len(CONFIG["uploads"]),
            item["email"],
            item.get("bot_name") or item.get("bot_id"),
            item["source"],
        ))
        try:
            client.ensure_login(item.get("email"), item.get("password"))
            existing = None
            if item.get("bot_id"):
                existing = {"id": item.get("bot_id"), "name": item.get("bot_name") or item.get("bot_id")}
            elif item.get("bot_name"):
                existing = choose_existing_bot(
                    client,
                    item.get("bot_name"),
                    item.get("game_id", CONFIG["game_id"]),
                )
            if existing:
                upload_args.bot_id = existing["id"]
                upload_args.create_new = False
                upload_args.join_rank = False
                print("Updating existing bot {} ({})".format(existing.get("name"), existing.get("id")))
            else:
                upload_args.create_new = True
                upload_args.join_rank = bool(item.get("join_rank_on_create", True))
                print("Creating new bot {}; join_rank={}".format(item.get("bot_name"), upload_args.join_rank))
            command_upload(client, upload_args)
        except BotzoneError as exc:
            exit_code = 1
            print("ERROR for {}: {}".format(item["email"], exc), file=sys.stderr)
        except Exception as exc:
            exit_code = 1
            print("ERROR for {}: {}".format(item["email"], exc), file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
