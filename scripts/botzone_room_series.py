#!/usr/bin/env python3
from __future__ import print_function

import argparse
import os
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from botzone_upload_match import (  # noqa: E402
    BotzoneClient,
    BotzoneError,
    DEFAULT_BASE_URL,
    DEFAULT_CAPTCHA_AUTO_ATTEMPTS,
    DEFAULT_CAPTCHA_CHARS,
    DEFAULT_CAPTCHA_MIN_GAP,
    DEFAULT_CAPTCHA_MIN_SCORE,
    DEFAULT_CAPTCHA_RETRY_DELAY,
    DEFAULT_CAPTCHA_RECOGNIZER,
    DEFAULT_COOKIE_FILE,
    DEFAULT_MATCHES_PER_OPPONENT,
    DEFAULT_ROOM_SOURCE_BOT_NAME,
    DEFAULT_RUNS_DIR,
    TEXAS_HOLDEM_2P_GAME_ID,
    command_run_room_series,
    ensure_dir,
    run_stamp,
    slugify,
)


class TeeStream(object):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
        return len(data)

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        return any(getattr(stream, "isatty", lambda: False)() for stream in self.streams)


def default_output_log_path(args):
    name = "room_series_{}_{}.log".format(run_stamp(), slugify(args.bot_name or args.bot_id or "bot", "bot"))
    return os.path.join(os.path.abspath(args.data_dir), "logs", name)


def setup_output_log(args):
    if getattr(args, "no_log", False):
        return None
    path = os.path.abspath(args.log_file or default_output_log_path(args))
    ensure_dir(os.path.dirname(path))
    log_file = open(path, "a", buffering=1)
    stdout = sys.stdout
    stderr = sys.stderr
    sys.stdout = TeeStream(stdout, log_file)
    sys.stderr = TeeStream(stderr, log_file)
    print("Output log: {}".format(path))
    return path, log_file, stdout, stderr


def restore_output_log(state):
    if not state:
        return
    _path, log_file, stdout, stderr = state
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        sys.stdout = stdout
        sys.stderr = stderr
        log_file.close()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run Botzone room matches for one bot and archive match logs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--cookie-file", default=os.environ.get("BOTZONE_COOKIE_FILE", DEFAULT_COOKIE_FILE))
    parser.add_argument("--email", default=os.environ.get("BOTZONE_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("BOTZONE_PASSWORD"))
    parser.add_argument("--bot-name", default="test")
    parser.add_argument("--bot-id")
    parser.add_argument("--game-id", default=TEXAS_HOLDEM_2P_GAME_ID)
    parser.add_argument(
        "--opponent-source",
        action="append",
        choices=("ranked", "history", "all"),
        default=None,
        help="opponent source; repeatable, default all",
    )
    parser.add_argument(
        "--rank-source-bot-name",
        default=DEFAULT_ROOM_SOURCE_BOT_NAME,
        help="optional ranked bot used to read Botzone's rank-match opponent list; default reads the game ranklist",
    )
    parser.add_argument(
        "--rank-source-bot-id",
        help="optional ranked bot id used to read Botzone's rank-match opponent list; default reads the game ranklist",
    )
    parser.add_argument("--opponent-version-id", action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--matches-per-opponent", type=int, default=DEFAULT_MATCHES_PER_OPPONENT)
    parser.add_argument("--data-dir", default=DEFAULT_RUNS_DIR)
    parser.add_argument("--run-dir")
    parser.add_argument("--resume")
    parser.add_argument("--log-file", help="save full terminal output to this file; default is data-dir/logs/room_series_*.log")
    parser.add_argument("--no-log", action="store_true", help="do not tee terminal output to a log file")
    parser.add_argument("--delay", type=float, default=3.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--match-timeout", type=float, default=900.0)
    parser.add_argument("--room-start-timeout", type=float, default=60.0)
    parser.add_argument("--socket-timeout", type=float, default=20.0)
    parser.add_argument("--socket-ready-wait", type=float, default=1.0)
    parser.add_argument("--socket-change-wait", type=float, default=1.0)
    parser.add_argument("--initdata", default="")
    parser.add_argument("--include-own", action="store_true")
    parser.add_argument("--include-disabled", action="store_true")
    parser.add_argument(
        "--captcha-mode",
        choices=("auto", "manual"),
        default="auto",
        help="solve one-character room captcha automatically or prompt manually",
    )
    parser.add_argument("--captcha-recognizer", default=DEFAULT_CAPTCHA_RECOGNIZER)
    parser.add_argument("--captcha-chars", default=DEFAULT_CAPTCHA_CHARS)
    parser.add_argument("--captcha-min-score", type=float, default=DEFAULT_CAPTCHA_MIN_SCORE)
    parser.add_argument("--captcha-min-gap", type=float, default=DEFAULT_CAPTCHA_MIN_GAP)
    parser.add_argument(
        "--no-captcha-try-below-threshold",
        dest="captcha_try_below_threshold",
        action="store_false",
        default=True,
        help="retry instead of submitting an auto captcha guess below score/gap thresholds",
    )
    parser.add_argument("--captcha-attempts", type=int, default=DEFAULT_CAPTCHA_AUTO_ATTEMPTS, help="0 means retry forever")
    parser.add_argument("--captcha-retry-delay", type=float, default=DEFAULT_CAPTCHA_RETRY_DELAY)
    parser.add_argument("--no-open-captcha", action="store_true", help="do not open captcha preview automatically")
    parser.add_argument("--dry-run", action="store_true", help="only resolve bot and opponent list")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    args.execute = not args.dry_run
    args.yes = True
    args.open_captcha = not args.no_open_captcha
    args.execute_hint = "remove --dry-run to create rooms and start matches"

    log_state = setup_output_log(args)
    try:
        client = BotzoneClient(args.base_url, args.cookie_file, args.verbose)
        command_run_room_series(client, args)
        return 0
    except BotzoneError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        restore_output_log(log_state)


if __name__ == "__main__":
    sys.exit(main())
