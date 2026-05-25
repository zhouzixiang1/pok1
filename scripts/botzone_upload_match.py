#!/usr/bin/env python3
from __future__ import print_function

import argparse
import base64
import csv
import getpass
import html
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BASE_URL = "https://www.botzone.org.cn"
TEXAS_HOLDEM_2P_GAME_ID = "63dcfaddee1bce5e6c8f4b53"
DEFAULT_COOKIE_FILE = os.path.join(PROJECT_DIR, "temp", "botzone_cookies.txt")
DEFAULT_CAPTCHA_FILE = os.path.join(PROJECT_DIR, "temp", "botzone_digit_captcha.svg")
DEFAULT_CAPTCHA_RECOGNIZER = os.path.join(PROJECT_DIR, "temp", "recognize_svg_char.py")
DEFAULT_CAPTCHA_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
DEFAULT_CAPTCHA_AUTO_ATTEMPTS = 0
DEFAULT_CAPTCHA_RETRY_DELAY = 2.0
DEFAULT_CAPTCHA_MIN_SCORE = 0.40
DEFAULT_CAPTCHA_MIN_GAP = 0.035
DEFAULT_ROOM_RESPONSE_FILE = os.path.join(PROJECT_DIR, "temp", "botzone_create_room_response.html")
DEFAULT_RUNS_DIR = os.path.join(PROJECT_DIR, "botzone_runs")
DEFAULT_ROOM_SOURCE_BOT_NAME = ""
DEFAULT_MATCHES_PER_OPPONENT = 100
MAX_DIRECT_UPLOAD_BYTES = 4000000

MATCH_CSV_FIELDS = [
    "planned_match_index",
    "opponent_index",
    "opponent_match_index",
    "matches_per_opponent",
    "match_id",
    "match_url",
    "started_at",
    "finished_at",
    "my_bot",
    "my_version",
    "my_version_id",
    "opponent",
    "opponent_version",
    "opponent_version_id",
    "result",
    "chip_delta",
    "my_win_games",
    "opp_win_games",
    "my_decisions",
    "opp_decisions",
    "my_avg_ms",
    "my_max_ms",
    "my_max_mem_kb",
    "opp_avg_ms",
    "opp_max_ms",
    "opp_max_mem_kb",
    "status",
    "error",
]


class BotzoneError(Exception):
    pass


class CaptchaRejectedError(BotzoneError):
    pass


class CaptchaRecognitionError(BotzoneError):
    pass


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)


def decode_body(body):
    return body.decode("utf-8-sig", "replace")


def strip_tags(text):
    text = re.sub(r"<script\b.*?</script>", "", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", "", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(re.sub(r"\s+", " ", text).strip())


def parse_attrs(tag):
    attrs = {}
    attr_re = re.compile(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))")
    for match in attr_re.finditer(tag):
        key = match.group(1).lower()
        value = match.group(2)
        if value is None:
            value = match.group(3)
        if value is None:
            value = match.group(4)
        attrs[key] = html.unescape(value or "")
    for key in ("checked", "disabled", "selected"):
        if re.search(r"(?:^|[\s<]){}(?:[\s>/]|$)".format(key), tag, flags=re.I):
            attrs[key] = key
    return attrs


def boolish(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on", "checked")


def short(text, limit=300):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def write_captcha_preview(path, body):
    ensure_dir(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(body)
    wrapper_path = os.path.splitext(path)[0] + ".html"
    name = os.path.basename(path)
    html_body = (
        "<!doctype html><meta charset='utf-8'>"
        "<title>Botzone captcha</title>"
        "<body style='font-family:sans-serif;padding:24px'>"
        "<p>Type the one character shown below into the waiting terminal. "
        "Do not refresh this page.</p>"
        "<img src='{name}' style='width:240px;height:320px;"
        "image-rendering:pixelated;border:1px solid #ccc'>"
        "</body>"
    ).format(name=html.escape(name, quote=True))
    with open(wrapper_path, "w") as f:
        f.write(html_body)
    return wrapper_path


def parse_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_captcha_candidates(candidates, limit=3):
    parts = []
    for row in (candidates or [])[:limit]:
        ch = row.get("char", "")
        score = parse_float(row.get("score"))
        if score is None:
            parts.append(str(ch))
        else:
            parts.append("{}:{:.3f}".format(ch, score))
    return ", ".join(parts)


def recognize_captcha_file(svg_path, args):
    recognizer = getattr(args, "captcha_recognizer", None) or DEFAULT_CAPTCHA_RECOGNIZER
    recognizer = os.path.abspath(recognizer)
    if not os.path.exists(recognizer):
        raise CaptchaRecognitionError("captcha recognizer not found: {}".format(recognizer))
    chars = getattr(args, "captcha_chars", None) or DEFAULT_CAPTCHA_CHARS
    cmd = [
        sys.executable,
        recognizer,
        svg_path,
        "--json",
        "--chars",
        chars,
        "--top",
        "8",
    ]
    try:
        output = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
    except subprocess.CalledProcessError as exc:
        raise CaptchaRecognitionError("captcha recognizer failed: {}".format(short(exc.output)))
    except OSError as exc:
        raise CaptchaRecognitionError("captcha recognizer failed: {}".format(exc))
    try:
        data = json.loads(output)
    except ValueError:
        raise CaptchaRecognitionError("captcha recognizer returned non-JSON: {}".format(short(output)))
    candidates = data.get("candidates") or []
    char = data.get("char") or (candidates[0].get("char") if candidates else "")
    if not isinstance(char, str) or not re.match(r"^\S$", char):
        raise CaptchaRecognitionError("captcha recognizer returned invalid char: {!r}".format(char))
    threshold_warnings = []
    try_below_threshold = boolish(getattr(args, "captcha_try_below_threshold", False))
    top = candidates[0] if candidates else {}
    score = parse_float(top.get("score"), 1.0 if top.get("method") == "text-node" else None)
    min_score = parse_float(getattr(args, "captcha_min_score", None), DEFAULT_CAPTCHA_MIN_SCORE)
    if score is not None and min_score is not None and score < min_score:
        warning = "captcha score {:.4f} below minimum {:.4f}; top={}".format(
            score,
            min_score,
            format_captcha_candidates(candidates),
        )
        if try_below_threshold:
            threshold_warnings.append(warning)
        else:
            raise CaptchaRecognitionError(warning)
    score_gap = None
    if len(candidates) >= 2 and score is not None:
        second_score = parse_float(candidates[1].get("score"))
        if second_score is not None:
            score_gap = score - second_score
    min_gap = parse_float(getattr(args, "captcha_min_gap", None), DEFAULT_CAPTCHA_MIN_GAP)
    if score_gap is not None and min_gap is not None and score_gap < min_gap:
        warning = "captcha score gap {:.4f} below minimum {:.4f}; top={}".format(
            score_gap,
            min_gap,
            format_captcha_candidates(candidates),
        )
        if try_below_threshold:
            threshold_warnings.append(warning)
        else:
            raise CaptchaRecognitionError(warning)
    return {
        "char": char,
        "score": score,
        "score_gap": score_gap,
        "candidates": candidates,
        "recognizer": recognizer,
        "threshold_warnings": threshold_warnings,
    }


def open_captcha_preview(path):
    try:
        subprocess.call(["open", path])
    except Exception:
        pass


def write_text(path, text):
    ensure_dir(os.path.dirname(path))
    with open(path, "w") as f:
        f.write(text)


def write_json(path, data):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def read_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def append_jsonl(path, data):
    ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def write_csv(path, rows, fields):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def append_csv_row(path, row, fields):
    ensure_dir(os.path.dirname(path))
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    if exists:
        try:
            with open(path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, [])
            if header != list(fields):
                existing_rows = []
                with open(path, newline="", encoding="utf-8") as f:
                    for existing in csv.DictReader(f):
                        existing_rows.append(existing)
                write_csv(path, existing_rows, fields)
        except Exception:
            pass
        exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run_stamp():
    return time.strftime("%Y%m%d_%H%M%S")


def slugify(text, fallback="item"):
    text = re.sub(r"[^\w.-]+", "_", text or "", flags=re.U).strip("._")
    if not text:
        text = fallback
    return text[:80]


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def get_nested(data, path, default=None):
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def extract_js_value_after(text, name):
    marker = re.search(r"(?:\bvar\s+)?\b{}\b\s*=".format(re.escape(name)), text)
    if not marker:
        return None
    start = marker.end()
    while start < len(text) and text[start].isspace():
        start += 1

    depth = 0
    quote = None
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch in ("{", "[", "("):
            depth += 1
        elif ch in ("}", "]", ")"):
            depth -= 1
        elif ch == ";" and depth == 0:
            return text[start:idx].strip()
    return None


def extract_js_object_after(text, name):
    marker = re.search(r"\b{}\b\s*=".format(re.escape(name)), text)
    if not marker:
        return None
    start = text.find("{", marker.end())
    if start < 0:
        return None

    depth = 0
    quote = None
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1]
    return None


def extract_room_id_from_page(body):
    ready_text = extract_js_object_after(body, "readyMessage")
    if ready_text:
        try:
            ready = json.loads(ready_text)
            for key in ("roomid", "room", "id", "_id"):
                room_id = ready.get(key)
                if isinstance(room_id, str) and room_id:
                    return room_id
        except ValueError:
            pass

    patterns = [
        r"/gametable/join/([0-9a-f]{24})",
        r"\broomid\b\s*[:=]\s*['\"]([0-9a-f]{24})['\"]",
        r"\broom\b\s*[:=]\s*['\"]([0-9a-f]{24})['\"]",
        r"\bid\b\s*[:=]\s*['\"]([0-9a-f]{24})['\"]",
    ]
    for pattern in patterns:
        match = re.search(pattern, body, flags=re.I)
        if match:
            return match.group(1)
    return None


class BotzoneClient(object):
    def __init__(self, base_url, cookie_file=None, verbose=False):
        self.base_url = base_url.rstrip("/")
        self.cookie_file = cookie_file
        self.verbose = verbose
        self.cookies = http.cookiejar.MozillaCookieJar(cookie_file) if cookie_file else http.cookiejar.CookieJar()
        if cookie_file and os.path.exists(cookie_file):
            try:
                self.cookies.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def save_cookies(self):
        if self.cookie_file and hasattr(self.cookies, "save"):
            ensure_dir(os.path.dirname(self.cookie_file))
            self.cookies.save(ignore_discard=True, ignore_expires=True)

    def cookie_header(self):
        pairs = []
        for cookie in self.cookies:
            pairs.append("{}={}".format(cookie.name, cookie.value))
        return "; ".join(pairs)

    def url(self, path):
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def request(self, method, path, data=None, ajax=False, timeout=30, binary=False):
        url = self.url(path)
        headers = {
            "User-Agent": "pok-botzone-script/1.0",
            "Accept": "application/json, text/javascript, */*; q=0.01" if ajax else "*/*",
            "Referer": self.base_url + "/",
        }
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
        req = urllib.request.Request(url, data=body, headers=headers)
        req.get_method = lambda: method.upper()
        if self.verbose:
            print("{} {}".format(method.upper(), url), file=sys.stderr)
        try:
            resp = self.opener.open(req, timeout=timeout)
            payload = resp.read()
            self.save_cookies()
            result = {
                "status": resp.getcode(),
                "url": resp.geturl(),
                "headers": resp.headers,
                "body": payload if binary else decode_body(payload),
            }
            return result
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            message = decode_body(payload)
            raise BotzoneError("HTTP {} for {}: {}".format(exc.code, url, short(message)))
        except urllib.error.URLError as exc:
            raise BotzoneError("Network error for {}: {}".format(url, exc))

    def raw_request(self, method, path, body=None, headers=None, timeout=30, binary=False):
        url = self.url(path)
        req_headers = {
            "User-Agent": "pok-botzone-script/1.0",
            "Accept": "*/*",
            "Referer": self.base_url + "/",
        }
        if headers:
            req_headers.update(headers)
        raw_body = None
        if body is not None:
            raw_body = body if isinstance(body, bytes) else body.encode("utf-8")
        req = urllib.request.Request(url, data=raw_body, headers=req_headers)
        req.get_method = lambda: method.upper()
        if self.verbose:
            print("{} {}".format(method.upper(), url), file=sys.stderr)
        try:
            resp = self.opener.open(req, timeout=timeout)
            payload = resp.read()
            self.save_cookies()
            return {
                "status": resp.getcode(),
                "url": resp.geturl(),
                "headers": resp.headers,
                "body": payload if binary else decode_body(payload),
            }
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            message = decode_body(payload)
            raise BotzoneError("HTTP {} for {}: {}".format(exc.code, url, short(message)))
        except urllib.error.URLError as exc:
            raise BotzoneError("Network error for {}: {}".format(url, exc))

    def get_text(self, path):
        return self.request("GET", path)["body"]

    def get_binary(self, path):
        return self.request("GET", path, binary=True)["body"]

    def post_json(self, path, data):
        resp = self.request("POST", path, data=data, ajax=True)
        text = resp["body"]
        try:
            return json.loads(text)
        except ValueError:
            raise BotzoneError("Expected JSON from {}, got: {}".format(self.url(path), short(text)))

    def has_logged_in_session(self):
        try:
            page = self.get_text("/mybots")
        except BotzoneError:
            return False
        return "Botzone.loggedIn = true" in page and 'id="frmLogin"' not in page

    def ensure_login(self, email=None, password=None):
        if self.has_logged_in_session():
            if self.verbose:
                print("Reusing Botzone session cookie.", file=sys.stderr)
            return
        if not email:
            email = os.environ.get("BOTZONE_EMAIL")
        if not password:
            password = os.environ.get("BOTZONE_PASSWORD")
        if not email:
            raise BotzoneError("Missing Botzone email. Pass --email or set BOTZONE_EMAIL.")
        if not password:
            if sys.stdin.isatty():
                password = getpass.getpass("Botzone password: ")
            else:
                raise BotzoneError("Missing Botzone password. Pass --password or set BOTZONE_PASSWORD.")
        result = self.post_json("/login", {"email": email, "password": password})
        if result.get("success") is False:
            raise BotzoneError("Login failed: {}".format(result.get("message", result)))
        if not self.has_logged_in_session():
            raise BotzoneError("Login request returned success, but /mybots is still not logged in.")


def parse_bot_list(page):
    starts = []
    for match in re.finditer(r"<a\b[^>]*>", page, flags=re.I | re.S):
        attrs = parse_attrs(match.group(0))
        classes = attrs.get("class", "")
        if "botlistitem" in classes.split():
            starts.append((match.start(), match.end(), attrs))

    bots = []
    for idx, (start, end, attrs) in enumerate(starts):
        next_start = starts[idx + 1][0] if idx + 1 < len(starts) else len(page)
        segment = page[end:next_start]
        name_match = re.search(r"<h4[^>]*>(.*?)</h4>", segment, flags=re.I | re.S)
        desc_match = re.search(r"<p[^>]*class=\"[^\"]*\bbotdesc\b[^\"]*\"[^>]*>(.*?)</p>", segment, flags=re.I | re.S)
        version_match = re.search(r"<p[^>]*class=\"[^\"]*\bbotversion\b[^\"]*\"[^>]*>.*?<span[^>]*>(.*?)</span>", segment, flags=re.I | re.S)
        score_match = re.search(r"<span[^>]*class=\"[^\"]*\brankscore\b[^\"]*\"[^>]*>(.*?)</span>", segment, flags=re.I | re.S)
        bot = {
            "id": attrs.get("data-botid") or attrs.get("data-id") or "",
            "name": strip_tags(name_match.group(1)) if name_match else "",
            "game_id": attrs.get("data-gameid") or "",
            "extension": attrs.get("data-ext") or "",
            "description": strip_tags(desc_match.group(1)) if desc_match else "",
            "version": strip_tags(version_match.group(1)) if version_match else "",
            "score_text": strip_tags(score_match.group(1)) if score_match else "",
            "opensource": boolish(attrs.get("data-opensrc")),
            "enable_keep_running": boolish(attrs.get("data-enablekeeprunning")),
            "simpleio": boolish(attrs.get("data-simpleio")),
        }
        if bot["id"] and bot["name"]:
            bots.append(bot)
    return bots


def resolve_bot(client, bot_name=None, bot_id=None, game_id=None):
    bots = parse_bot_list(client.get_text("/mybots"))
    if bot_id:
        for bot in bots:
            if bot["id"] == bot_id:
                return bot, bots
        return {"id": bot_id, "name": bot_name or bot_id, "game_id": game_id or ""}, bots

    matches = []
    for bot in bots:
        if bot["name"] == bot_name and (not game_id or bot["game_id"] == game_id):
            matches.append(bot)
    if len(matches) == 1:
        return matches[0], bots
    if len(matches) > 1:
        raise BotzoneError("Multiple bots named {!r}; pass --bot-id.".format(bot_name))
    available = [bot["name"] for bot in bots if not game_id or bot["game_id"] == game_id]
    raise BotzoneError("Bot {!r} not found. Available bots for this game: {}".format(bot_name, ", ".join(available) or "(none)"))


def print_bots(bots):
    if not bots:
        print("No bots found.")
        return
    for bot in bots:
        print("{name}\t{id}\tver={version}\tgame={game}\text={ext}\tscore={score}".format(
            name=bot["name"],
            id=bot["id"],
            version=bot["version"] or "?",
            game=bot["game_id"] or "?",
            ext=bot["extension"] or "?",
            score=bot["score_text"] or "-",
        ))


def get_json(client, path):
    text = client.get_text(path)
    try:
        return json.loads(text)
    except ValueError:
        raise BotzoneError("Expected JSON from {}, got: {}".format(client.url(path), short(text)))


def fetch_bot_detail(client, bot_id):
    result = get_json(client, "/mybots/detail/{}".format(bot_id))
    if result.get("success") is False:
        raise BotzoneError("Bot detail failed: {}".format(result.get("message", result)))
    return result.get("bot", result)


def normalize_bot_version(bot):
    versions = bot.get("versions") or []
    latest_index = len(versions) - 1
    latest = versions[latest_index] if versions else ""
    latest_id = latest.get("_id", "") if isinstance(latest, dict) else latest
    user = bot.get("user") or {}
    if not isinstance(user, dict):
        user = {"name": str(user)}
    return {
        "bot_id": bot.get("_id", ""),
        "bot_name": bot.get("name", ""),
        "version": latest_index if latest_index >= 0 else "",
        "version_id": latest_id,
        "score": bot.get("score", ""),
        "ranked": bot.get("ranked", False),
        "desc": bot.get("desc", ""),
        "user_id": user.get("_id", ""),
        "user_name": user.get("name", ""),
    }


def print_room_bot_rows(title, rows, json_output=False):
    if json_output:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    print(title)
    print("In a game table, use version_id with the room UI's select-by-ID path.")
    if not rows:
        print("No bot versions found.")
        return
    for index, row in enumerate(rows, 1):
        rank = "ranked" if row.get("ranked") else "unranked"
        print("{idx:>2}. {name}\tversion_id={vid}\tbot_id={bid}\tver={ver}\tscore={score}\t{rank}\tuser={user}".format(
            idx=index,
            name=row.get("bot_name", ""),
            vid=row.get("version_id", ""),
            bid=row.get("bot_id", ""),
            ver=row.get("version", ""),
            score=row.get("score", ""),
            rank=rank,
            user=row.get("user_name", ""),
        ))


def confirm_or_skip(args, label, detail):
    if not getattr(args, "execute", False):
        print("DRY RUN: {}".format(detail))
        print("Add --execute to perform this action.")
        return False
    if getattr(args, "yes", False):
        return True
    if not sys.stdin.isatty():
        raise BotzoneError("Refusing to {} without --yes in a non-interactive shell.".format(label.lower()))
    answer = input("Type {} to continue: ".format(label))
    if answer != label:
        print("Cancelled.")
        return False
    return True


def compact_static_source_for_upload(source):
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "STATIC_VERSION = \"bot19-static-v1\"" not in text:
        return None
    pattern = r"_B19_BOARD5_PAYLOAD\s*=\s*\(\n(?:    b.*\n)+\)"
    compact, n = re.subn(pattern, "_B19_BOARD5_PAYLOAD = (b'')", text, count=1)
    if n != 1:
        return None
    marker = "STATIC_VERSION = \"bot19-static-v1\""
    compact = compact.replace(marker, "STATIC_VERSION = \"bot19-static-v1-compact\"", 1)
    return compact.encode("utf-8")


def upload_plain(client, args, bot_id):
    source_path = os.path.abspath(args.source)
    with open(source_path, "rb") as f:
        source = f.read()
    print("Source size: {} bytes".format(len(source)))
    if len(source) > MAX_DIRECT_UPLOAD_BYTES:
        raise BotzoneError("Direct code upload is limited to {} bytes by this script.".format(MAX_DIRECT_UPLOAD_BYTES))
    description = args.description
    if description is None:
        description = "upload {} {}".format(os.path.basename(source_path), time.strftime("%Y-%m-%d %H:%M:%S"))
    if len(description) > 100:
        raise BotzoneError("Description is longer than Botzone's 100 character limit.")

    payload = [
        ("id", bot_id or ""),
        ("name", args.bot_name or ""),
        ("game", args.game_id),
        ("description", description),
        ("code", base64.b64encode(source).decode("ascii")),
        ("extension", args.extension),
    ]
    if args.keep_running:
        payload.append(("enable_keep_running", "on"))
    if args.simpleio:
        payload.append(("simpleio", "on"))
    if args.opensource:
        payload.append(("opensource", "on"))

    detail = "would upload {} bytes from {} to bot {!r} ({})".format(
        len(source), source_path, args.bot_name or bot_id, bot_id or "new bot"
    )
    if not confirm_or_skip(args, "UPLOAD", detail):
        return None
    try:
        result = client.post_json("/mybots/create_plain", payload)
    except BotzoneError as exc:
        if len(source) <= 1000000 or "HTTP 413" not in str(exc):
            raise
        result = {"success": False, "message": str(exc)}
    if result.get("success") is False and len(source) > 1000000:
        compact_source = compact_static_source_for_upload(source)
        if compact_source is not None and len(compact_source) < 1000000:
            print("Large upload rejected; retrying compact bot19 static source ({} bytes).".format(len(compact_source)))
            payload = list(payload)
            for idx, item in enumerate(payload):
                if item[0] == "description":
                    payload[idx] = ("description", (item[1][:88] + " compact")[:100])
                elif item[0] == "code":
                    payload[idx] = ("code", base64.b64encode(compact_source).decode("ascii"))
            result = client.post_json("/mybots/create_plain", payload)
    if result.get("success") is False:
        raise BotzoneError("Upload failed: {}".format(result.get("message", result)))
    return result


def parse_curr_bot(page):
    match = re.search(r"var\s+currBot\s*=\s*(\{.*?\});", page, flags=re.S)
    if not match:
        raise BotzoneError("Could not find currBot on rank-match page.")
    return json.loads(match.group(1))


def parse_rank_candidates(page):
    table_match = re.search(r"<table\b[^>]*id=[\"']tabBots[\"'][^>]*>(.*?)</table>", page, flags=re.I | re.S)
    table = table_match.group(1) if table_match else page
    candidates = []
    for row_match in re.finditer(r"<tr\b([^>]*)>(.*?)</tr>", table, flags=re.I | re.S):
        row_attrs = parse_attrs("<tr{}>".format(row_match.group(1)))
        row = row_match.group(2)
        input_match = re.search(r"<input\b[^>]*type=[\"']checkbox[\"'][^>]*>", row, flags=re.I | re.S)
        if not input_match:
            continue
        input_attrs = parse_attrs(input_match.group(0))
        bot_id = input_attrs.get("name", "")
        name_match = re.search(r"<a\b[^>]*class=[\"'][^\"']*\bbotname\b[^\"']*[\"'][^>]*>(.*?)</a>", row, flags=re.I | re.S)
        name_html = name_match.group(1) if name_match else ""
        name_html = re.sub(r"<span\b[^>]*class=[\"'][^\"']*\bversion\b[^\"']*[\"'][^>]*>.*?</span>", "", name_html, flags=re.I | re.S)
        version_match = re.search(r"<span\b[^>]*class=[\"'][^\"']*\bversion\b[^\"']*[\"'][^>]*>(.*?)</span>", row, flags=re.I | re.S)
        score_match = re.search(r"排名分[:：]\s*([0-9.]+)", row)
        if not score_match:
            td_matches = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.I | re.S)
            if len(td_matches) > 1:
                score_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", strip_tags(td_matches[1]))
        victory_match = re.search(r"victory-increase.*?<span[^>]*class=[\"'][^\"']*\bamount\b[^\"']*[\"'][^>]*>(.*?)</span>", row, flags=re.I | re.S)
        defeated_match = re.search(r"defeated-decrease.*?<span[^>]*class=[\"'][^\"']*\bamount\b[^\"']*[\"'][^>]*>(.*?)</span>", row, flags=re.I | re.S)
        row_classes = row_attrs.get("class", "").split()
        candidate = {
            "id": bot_id,
            "name": strip_tags(name_html),
            "version": strip_tags(version_match.group(1)) if version_match else "",
            "score": score_match.group(1) if score_match else "",
            "victory_increase": strip_tags(victory_match.group(1)) if victory_match else "",
            "defeated_decrease": strip_tags(defeated_match.group(1)) if defeated_match else "",
            "selected": ("selected" in row_classes) or boolish(input_attrs.get("checked")),
            "disabled": "disabled" in row_classes or boolish(input_attrs.get("disabled")),
        }
        if candidate["id"]:
            candidates.append(candidate)
    return candidates


def parse_game_ranklist_page_paths(page, game_id):
    base_path = "/game/ranklist/{}".format(game_id)
    page_numbers = set([0])
    pattern = r"href=[\"']([^\"']*?/game/ranklist/{}(?:\?[^\"']*)?)[\"']".format(re.escape(game_id))
    for href in re.findall(pattern, page, flags=re.I):
        href = html.unescape(href)
        parsed = urllib.parse.urlsplit(href)
        if parsed.path != base_path:
            continue
        query = urllib.parse.parse_qs(parsed.query)
        values = query.get("page") or []
        if not values:
            continue
        try:
            page_numbers.add(int(values[0]))
        except (TypeError, ValueError):
            continue
    page_numbers = sorted(n for n in page_numbers if n >= 0)
    paths = [base_path]
    for page_number in page_numbers:
        if page_number == 0:
            continue
        paths.append("{}?page={}".format(base_path, page_number))
        if len(paths) >= 20:
            break
    return paths


def parse_game_ranklist_rows(page):
    table_match = re.search(
        r"<table\b[^>]*class=[\"'][^\"']*\btable-rank-effect\b[^\"']*[\"'][^>]*>(.*?)</table>",
        page,
        flags=re.I | re.S,
    )
    table = table_match.group(1) if table_match else page
    rows = []
    for row_match in re.finditer(r"<tr\b([^>]*)>(.*?)</tr>", table, flags=re.I | re.S):
        attrs = parse_attrs("<tr{}>".format(row_match.group(1)))
        bot_id = attrs.get("data-botid") or attrs.get("data-id") or ""
        if not bot_id:
            continue
        row = row_match.group(2)
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.I | re.S)
        if len(cells) < 6:
            continue

        user_id = ""
        user_name = ""
        user_match = re.search(
            r"<a\b[^>]*href=[\"']/account/([^\"']+)[\"'][^>]*>(.*?)</a>",
            cells[2],
            flags=re.I | re.S,
        )
        if user_match:
            user_id = user_match.group(1)
            user_name = strip_tags(user_match.group(2))
        else:
            user_name = strip_tags(cells[2])

        score_text = strip_tags(cells[3])
        score_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", score_text)
        copy_match = re.search(
            r"\bbtnCopyID\b.*?Botzone\.copy\('([^']+)'",
            row,
            flags=re.I | re.S,
        )
        if not copy_match:
            copy_match = re.search(r"Botzone\.copy\('([^']+)'", row, flags=re.I | re.S)
        ext_match = re.search(
            r"<span\b[^>]*class=[\"'][^\"']*\bext\b[^\"']*[\"'][^>]*>(.*?)</span>",
            row,
            flags=re.I | re.S,
        )

        rows.append({
            "bot_id": bot_id,
            "bot_name": strip_tags(cells[1]),
            "version": strip_tags(cells[5]),
            "version_id": copy_match.group(1) if copy_match else "",
            "score": score_match.group(1) if score_match else score_text,
            "ranked": True,
            "disabled": False,
            "desc": strip_tags(cells[4]),
            "user_id": user_id,
            "user_name": user_name,
            "extension": strip_tags(ext_match.group(1)) if ext_match else "",
        })
    return rows


def choose_rank_opponents(candidates, current_id, needed, opponent_ids=None, opponent_names=None):
    opponent_ids = opponent_ids or []
    opponent_names = opponent_names or []
    by_id = dict((candidate["id"], candidate) for candidate in candidates)
    chosen = []
    for bot_id in opponent_ids:
        if bot_id == current_id:
            continue
        if bot_id not in by_id:
            raise BotzoneError("Opponent bot id {} is not on this rank-match page.".format(bot_id))
        chosen.append(by_id[bot_id])
    for name in opponent_names:
        lowered = name.lower()
        matches = [c for c in candidates if c["id"] != current_id and lowered in c["name"].lower()]
        if len(matches) != 1:
            raise BotzoneError("Opponent name {!r} matched {} bots; use --opponent-bot-id.".format(name, len(matches)))
        chosen.append(matches[0])
    seen = set()
    unique = []
    for candidate in chosen:
        if candidate["id"] not in seen:
            seen.add(candidate["id"])
            unique.append(candidate)
    chosen = unique
    if len(chosen) > needed:
        raise BotzoneError("Too many opponents selected; this game needs {} opponent(s).".format(needed))
    if len(chosen) < needed:
        for candidate in candidates:
            if candidate["id"] == current_id or candidate["disabled"] or candidate["id"] in seen:
                continue
            chosen.append(candidate)
            seen.add(candidate["id"])
            if len(chosen) == needed:
                break
    if len(chosen) != needed:
        raise BotzoneError("Could only choose {} opponent(s), but {} are needed.".format(len(chosen), needed))
    return chosen


def start_rank_match(client, args, bot_id):
    page_path = "/game/ranklist/match/{}".format(bot_id)
    page = client.get_text(page_path)
    if "msg=notexist" in page or "对象不存在" in page:
        raise BotzoneError("Rank-match page is unavailable. The bot may not be ranked.")
    curr_bot = parse_curr_bot(page)
    candidates = parse_rank_candidates(page)
    current_id = curr_bot.get("_id") or bot_id
    min_players = int(curr_bot.get("game", {}).get("min_player_num", 2))
    opponents = choose_rank_opponents(
        candidates,
        current_id,
        max(0, min_players - 1),
        getattr(args, "opponent_bot_id", None),
        getattr(args, "opponent_name", None),
    )
    data = [(current_id, "true")]
    for opponent in opponents:
        data.append((opponent["id"], "true"))
    names = ", ".join("{} ({})".format(o["name"], o["id"]) for o in opponents)
    detail = "would start ranked match for {} ({}) vs {}".format(curr_bot.get("name", bot_id), current_id, names)
    if not confirm_or_skip(args, "MATCH", detail):
        return None
    result = client.post_json(page_path, data)
    if result.get("success") is False:
        raise BotzoneError("Rank match failed: {}".format(result.get("message", result)))
    return result


def ensure_bot_ranked(client, args, bot_id):
    if not bot_id:
        raise BotzoneError("Missing bot id for leaderboard join.")
    detail = fetch_bot_detail(client, bot_id)
    if detail.get("ranked"):
        print("Leaderboard: already ranked bot={} score={}".format(
            bot_id,
            detail.get("score", ""),
        ))
        return detail
    name = detail.get("name") or bot_id
    game = detail.get("game") or {}
    game_name = game.get("name") if isinstance(game, dict) else str(game)
    detail_text = (
        "would join leaderboard for bot {!r} ({}) in {}; Botzone may reset its score "
        "and unrank another bot in the same game"
    ).format(name, bot_id, game_name or "this game")
    if not confirm_or_skip(args, "RANK", detail_text):
        return None
    result = client.post_json("/mybots/rankbot", {"botid": bot_id})
    if result.get("success") is False:
        raise BotzoneError("Leaderboard join failed: {}".format(result.get("message", result)))
    bot = result.get("bot") or {}
    if not bot.get("ranked"):
        raise BotzoneError("Leaderboard toggle returned unranked state for bot {}.".format(bot_id))
    print("Leaderboard joined: bot={} score={}".format(
        bot.get("_id", bot_id),
        bot.get("score", ""),
    ))
    return bot


def read_captcha_attempts(args):
    try:
        raw_attempts = getattr(args, "captcha_attempts", 1)
        attempts = 1 if raw_attempts is None else int(raw_attempts)
    except (TypeError, ValueError):
        attempts = 1
    return max(0, attempts)


def get_captcha_mode(args):
    mode = getattr(args, "captcha_mode", "manual") or "manual"
    mode = str(mode).strip().lower()
    if mode not in ("auto", "manual"):
        mode = "manual"
    return mode


def read_captcha_retry_delay(args):
    try:
        delay = float(getattr(args, "captcha_retry_delay", DEFAULT_CAPTCHA_RETRY_DELAY))
    except (TypeError, ValueError):
        delay = DEFAULT_CAPTCHA_RETRY_DELAY
    return max(0.0, delay)


def normalize_captcha_answer(captcha):
    return (captcha or "").strip()


def fetch_and_solve_captcha(client, args):
    captcha_path = os.path.abspath(args.captcha_path)
    body = client.get_binary("/captcha/digit?{}".format(random.random()))
    preview_path = write_captcha_preview(captcha_path, body)
    print("Captcha image saved to: {}".format(captcha_path))

    mode = get_captcha_mode(args)
    if mode == "auto":
        try:
            recognized = recognize_captcha_file(captcha_path, args)
            score = recognized.get("score")
            score_gap = recognized.get("score_gap")
            if score is None:
                score_text = "score=?"
            else:
                score_text = "score={:.4f}".format(score)
            if score_gap is not None:
                score_text += ", gap={:.4f}".format(score_gap)
            top_text = format_captcha_candidates(recognized.get("candidates"))
            if top_text:
                print("Auto captcha: {} ({}; top={})".format(recognized["char"], score_text, top_text))
            else:
                print("Auto captcha: {} ({})".format(recognized["char"], score_text))
            for warning in recognized.get("threshold_warnings") or []:
                print("Auto captcha below threshold; trying anyway: {}".format(warning), file=sys.stderr)
            return recognized["char"]
        except CaptchaRecognitionError as exc:
            print("Auto captcha skipped: {}".format(exc), file=sys.stderr)
            raise

    print("Captcha preview saved to: {}".format(preview_path))
    if getattr(args, "open_captcha", False):
        open_captcha_preview(preview_path)
    if not sys.stdin.isatty():
        raise BotzoneError("Pass --captcha in non-interactive mode.")
    return input("Captcha char: ").strip()


def submit_create_room_with_captcha(client, args, captcha):
    captcha = normalize_captcha_answer(captcha)
    if not re.match(r"^\S$", captcha):
        raise BotzoneError("Captcha must be exactly one non-space character.")
    path = "/gametable/create?{}".format(urllib.parse.urlencode({"game": args.game_id, "captcha": captcha}))
    resp = client.request("GET", path)
    final_url = resp["url"]
    if final_url.startswith("/"):
        final_url = client.url(final_url)
    body = resp.get("body") or ""
    if "/gametable/join/" in final_url:
        return {"url": final_url, "body": body}
    if "javascripts/gametable_room.js" in body or "readyMessage" in body:
        response_path = os.path.abspath(args.response_path)
        write_text(response_path, body)
        room_id = extract_room_id_from_page(body)
        if room_id:
            return {
                "url": client.url("/gametable/join/{}".format(room_id)),
                "actual_url": final_url,
                "response_path": response_path,
                "body": body,
            }
        return {
            "url": final_url,
            "response_path": response_path,
            "body": body,
            "warning": "Created room page was returned, but no reusable /gametable/join/<id> link was found.",
        }
    if "captcha.wrong" in final_url or "captcha.wrong" in body:
        raise CaptchaRejectedError("Captcha was rejected by Botzone.")
    if "captcha" in final_url:
        response_path = os.path.abspath(args.response_path)
        write_text(response_path, body)
        raise BotzoneError("Create room may have failed; final URL: {}. The response was saved to {}.".format(final_url, response_path))
    raise BotzoneError("Create room may have failed; final URL: {}".format(final_url))


def create_room(client, args):
    detail = "would create a game table for game {}".format(args.game_id)
    if not confirm_or_skip(args, "CREATE", detail):
        return None

    if args.captcha:
        return submit_create_room_with_captcha(client, args, args.captcha)

    attempts = read_captcha_attempts(args)
    retry_delay = read_captcha_retry_delay(args)
    unlimited = attempts == 0
    last_error = None
    attempt = 0
    while unlimited or attempt < attempts:
        attempt += 1
        try:
            captcha = fetch_and_solve_captcha(client, args)
            return submit_create_room_with_captcha(client, args, captcha)
        except (CaptchaRecognitionError, CaptchaRejectedError) as exc:
            last_error = exc
            if not unlimited and attempt >= attempts:
                break
            attempt_label = "{}/∞".format(attempt) if unlimited else "{}/{}".format(attempt, attempts)
            print(
                "Captcha attempt {} failed: {}; retrying in {:.1f}s.".format(attempt_label, exc, retry_delay),
                file=sys.stderr,
            )
            if retry_delay > 0:
                time.sleep(retry_delay)
    if last_error:
        raise last_error
    raise BotzoneError("Room creation failed before submitting a captcha.")


def command_list_bots(client, args):
    client.ensure_login(args.email, args.password)
    bots = parse_bot_list(client.get_text("/mybots"))
    if args.game_id:
        bots = [bot for bot in bots if bot["game_id"] == args.game_id]
    print_bots(bots)


def command_list_room_bots(client, args):
    client.ensure_login(args.email, args.password)
    bot_id = args.bot_id
    rows = []
    if args.user_id:
        result = get_json(client, "/listbots/{}?{}".format(
            args.user_id,
            urllib.parse.urlencode({"game": args.game_id})
        ))
        if result.get("success") is False:
            raise BotzoneError("List user bots failed: {}".format(result.get("message", result)))
        rows = [normalize_bot_version(bot) for bot in result.get("bots", [])]
        title = "Room-selectable bots for user {}:".format(args.user_id)
    else:
        if not bot_id:
            bot, _ = resolve_bot(client, args.bot_name, None, args.game_id)
            bot_id = bot["id"]
        detail = fetch_bot_detail(client, bot_id)
        rows = [normalize_bot_version(detail)]
        title = "Room-selectable latest version for {}:".format(detail.get("name", bot_id))
    if args.ranked_only:
        rows = [row for row in rows if row.get("ranked")]
    if args.limit is not None:
        rows = rows[:args.limit]
    print_room_bot_rows(title, rows, args.json)


def command_list_room_opponents(client, args):
    client.ensure_login(args.email, args.password)
    bot_id = args.bot_id
    if not bot_id:
        bot, _ = resolve_bot(client, args.bot_name, None, args.game_id)
        bot_id = bot["id"]
    detail = fetch_bot_detail(client, bot_id)
    owner_id = (detail.get("user") or {}).get("_id", "")
    rows = []
    seen = set()
    for match in detail.get("rank_matches", []) + detail.get("matches", []):
        for player in match.get("players", []):
            botver = player.get("bot")
            if not botver:
                continue
            bot = botver.get("bot") or {}
            user = botver.get("user") or {}
            if bot.get("_id") == bot_id:
                continue
            key = botver.get("_id") or bot.get("_id")
            if not key or key in seen:
                continue
            if args.external_only and user.get("_id") == owner_id:
                continue
            seen.add(key)
            rows.append({
                "bot_id": bot.get("_id", ""),
                "bot_name": bot.get("name", ""),
                "version": botver.get("ver", ""),
                "version_id": botver.get("_id", ""),
                "score": bot.get("score", ""),
                "ranked": bot.get("ranked", False),
                "desc": "",
                "user_id": user.get("_id", ""),
                "user_name": user.get("name", ""),
                "match_id": match.get("_id", ""),
            })
    if args.ranked_only:
        rows = [row for row in rows if row.get("ranked")]
    rows.sort(key=lambda row: (0 if row.get("ranked") else 1, -(float(row.get("score") or 0))))
    if args.limit is not None:
        rows = rows[:args.limit]
    title = "Prior room/rank opponents for {}:".format(detail.get("name", bot_id))
    print_room_bot_rows(title, rows, args.json)


def command_upload(client, args):
    client.ensure_login(args.email, args.password)
    bot_id = args.bot_id or ""
    if args.create_new:
        if not args.bot_name:
            raise BotzoneError("--bot-name is required with --create-new.")
    else:
        if not bot_id and not args.bot_name:
            raise BotzoneError("Pass --bot-name or --bot-id.")
        bot, _ = resolve_bot(client, args.bot_name, bot_id, args.game_id)
        bot_id = bot["id"]
        if not args.bot_name:
            args.bot_name = bot["name"]
    result = upload_plain(client, args, bot_id)
    if result is not None:
        bot = result.get("bot", {})
        versions = bot.get("versions") or []
        latest = len(versions) - 1 if versions else "?"
        print("Upload success: bot={} latest_version={}".format(bot.get("_id", bot_id), latest))
        if result.get("warnings"):
            print("Warnings: {}".format(result.get("warnings")))
    if getattr(args, "join_rank", False):
        if result is not None:
            bot_id = result.get("bot", {}).get("_id", bot_id)
        if bot_id:
            rank_result = ensure_bot_ranked(client, args, bot_id)
            if result is not None and rank_result is not None:
                result["rank_bot"] = rank_result
        elif not getattr(args, "execute", False):
            print("DRY RUN: would join uploaded bot to leaderboard after creation.")
        else:
            raise BotzoneError("Upload did not return a bot id for leaderboard join.")
    if args.rank_match:
        if result is not None:
            bot_id = result.get("bot", {}).get("_id", bot_id)
        match_result = start_rank_match(client, args, bot_id)
        if match_result is not None:
            print("Rank match: {}".format(client.url("/match/{}".format(match_result.get("matchid")))))
    return result


def command_rank_match(client, args):
    client.ensure_login(args.email, args.password)
    bot_id = args.bot_id
    if not bot_id:
        bot, _ = resolve_bot(client, args.bot_name, None, args.game_id)
        bot_id = bot["id"]
    result = start_rank_match(client, args, bot_id)
    if result is not None:
        print("Rank match: {}".format(client.url("/match/{}".format(result.get("matchid")))))


def command_list_opponents(client, args):
    client.ensure_login(args.email, args.password)
    bot_id = args.bot_id
    if not bot_id:
        bot, _ = resolve_bot(client, args.bot_name, None, args.game_id)
        bot_id = bot["id"]
    page_path = "/game/ranklist/match/{}".format(bot_id)
    page = client.get_text(page_path)
    if "msg=notexist" in page or "对象不存在" in page:
        raise BotzoneError("Opponent list is unavailable. The bot may not be ranked.")
    curr_bot = parse_curr_bot(page)
    candidates = parse_rank_candidates(page)
    rows = []
    for candidate in candidates:
        if candidate["id"] == curr_bot.get("_id"):
            if not args.include_self:
                continue
            candidate = dict(candidate)
            candidate["name"] = candidate["name"] + " (self)"
        if candidate.get("disabled") and not args.include_disabled:
            continue
        rows.append(candidate)
    if args.limit is not None:
        rows = rows[:args.limit]
    if args.json:
        print(json.dumps({
            "bot": curr_bot,
            "opponents": rows,
            "rank_match_url": client.url(page_path),
        }, ensure_ascii=False, indent=2))
        return
    print("Rank-match bot: {} ({})".format(curr_bot.get("name", bot_id), curr_bot.get("_id", bot_id)))
    print("Use --opponent-bot-id <id> or --opponent-name <substring> with rank-match.")
    for index, candidate in enumerate(rows, 1):
        flags = []
        if candidate.get("disabled"):
            flags.append("disabled")
        if candidate.get("selected"):
            flags.append("selected")
        flag_text = " [{}]".format(",".join(flags)) if flags else ""
        print("{idx:>2}. {name}\tid={id}\tver={version}\tscore={score}\twin+={win}\tlose-={lose}{flags}".format(
            idx=index,
            name=candidate.get("name", ""),
            id=candidate.get("id", ""),
            version=candidate.get("version", ""),
            score=candidate.get("score", ""),
            win=candidate.get("victory_increase", ""),
            lose=candidate.get("defeated_decrease", ""),
            flags=flag_text,
        ))


def command_create_room(client, args):
    client.ensure_login(args.email, args.password)
    result = create_room(client, args)
    if result is not None:
        print("Game table: {}".format(result["url"]))
        if result.get("actual_url") and result["actual_url"] != result["url"]:
            print("Actual returned URL: {}".format(result["actual_url"]))
        if result.get("response_path"):
            print("Saved room response: {}".format(result["response_path"]))
        if result.get("warning"):
            print("WARNING: {}".format(result["warning"]))
        print("Next: open the table, set both slots to Bot, then use select-by-ID with bot version ids from list-room-bots/list-room-opponents.")


def fetch_version_detail(client, version_id, game_id):
    result = get_json(client, "/mybots/detail/version/{}?{}".format(
        version_id,
        urllib.parse.urlencode({"game": game_id})
    ))
    if result.get("success") is False:
        raise BotzoneError("Version detail failed for {}: {}".format(version_id, result.get("message", result)))
    botver = result.get("bot") or {}
    bot = botver.get("bot") or {}
    user = botver.get("user") or {}
    if not isinstance(user, dict):
        user = {"name": str(user)}
    return {
        "bot_id": bot.get("_id", ""),
        "bot_name": bot.get("name", ""),
        "version": botver.get("ver", ""),
        "version_id": botver.get("_id", version_id),
        "score": bot.get("score", ""),
        "ranked": bot.get("ranked", False),
        "disabled": botver.get("disabled", False),
        "desc": botver.get("desc", ""),
        "user_id": user.get("_id", ""),
        "user_name": user.get("name", ""),
    }


def fetch_latest_room_bot(client, bot_id=None, bot_name=None, game_id=None):
    if not bot_id:
        bot, _ = resolve_bot(client, bot_name, None, game_id)
        bot_id = bot["id"]
    detail = fetch_bot_detail(client, bot_id)
    row = normalize_bot_version(detail)
    row["disabled"] = False
    versions = detail.get("versions") or []
    if versions:
        latest = versions[-1]
        if isinstance(latest, dict):
            row["version_id"] = latest.get("_id", row.get("version_id", ""))
            row["disabled"] = latest.get("disabled", False)
            row["desc"] = latest.get("desc", row.get("desc", ""))
    return row


def collect_own_bot_ids(client, game_id):
    own = set()
    for bot in parse_bot_list(client.get_text("/mybots")):
        if not game_id or bot.get("game_id") == game_id:
            own.add(bot.get("id"))
    return own


def add_opponent(rows, seen, row, owner_user_id="", own_bot_ids=None, include_own=False, include_disabled=False, source=""):
    own_bot_ids = own_bot_ids or set()
    version_id = row.get("version_id")
    if not version_id or version_id in seen:
        return
    if row.get("disabled") and not include_disabled:
        return
    if not include_own:
        if row.get("user_id") and owner_user_id and row.get("user_id") == owner_user_id:
            return
        if row.get("bot_id") in own_bot_ids:
            return
    row = dict(row)
    row["source"] = source
    seen.add(version_id)
    rows.append(row)


def collect_ranked_room_opponents(client, args, owner_user_id, own_bot_ids):
    source_bot_id = args.rank_source_bot_id
    source_bot_name = getattr(args, "rank_source_bot_name", "") or ""
    if not source_bot_id and not source_bot_name:
        return collect_game_ranklist_room_opponents(client, args, owner_user_id, own_bot_ids)
    if not source_bot_id:
        source_bot = fetch_latest_room_bot(
            client,
            bot_name=source_bot_name,
            game_id=args.game_id,
        )
        source_bot_id = source_bot.get("bot_id")
    page_path = "/game/ranklist/match/{}".format(source_bot_id)
    page = client.get_text(page_path)
    if "msg=notexist" in page or "对象不存在" in page:
        raise BotzoneError("Ranked opponent list is unavailable for {}.".format(source_bot_id))
    curr_bot = parse_curr_bot(page)
    candidates = parse_rank_candidates(page)
    rows = []
    seen = set()
    for candidate in candidates:
        if candidate.get("disabled") and not args.include_disabled:
            continue
        candidate_id = candidate.get("id")
        if not candidate_id or candidate_id == curr_bot.get("_id"):
            continue
        if candidate_id in own_bot_ids and not args.include_own:
            continue
        try:
            row = fetch_latest_room_bot(client, bot_id=candidate_id, game_id=args.game_id)
        except BotzoneError as exc:
            if args.verbose:
                print("Skipping ranked opponent {}: {}".format(candidate_id, exc), file=sys.stderr)
            continue
        row["rank_score_text"] = candidate.get("score", "")
        row["victory_increase"] = candidate.get("victory_increase", "")
        row["defeated_decrease"] = candidate.get("defeated_decrease", "")
        add_opponent(
            rows,
            seen,
            row,
            owner_user_id,
            own_bot_ids,
            args.include_own,
            args.include_disabled,
            "ranked",
        )
    return rows


def collect_game_ranklist_room_opponents(client, args, owner_user_id, own_bot_ids):
    page_path = "/game/ranklist/{}".format(args.game_id)
    first_page = client.get_text(page_path)
    if "msg=notexist" in first_page or "对象不存在" in first_page:
        raise BotzoneError("Game ranklist is unavailable for {}.".format(args.game_id))

    rows = []
    seen = set()
    page_paths = parse_game_ranklist_page_paths(first_page, args.game_id)
    for index, current_path in enumerate(page_paths):
        page = first_page if index == 0 else client.get_text(current_path)
        for row in parse_game_ranklist_rows(page):
            row["ranklist_url"] = client.url(current_path)
            add_opponent(
                rows,
                seen,
                row,
                owner_user_id,
                own_bot_ids,
                args.include_own,
                args.include_disabled,
                "ranked",
            )
            if args.limit is not None and len(rows) >= args.limit:
                return rows
    return rows


def collect_history_room_opponents(client, args, my_bot_id, owner_user_id, own_bot_ids):
    detail = fetch_bot_detail(client, my_bot_id)
    rows = []
    seen = set()
    matches = detail.get("rank_matches", []) + detail.get("matches", [])
    for match in matches:
        for player in match.get("players", []):
            botver = player.get("bot")
            if not botver:
                continue
            bot = botver.get("bot") or {}
            user = botver.get("user") or {}
            row = {
                "bot_id": bot.get("_id", ""),
                "bot_name": bot.get("name", ""),
                "version": botver.get("ver", ""),
                "version_id": botver.get("_id", ""),
                "score": bot.get("score", ""),
                "ranked": bot.get("ranked", False),
                "disabled": botver.get("disabled", False),
                "desc": botver.get("desc", ""),
                "user_id": user.get("_id", ""),
                "user_name": user.get("name", ""),
                "match_id": match.get("_id", ""),
            }
            add_opponent(
                rows,
                seen,
                row,
                owner_user_id,
                own_bot_ids,
                args.include_own,
                args.include_disabled,
                "history",
            )
    rows.sort(key=lambda row: (0 if row.get("ranked") else 1, -(float(row.get("score") or 0))))
    return rows


def merge_opponent_lists(*lists):
    rows = []
    seen = set()
    for items in lists:
        for row in items:
            version_id = row.get("version_id")
            if not version_id or version_id in seen:
                continue
            seen.add(version_id)
            rows.append(row)
    return rows


def resolve_room_opponents(client, args, my_bot):
    owner_user_id = my_bot.get("user_id", "")
    own_bot_ids = collect_own_bot_ids(client, args.game_id)
    sources = args.opponent_source or ["all"]
    if "all" in sources:
        sources = ["ranked", "history"]

    direct = []
    for version_id in args.opponent_version_id or []:
        row = fetch_version_detail(client, version_id, args.game_id)
        row["source"] = "direct"
        direct.append(row)

    ranked = collect_ranked_room_opponents(client, args, owner_user_id, own_bot_ids) if "ranked" in sources else []
    history = collect_history_room_opponents(client, args, my_bot.get("bot_id"), owner_user_id, own_bot_ids) if "history" in sources else []
    rows = merge_opponent_lists(direct, ranked, history)
    if args.limit is not None:
        rows = rows[:args.limit]
    return rows


def room_slot(row):
    version = row.get("version", "")
    name = "{}【{}】".format(row.get("bot_name", ""), version)
    return {
        "type": "bot",
        "name": name,
        "id": row.get("version_id", ""),
        "botid": row.get("bot_id", ""),
        "ranked": bool(row.get("ranked")),
    }


def read_room_ready_message(page):
    expr = extract_js_value_after(page, "readyMessage")
    if not expr:
        raise BotzoneError("Could not find readyMessage in room page.")
    try:
        return json.loads(expr)
    except ValueError:
        raise BotzoneError("Could not parse readyMessage from room page.")


def socketio_timestamp():
    return "{}-{}".format(int(time.time() * 1000), random.randint(0, 999999))


def encode_engineio_payload(packets):
    return "".join("{}:{}".format(len(packet), packet) for packet in packets)


def decode_engineio_payload(payload):
    packets = []
    idx = 0
    while idx < len(payload):
        colon = payload.find(":", idx)
        if colon < 0 or not payload[idx:colon].isdigit():
            packets.append(payload[idx:])
            break
        size = int(payload[idx:colon])
        start = colon + 1
        packets.append(payload[start:start + size])
        idx = start + size
    return packets


class SocketIOV2PollingClient(object):
    def __init__(self, client, namespace, timeout=20, verbose=False):
        self.client = client
        self.namespace = namespace
        self.timeout = timeout
        self.verbose = verbose
        self.sid = None

    def socket_path(self, sid=True):
        query = {
            "EIO": "3",
            "transport": "polling",
            "t": socketio_timestamp(),
        }
        if sid and self.sid:
            query["sid"] = self.sid
        return "/socket.io/?{}".format(urllib.parse.urlencode(query))

    def post_packets(self, packets):
        body = encode_engineio_payload(packets)
        resp = self.client.raw_request(
            "POST",
            self.socket_path(sid=True),
            body=body,
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "Origin": self.client.base_url,
                "Referer": self.client.base_url + "/",
            },
            timeout=self.timeout,
        )
        if self.verbose:
            print("Socket.IO POST {} -> {}".format(packets, short(resp.get("body", ""))), file=sys.stderr)
        return resp.get("body", "")

    def poll_packets(self):
        resp = self.client.raw_request(
            "GET",
            self.socket_path(sid=True),
            headers={
                "Origin": self.client.base_url,
                "Referer": self.client.base_url + "/",
            },
            timeout=self.timeout,
        )
        payload = resp.get("body", "")
        packets = decode_engineio_payload(payload)
        if self.verbose:
            print("Socket.IO POLL {}".format(packets), file=sys.stderr)
        return packets

    def connect(self):
        resp = self.client.raw_request(
            "GET",
            self.socket_path(sid=False),
            headers={
                "Origin": self.client.base_url,
                "Referer": self.client.base_url + "/",
            },
            timeout=self.timeout,
        )
        packets = decode_engineio_payload(resp.get("body", ""))
        open_packet = None
        for packet in packets:
            if packet.startswith("0"):
                open_packet = packet[1:]
                break
        if not open_packet:
            raise BotzoneError("Socket.IO handshake failed: {}".format(short(resp.get("body", ""))))
        try:
            info = json.loads(open_packet)
        except ValueError:
            raise BotzoneError("Socket.IO handshake returned invalid JSON: {}".format(short(open_packet)))
        self.sid = info.get("sid")
        if not self.sid:
            raise BotzoneError("Socket.IO handshake did not return sid.")
        self.post_packets(["40{}".format(self.namespace)])
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            for packet in self.poll_packets():
                if packet == "2":
                    self.post_packets(["3"])
                elif packet.startswith("40{}".format(self.namespace)):
                    return
                elif packet.startswith("44{}".format(self.namespace)):
                    raise BotzoneError("Socket.IO namespace error: {}".format(packet))
        raise BotzoneError("Timed out connecting Socket.IO namespace {}.".format(self.namespace))

    def emit(self, event, data):
        payload = json.dumps([event, data], ensure_ascii=False, separators=(",", ":"))
        self.post_packets(["42{},{}".format(self.namespace, payload)])

    def poll_event(self, event_name, deadline):
        prefix = "42{},".format(self.namespace)
        while time.time() < deadline:
            for packet in self.poll_packets():
                if packet == "2":
                    self.post_packets(["3"])
                    continue
                if packet.startswith("44{}".format(self.namespace)):
                    raise BotzoneError("Socket.IO namespace error: {}".format(packet))
                if not packet.startswith(prefix):
                    continue
                try:
                    payload = json.loads(packet[len(prefix):])
                except ValueError:
                    continue
                if isinstance(payload, list) and payload and payload[0] == event_name:
                    return payload[1] if len(payload) > 1 else None
        return None

    def close(self):
        if self.sid:
            try:
                self.post_packets(["1"])
            except Exception:
                pass


def start_socketio_room_match(client, room_url, my_bot, opponent, args, room_page_body=None):
    page = room_page_body if room_page_body is not None else client.get_text(room_url)
    ready_message = read_room_ready_message(page)
    slots = [room_slot(my_bot), room_slot(opponent)]
    sio = SocketIOV2PollingClient(client, "/room", timeout=args.socket_timeout, verbose=args.verbose)
    try:
        sio.connect()
        sio.emit("gametable.ready", ready_message)
        time.sleep(args.socket_ready_wait)
        sio.emit("gametable.change", slots)
        time.sleep(args.socket_change_wait)
        sio.emit("gametable.start", args.initdata or "")
        match_id = sio.poll_event("gametable.start", time.time() + args.room_start_timeout)
    finally:
        sio.close()

    if not match_id:
        raise BotzoneError("Timed out waiting for gametable.start from room.")
    match_id = str(match_id)
    return {
        "match_id": match_id,
        "match_url": client.url("/match/{}".format(match_id)),
        "slots": slots,
        "ready_message": ready_message,
    }


def extract_raw_log_from_match_page(page):
    expr = extract_js_value_after(page, "_rawLogJSON")
    if not expr:
        raise BotzoneError("Could not find _rawLogJSON in match page.")
    try:
        raw = json.loads(expr)
        return json.loads(raw)
    except ValueError:
        raise BotzoneError("Could not parse _rawLogJSON from match page.")


def extract_player_names_from_match_page(page):
    expr = extract_js_value_after(page, "playerNames")
    if not expr:
        return []
    try:
        rows = json.loads(expr)
    except ValueError:
        return []
    names = []
    for row in rows:
        if isinstance(row, dict):
            names.append(row.get("name", ""))
    return names


def stage_name(request, display):
    round_id = display.get("round") if isinstance(display, dict) else None
    if round_id is None:
        public_count = len(request.get("public_cards") or [])
        if public_count >= 5:
            round_id = 3
        elif public_count == 4:
            round_id = 2
        elif public_count == 3:
            round_id = 1
        else:
            round_id = 0
    names = {
        0: "PreFlop",
        1: "Flop",
        2: "Turn",
        3: "River",
    }
    return names.get(round_id, str(round_id)), round_id


def action_type(action):
    if action == -1:
        return "fold"
    if action == -2:
        return "all-in"
    if action == 0:
        return "call/check"
    if isinstance(action, int) and action > 0:
        return "raise"
    return "unknown"


def find_response(log, start_index, actor_id):
    key = str(actor_id)
    for idx in range(start_index + 1, len(log)):
        item = log[idx]
        if isinstance(item, dict) and key in item and isinstance(item[key], dict):
            if "response" in item[key] or "verdict" in item[key]:
                return idx, item[key]
        if isinstance(item, dict) and item.get("output", {}).get("command") == "request":
            break
    return None, {}


def parse_match_log(log, match_id, match_url="", players=None):
    players = players or []
    decisions = []
    decisions_by_hand = {}
    hand_results = []
    seen_hands = set()
    final_result = None
    final_matchdata = None
    decision_no = 0

    for idx, item in enumerate(log):
        if not isinstance(item, dict):
            continue
        output = item.get("output") or {}
        command = output.get("command")
        display = output.get("display") or {}
        matchdata = display.get("matchdata") or {}
        if command == "finish":
            final_result = display.get("final_result")
            final_matchdata = matchdata
        if display.get("temp_result") and matchdata:
            if command == "finish":
                result_hand = matchdata.get("hand")
            else:
                result_hand = matchdata.get("hand", 0) - 1
            if result_hand is not None and result_hand >= 0 and result_hand not in seen_hands:
                seen_hands.add(result_hand)
                temp = display.get("temp_result") or []
                p0 = temp[0].get("win_chips", "") if len(temp) > 0 and isinstance(temp[0], dict) else ""
                p1 = temp[1].get("win_chips", "") if len(temp) > 1 and isinstance(temp[1], dict) else ""
                last = display.get("last_action") or {}
                hand_results.append({
                    "hand": result_hand,
                    "p0_delta": p0,
                    "p1_delta": p1,
                    "total_after": matchdata.get("total_win_chips", []),
                    "games_after": matchdata.get("total_win_games", []),
                    "last_action": last,
                })
        if command != "request":
            continue
        content = output.get("content") or {}
        if not isinstance(content, dict) or not content:
            continue
        actor_key = sorted(content.keys())[0]
        try:
            actor_id = int(actor_key)
        except ValueError:
            actor_id = actor_key
        request_data = content.get(actor_key) or {}
        response_index, response_data = find_response(log, idx, actor_id)
        action = response_data.get("response")
        stage, stage_id = stage_name(request_data, display)
        decision_no += 1
        row = {
            "decision_no": decision_no,
            "log_index_request": idx,
            "log_index_response": response_index,
            "hand": request_data.get("hand", ""),
            "round": stage,
            "stage": stage,
            "stage_id": stage_id,
            "player": actor_id,
            "actor_id": actor_id,
            "bot_name": players[actor_id] if isinstance(actor_id, int) and actor_id < len(players) else "",
            "actor": players[actor_id] if isinstance(actor_id, int) and actor_id < len(players) else "",
            "dealer_id": request_data.get("dealer_id", ""),
            "my_chips_before": request_data.get("my_chips", ""),
            "my_cards": json_text(request_data.get("my_cards", [])),
            "public_cards": json_text(request_data.get("public_cards", [])),
            "history_len_before": len(request_data.get("history") or []),
            "pot_before": display.get("pot", ""),
            "round_bet_before": display.get("round_bet", ""),
            "round_raise_before": display.get("round_raise", ""),
            "round_player_bet_before": json_text(display.get("round_player_bet", [])),
            "player_chips_before": json_text(display.get("player_chips", [])),
            "action": action,
            "response_action": action,
            "response_action_type": action_type(action),
            "time_ms": response_data.get("time", ""),
            "memory_kb": response_data.get("memory", ""),
            "verdict": response_data.get("verdict", ""),
            "request_json": json_text(request_data),
            "response_json": json_text(response_data),
        }
        decisions.append(row)
        decisions_by_hand.setdefault(str(row["hand"]), []).append(row)

    if final_matchdata is None:
        for item in reversed(log):
            display = get_nested(item, ["output", "display"], {})
            if isinstance(display, dict) and display.get("matchdata"):
                final_matchdata = display.get("matchdata")
                break
    if final_result is None and final_matchdata:
        chips = final_matchdata.get("total_win_chips") or []
        games = final_matchdata.get("total_win_games") or []
        final_result = []
        for idx in range(max(len(chips), len(games))):
            final_result.append({
                "win_chips": chips[idx] if idx < len(chips) else "",
                "win_games": games[idx] if idx < len(games) else "",
            })

    player_summary = {}
    for row in decisions:
        key = str(row.get("actor_id"))
        summary = player_summary.setdefault(key, {
            "name": row.get("actor", ""),
            "decisions": 0,
            "verdicts": {},
            "time_ms_values": [],
            "memory_kb_values": [],
            "actions": {},
        })
        summary["decisions"] += 1
        verdict = row.get("verdict") or ""
        summary["verdicts"][verdict] = summary["verdicts"].get(verdict, 0) + 1
        if isinstance(row.get("time_ms"), int) or str(row.get("time_ms")).isdigit():
            summary["time_ms_values"].append(int(row.get("time_ms")))
        if isinstance(row.get("memory_kb"), int) or str(row.get("memory_kb")).isdigit():
            summary["memory_kb_values"].append(int(row.get("memory_kb")))
        action = row.get("response_action")
        summary["actions"][str(action)] = summary["actions"].get(str(action), 0) + 1
    for summary in player_summary.values():
        times = summary.pop("time_ms_values")
        memory = summary.pop("memory_kb_values")
        actions = summary.pop("actions")
        summary["time_ms_avg"] = round(sum(times) / float(len(times)), 2) if times else ""
        summary["time_ms_max"] = max(times) if times else ""
        summary["memory_kb_max"] = max(memory) if memory else ""
        summary["actions_top"] = sorted(actions.items(), key=lambda kv: (-kv[1], kv[0]))[:10]

    non_ok = [row for row in decisions if row.get("verdict") and row.get("verdict") != "OK"]
    return {
        "match_id": match_id,
        "match_url": match_url,
        "game": "TexasHoldem2p",
        "players": players,
        "final_result": final_result,
        "final_matchdata": final_matchdata or {},
        "player_summary": player_summary,
        "non_ok": non_ok,
        "hand_results": sorted(hand_results, key=lambda row: row.get("hand", 0)),
        "decisions": decisions,
        "decisions_by_hand": decisions_by_hand,
        "complete": final_result is not None or any(get_nested(x, ["output", "command"]) == "finish" for x in log if isinstance(x, dict)),
    }


def archive_match_page(page, match_id, match_url, out_dir, players=None):
    ensure_dir(out_dir)
    raw_html_path = os.path.join(out_dir, "raw.html")
    write_text(raw_html_path, page)
    log = extract_raw_log_from_match_page(page)
    page_players = extract_player_names_from_match_page(page)
    if not players:
        players = page_players
    parsed = parse_match_log(log, match_id, match_url, players)
    write_json(os.path.join(out_dir, "raw_log.json"), log)
    summary = dict(parsed)
    decisions = summary.pop("decisions")
    decisions_by_hand = summary.pop("decisions_by_hand")
    write_json(os.path.join(out_dir, "summary.json"), summary)
    write_json(os.path.join(out_dir, "decisions.json"), decisions)
    write_json(os.path.join(out_dir, "decisions_by_hand.json"), decisions_by_hand)

    hand_rows = []
    for row in parsed["hand_results"]:
        total = row.get("total_after") or []
        games = row.get("games_after") or []
        last = row.get("last_action") or {}
        hand_rows.append({
            "hand": row.get("hand", ""),
            "test_delta": row.get("p0_delta", ""),
            "opponent_delta": row.get("p1_delta", ""),
            "test_total_after": total[0] if len(total) > 0 else "",
            "opponent_total_after": total[1] if len(total) > 1 else "",
            "test_games_after": games[0] if len(games) > 0 else "",
            "opponent_games_after": games[1] if len(games) > 1 else "",
            "last_player": last.get("player_id", ""),
            "last_action": last.get("action", ""),
            "last_action_type": last.get("action_type", ""),
        })
    write_csv(os.path.join(out_dir, "hands.csv"), hand_rows, [
        "hand",
        "test_delta",
        "opponent_delta",
        "test_total_after",
        "opponent_total_after",
        "test_games_after",
        "opponent_games_after",
        "last_player",
        "last_action",
        "last_action_type",
    ])
    write_csv(os.path.join(out_dir, "decisions.csv"), decisions, [
        "decision_no",
        "hand",
        "round",
        "player",
        "bot_name",
        "action",
        "verdict",
        "time_ms",
        "memory_kb",
        "request_json",
        "response_json",
        "stage",
        "actor_id",
        "actor",
        "dealer_id",
        "my_chips_before",
        "my_cards",
        "public_cards",
        "history_len_before",
        "pot_before",
        "round_bet_before",
        "round_raise_before",
        "round_player_bet_before",
        "player_chips_before",
        "response_action",
        "response_action_type",
        "log_index_request",
        "log_index_response",
    ])
    return parsed


def poll_and_archive_match(client, match_id, match_url, out_dir, players, args):
    deadline = time.time() + args.match_timeout
    last_error = None
    while time.time() < deadline:
        page = client.get_text("/match/{}".format(match_id))
        try:
            parsed = archive_match_page(page, match_id, match_url, out_dir, players)
            if parsed.get("complete"):
                return parsed
            last_error = "match log exists but has no finish command yet"
        except BotzoneError as exc:
            last_error = str(exc)
            write_text(os.path.join(out_dir, "raw.html"), page)
        time.sleep(args.poll_interval)
    raise BotzoneError("Timed out waiting for match {} to finish: {}".format(match_id, last_error or "no status"))


def summarize_match_record(match_id, match_url, started_at, finished_at, my_bot, opponent, parsed, status="completed", error=""):
    final = parsed.get("final_matchdata") or {}
    chips = final.get("total_win_chips") or []
    games = final.get("total_win_games") or []
    psummary = parsed.get("player_summary") or {}
    my_summary = psummary.get("0", {})
    opp_summary = psummary.get("1", {})
    chip_delta = chips[0] if len(chips) > 0 else ""
    result = ""
    if isinstance(chip_delta, int) or str(chip_delta).lstrip("-").isdigit():
        val = int(chip_delta)
        result = "win" if val > 0 else ("loss" if val < 0 else "draw")
    return {
        "match_id": match_id,
        "match_url": match_url,
        "started_at": started_at,
        "finished_at": finished_at,
        "my_bot": my_bot.get("bot_name", ""),
        "my_version": my_bot.get("version", ""),
        "my_version_id": my_bot.get("version_id", ""),
        "opponent": opponent.get("bot_name", ""),
        "opponent_version": opponent.get("version", ""),
        "opponent_version_id": opponent.get("version_id", ""),
        "result": result,
        "chip_delta": chip_delta,
        "my_win_games": games[0] if len(games) > 0 else "",
        "opp_win_games": games[1] if len(games) > 1 else "",
        "my_decisions": my_summary.get("decisions", ""),
        "opp_decisions": opp_summary.get("decisions", ""),
        "my_avg_ms": my_summary.get("time_ms_avg", ""),
        "my_max_ms": my_summary.get("time_ms_max", ""),
        "my_max_mem_kb": my_summary.get("memory_kb_max", ""),
        "opp_avg_ms": opp_summary.get("time_ms_avg", ""),
        "opp_max_ms": opp_summary.get("time_ms_max", ""),
        "opp_max_mem_kb": opp_summary.get("memory_kb_max", ""),
        "status": status,
        "error": error,
    }


def make_run_dir(args, my_bot):
    if args.resume:
        return os.path.abspath(args.resume)
    if args.run_dir:
        return os.path.abspath(args.run_dir)
    name = "{}_{}_v{}".format(run_stamp(), slugify(my_bot.get("bot_name", ""), "bot"), my_bot.get("version", ""))
    return os.path.join(os.path.abspath(args.data_dir), name)


def load_saved_room_series_plan(run_dir):
    config_path = os.path.join(run_dir, "run_config.json")
    opponents_path = os.path.join(run_dir, "opponents.json")
    config = read_json(config_path) or {}
    opponents = read_json(opponents_path)
    if not isinstance(opponents, list) or not opponents:
        return None
    my_bot = config.get("my_bot")
    if not isinstance(my_bot, dict) or not my_bot.get("version_id"):
        return None
    return {
        "config": config,
        "my_bot": my_bot,
        "opponents": opponents,
    }


def completed_opponent_match_counts(run_dir):
    counts = {}
    for row in read_jsonl(os.path.join(run_dir, "matches.jsonl")):
        if row.get("status") == "completed" and row.get("opponent_version_id"):
            version_id = row.get("opponent_version_id")
            counts[version_id] = counts.get(version_id, 0) + 1
    return counts


def read_matches_per_opponent(args):
    try:
        value = int(getattr(args, "matches_per_opponent", DEFAULT_MATCHES_PER_OPPONENT) or DEFAULT_MATCHES_PER_OPPONENT)
    except (TypeError, ValueError):
        value = DEFAULT_MATCHES_PER_OPPONENT
    return max(1, value)


def print_run_plan(run_dir, my_bot, opponents, matches_per_opponent):
    print("Run directory: {}".format(run_dir))
    print("My bot: {name} v{ver} version_id={vid}".format(
        name=my_bot.get("bot_name", ""),
        ver=my_bot.get("version", ""),
        vid=my_bot.get("version_id", ""),
    ))
    print("Opponents: {}".format(len(opponents)))
    print("Matches per opponent: {}".format(matches_per_opponent))
    print("Total planned matches: {}".format(len(opponents) * matches_per_opponent))
    for idx, row in enumerate(opponents, 1):
        print("{idx:>2}. {name} v{ver} version_id={vid} score={score} source={source} user={user}".format(
            idx=idx,
            name=row.get("bot_name", ""),
            ver=row.get("version", ""),
            vid=row.get("version_id", ""),
            score=row.get("score", ""),
            source=row.get("source", ""),
            user=row.get("user_name", ""),
        ))


def command_run_room_series(client, args):
    client.ensure_login(args.email, args.password)
    saved_plan = None
    if args.resume:
        args.run_dir = os.path.abspath(args.resume)
        saved_plan = load_saved_room_series_plan(args.run_dir)

    if saved_plan:
        config = saved_plan["config"]
        my_bot = saved_plan["my_bot"]
        opponents = saved_plan["opponents"]
        if config.get("game_id"):
            args.game_id = config.get("game_id")
        matches_per_opponent = int(config.get("matches_per_opponent") or read_matches_per_opponent(args))
        print("Resume: using saved run_config.json and opponents.json; plan order is fixed.")
    else:
        my_bot = fetch_latest_room_bot(client, bot_id=args.bot_id, bot_name=args.bot_name, game_id=args.game_id)
        if not my_bot.get("version_id"):
            raise BotzoneError("Could not resolve latest version id for {}.".format(args.bot_name or args.bot_id))
        args.run_dir = make_run_dir(args, my_bot)
        opponents = resolve_room_opponents(client, args, my_bot)
        matches_per_opponent = read_matches_per_opponent(args)

    if not opponents:
        raise BotzoneError("No opponents found.")

    print_run_plan(args.run_dir, my_bot, opponents, matches_per_opponent)
    if args.dry_run or not args.execute:
        if not args.execute:
            hint = getattr(args, "execute_hint", "add --execute to create rooms and start matches")
            print("DRY RUN: {}.".format(hint))
        return

    ensure_dir(args.run_dir)
    ensure_dir(os.path.join(args.run_dir, "captchas"))
    ensure_dir(os.path.join(args.run_dir, "rooms"))
    ensure_dir(os.path.join(args.run_dir, "matches"))
    if not saved_plan:
        write_json(os.path.join(args.run_dir, "run_config.json"), {
            "created_at": now_text(),
            "base_url": args.base_url,
            "game_id": args.game_id,
            "my_bot": my_bot,
            "opponent_source": args.opponent_source,
            "rank_source_bot_name": args.rank_source_bot_name,
            "rank_source_bot_id": args.rank_source_bot_id,
            "limit": args.limit,
            "matches_per_opponent": matches_per_opponent,
            "total_planned_matches": len(opponents) * matches_per_opponent,
            "match_timeout": args.match_timeout,
            "poll_interval": args.poll_interval,
        })
        write_json(os.path.join(args.run_dir, "opponents.json"), opponents)

    completed_counts = completed_opponent_match_counts(args.run_dir)
    if completed_counts:
        completed_total = sum(min(completed_counts.get(row.get("version_id"), 0), matches_per_opponent) for row in opponents)
        print("Resume: {} completed match(es) already count toward this plan.".format(completed_total))

    total_planned_matches = len(opponents) * matches_per_opponent
    opponent_width = max(2, len(str(len(opponents))))
    repeat_width = max(2, len(str(matches_per_opponent)))
    for index, opponent in enumerate(opponents, 1):
        opponent_version_id = opponent.get("version_id")
        completed_for_opponent = min(completed_counts.get(opponent_version_id, 0), matches_per_opponent)
        if completed_for_opponent >= matches_per_opponent:
            continue
        for opponent_match_index in range(completed_for_opponent + 1, matches_per_opponent + 1):
            planned_match_index = (index - 1) * matches_per_opponent + opponent_match_index
            label = "{idx:0{iw}d}_m{rep:0{rw}d}_{name}_v{ver}".format(
                idx=index,
                iw=opponent_width,
                rep=opponent_match_index,
                rw=repeat_width,
                name=slugify(opponent.get("bot_name", ""), "opponent"),
                ver=opponent.get("version", ""),
            )
            started_at = now_text()
            match_id = ""
            match_url = ""
            try:
                print("\n[{}/{}] Creating room: {} v{} vs {} v{} (match {}/{})".format(
                    planned_match_index,
                    total_planned_matches,
                    my_bot.get("bot_name", ""),
                    my_bot.get("version", ""),
                    opponent.get("bot_name", ""),
                    opponent.get("version", ""),
                    opponent_match_index,
                    matches_per_opponent,
                ))
                room_args = argparse.Namespace(
                    execute=True,
                    yes=True,
                    game_id=args.game_id,
                    captcha=None,
                    captcha_path=os.path.join(args.run_dir, "captchas", "{}.svg".format(label)),
                    response_path=os.path.join(args.run_dir, "rooms", "{}.html".format(label)),
                    captcha_mode=args.captcha_mode,
                    captcha_recognizer=args.captcha_recognizer,
                    captcha_chars=args.captcha_chars,
                    captcha_min_score=args.captcha_min_score,
                    captcha_min_gap=args.captcha_min_gap,
                    captcha_try_below_threshold=getattr(args, "captcha_try_below_threshold", False),
                    captcha_attempts=args.captcha_attempts,
                    captcha_retry_delay=args.captcha_retry_delay,
                    open_captcha=args.open_captcha,
                )
                room = create_room(client, room_args)
                if not room:
                    raise BotzoneError("Room creation returned no result.")
                room_url = room["url"]
                print("Room: {}".format(room_url))
                started = start_socketio_room_match(client, room_url, my_bot, opponent, args, room.get("body"))
                match_id = started["match_id"]
                match_url = started["match_url"]
                print("Match: {}".format(match_url))
                match_dir = os.path.join(args.run_dir, "matches", match_id)
                write_json(os.path.join(match_dir, "room_start.json"), started)
                player_labels = [
                    "[{}]{}".format(my_bot.get("user_name", ""), room_slot(my_bot)["name"]),
                    "[{}]{}".format(opponent.get("user_name", ""), room_slot(opponent)["name"]),
                ]
                parsed = poll_and_archive_match(client, match_id, match_url, match_dir, player_labels, args)
                record = summarize_match_record(match_id, match_url, started_at, now_text(), my_bot, opponent, parsed)
                record.update({
                    "planned_match_index": planned_match_index,
                    "opponent_index": index,
                    "opponent_match_index": opponent_match_index,
                    "matches_per_opponent": matches_per_opponent,
                })
                append_jsonl(os.path.join(args.run_dir, "matches.jsonl"), record)
                append_csv_row(os.path.join(args.run_dir, "matches.csv"), record, MATCH_CSV_FIELDS)
                print("Result: {result}, chip_delta={chips}, logs={path}".format(
                    result=record.get("result"),
                    chips=record.get("chip_delta"),
                    path=match_dir,
                ))
            except Exception as exc:
                record = summarize_match_record(
                    match_id,
                    match_url,
                    started_at,
                    now_text(),
                    my_bot,
                    opponent,
                    {"final_matchdata": {}, "player_summary": {}},
                    status="error",
                    error=str(exc),
                )
                record.update({
                    "planned_match_index": planned_match_index,
                    "opponent_index": index,
                    "opponent_match_index": opponent_match_index,
                    "matches_per_opponent": matches_per_opponent,
                })
                append_jsonl(os.path.join(args.run_dir, "matches.jsonl"), record)
                append_csv_row(os.path.join(args.run_dir, "matches.csv"), record, MATCH_CSV_FIELDS)
                append_jsonl(os.path.join(args.run_dir, "errors.jsonl"), {
                    "at": now_text(),
                    "opponent": opponent,
                    "planned_match_index": planned_match_index,
                    "opponent_index": index,
                    "opponent_match_index": opponent_match_index,
                    "matches_per_opponent": matches_per_opponent,
                    "match_id": match_id,
                    "match_url": match_url,
                    "error": str(exc),
                })
                print("ERROR for opponent {} match {}/{}: {}".format(
                    opponent.get("bot_name", ""),
                    opponent_match_index,
                    matches_per_opponent,
                    exc,
                ), file=sys.stderr)
                if args.stop_on_error:
                    raise
            if args.delay > 0 and planned_match_index < total_planned_matches:
                time.sleep(args.delay)


def add_auth_args(parser):
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--email", default=os.environ.get("BOTZONE_EMAIL"))
    parser.add_argument("--password", default=os.environ.get("BOTZONE_PASSWORD"))
    parser.add_argument("--cookie-file", default=os.environ.get("BOTZONE_COOKIE_FILE", DEFAULT_COOKIE_FILE))
    parser.add_argument("-v", "--verbose", action="store_true")


def add_execute_args(parser):
    parser.add_argument("--execute", action="store_true", help="perform the state-changing request")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation prompt")


def add_captcha_args(parser, default_mode="manual", default_attempts=1):
    parser.add_argument(
        "--captcha-mode",
        choices=("auto", "manual"),
        default=default_mode,
        help="solve one-character room captcha automatically or prompt manually",
    )
    parser.add_argument("--captcha-recognizer", default=DEFAULT_CAPTCHA_RECOGNIZER)
    parser.add_argument("--captcha-chars", default=DEFAULT_CAPTCHA_CHARS, help="candidate chars passed to the recognizer")
    parser.add_argument(
        "--captcha-min-score",
        type=float,
        default=DEFAULT_CAPTCHA_MIN_SCORE,
        help="minimum recognizer score before retrying",
    )
    parser.add_argument(
        "--captcha-min-gap",
        type=float,
        default=DEFAULT_CAPTCHA_MIN_GAP,
        help="minimum top1-top2 score gap before retrying",
    )
    parser.add_argument(
        "--captcha-try-below-threshold",
        action="store_true",
        help="submit the best auto captcha guess even when score/gap is below threshold",
    )
    parser.add_argument(
        "--captcha-attempts",
        type=int,
        default=default_attempts,
        help="captcha solve/create retries before failing; 0 means retry forever",
    )
    parser.add_argument(
        "--captcha-retry-delay",
        type=float,
        default=DEFAULT_CAPTCHA_RETRY_DELAY,
        help="seconds to wait between captcha retries",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Upload Botzone bot source and create Botzone matches/tables.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_auth_args(parser)
    subparsers = parser.add_subparsers(dest="command")

    p_list = subparsers.add_parser("list-bots", help="list bots in the account")
    p_list.add_argument("--game-id", default=TEXAS_HOLDEM_2P_GAME_ID)
    p_list.set_defaults(func=command_list_bots)

    p_room_bots = subparsers.add_parser("list-room-bots", help="list bot version ids usable in a game table")
    p_room_bots.add_argument("--bot-name", help="own bot name")
    p_room_bots.add_argument("--bot-id", help="own bot id")
    p_room_bots.add_argument("--user-id", help="list public bots owned by this user id")
    p_room_bots.add_argument("--game-id", default=TEXAS_HOLDEM_2P_GAME_ID)
    p_room_bots.add_argument("--limit", type=int, default=20)
    p_room_bots.add_argument("--ranked-only", action="store_true")
    p_room_bots.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p_room_bots.set_defaults(func=command_list_room_bots)

    p_room_opps = subparsers.add_parser("list-room-opponents", help="list prior opponent version ids for game-table testing")
    p_room_opps.add_argument("--bot-name", help="own bot name")
    p_room_opps.add_argument("--bot-id", help="own bot id")
    p_room_opps.add_argument("--game-id", default=TEXAS_HOLDEM_2P_GAME_ID)
    p_room_opps.add_argument("--limit", type=int, default=20)
    p_room_opps.add_argument("--ranked-only", action="store_true")
    p_room_opps.add_argument("--external-only", action="store_true")
    p_room_opps.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p_room_opps.set_defaults(func=command_list_room_opponents)

    p_upload = subparsers.add_parser("upload", help="upload source as a new bot version or new bot")
    p_upload.add_argument("--source", required=True, help="local source file to upload")
    p_upload.add_argument("--bot-name", help="existing or new Botzone bot name")
    p_upload.add_argument("--bot-id", help="existing Botzone bot id")
    p_upload.add_argument("--description", help="Botzone description/stat text, max 100 chars")
    p_upload.add_argument("--game-id", default=TEXAS_HOLDEM_2P_GAME_ID)
    p_upload.add_argument("--extension", default="py36", help="Botzone compiler extension")
    p_upload.add_argument("--create-new", action="store_true", help="create a new bot instead of adding a version")
    p_upload.add_argument("--keep-running", action="store_true", help="set enable_keep_running on upload")
    p_upload.add_argument("--simpleio", action="store_true", help="set simpleio on upload")
    p_upload.add_argument("--opensource", action="store_true", help="set opensource on upload")
    p_upload.add_argument("--join-rank", action="store_true", help="ensure the uploaded bot is on the leaderboard")
    p_upload.add_argument("--rank-match", action="store_true", help="start a ranked match after upload")
    p_upload.add_argument("--opponent-bot-id", action="append", help="opponent bot id for ranked match")
    p_upload.add_argument("--opponent-name", action="append", help="unique substring of opponent bot name")
    add_execute_args(p_upload)
    p_upload.set_defaults(func=command_upload)

    p_rank = subparsers.add_parser("rank-match", help="start a Botzone ranked self-service match")
    p_rank.add_argument("--bot-name", help="ranked bot name")
    p_rank.add_argument("--bot-id", help="ranked bot id")
    p_rank.add_argument("--game-id", default=TEXAS_HOLDEM_2P_GAME_ID)
    p_rank.add_argument("--opponent-bot-id", action="append", help="opponent bot id")
    p_rank.add_argument("--opponent-name", action="append", help="unique substring of opponent bot name")
    add_execute_args(p_rank)
    p_rank.set_defaults(func=command_rank_match)

    p_opp = subparsers.add_parser("list-opponents", help="list ranked-match opponent choices")
    p_opp.add_argument("--bot-name", help="ranked bot name")
    p_opp.add_argument("--bot-id", help="ranked bot id")
    p_opp.add_argument("--game-id", default=TEXAS_HOLDEM_2P_GAME_ID)
    p_opp.add_argument("--limit", type=int, default=20)
    p_opp.add_argument("--include-self", action="store_true")
    p_opp.add_argument("--include-disabled", action="store_true")
    p_opp.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p_opp.set_defaults(func=command_list_opponents)

    p_room = subparsers.add_parser("create-room", help="create an open Botzone game table")
    p_room.add_argument("--game-id", default=TEXAS_HOLDEM_2P_GAME_ID)
    p_room.add_argument("--captcha", help="one-character captcha from Botzone")
    p_room.add_argument("--captcha-path", default=DEFAULT_CAPTCHA_FILE)
    p_room.add_argument("--response-path", default=DEFAULT_ROOM_RESPONSE_FILE, help="save returned room HTML here for debugging")
    add_captcha_args(p_room, default_mode="manual", default_attempts=DEFAULT_CAPTCHA_AUTO_ATTEMPTS)
    p_room.add_argument("--open-captcha", action="store_true", help="open local captcha preview with macOS open")
    add_execute_args(p_room)
    p_room.set_defaults(func=command_create_room, yes=True)

    p_series = subparsers.add_parser("run-room-series", help="run one bot against room-selectable opponents and archive logs")
    p_series.add_argument("--bot-name", default="test", help="own room bot name")
    p_series.add_argument("--bot-id", help="own room bot id")
    p_series.add_argument("--game-id", default=TEXAS_HOLDEM_2P_GAME_ID)
    p_series.add_argument(
        "--opponent-source",
        action="append",
        choices=("ranked", "history", "all"),
        default=None,
        help="opponent source; repeatable, default all",
    )
    p_series.add_argument(
        "--rank-source-bot-name",
        default=DEFAULT_ROOM_SOURCE_BOT_NAME,
        help="optional ranked bot used to read Botzone's rank-match opponent list; default reads the game ranklist",
    )
    p_series.add_argument(
        "--rank-source-bot-id",
        help="optional ranked bot id used to read Botzone's rank-match opponent list; default reads the game ranklist",
    )
    p_series.add_argument("--opponent-version-id", action="append", help="directly include a room-selectable opponent version id")
    p_series.add_argument("--limit", type=int, help="limit opponent count")
    p_series.add_argument("--matches-per-opponent", type=int, default=DEFAULT_MATCHES_PER_OPPONENT, help="number of matches to run against each opponent")
    p_series.add_argument("--data-dir", default=DEFAULT_RUNS_DIR, help="root directory for run data")
    p_series.add_argument("--run-dir", help="explicit output directory for this run")
    p_series.add_argument("--resume", help="resume an existing run directory")
    p_series.add_argument("--delay", type=float, default=3.0, help="delay between opponents after each match")
    p_series.add_argument("--poll-interval", type=float, default=5.0, help="seconds between match page polls")
    p_series.add_argument("--match-timeout", type=float, default=900.0, help="seconds to wait for each match to finish")
    p_series.add_argument("--room-start-timeout", type=float, default=60.0, help="seconds to wait for room start event")
    p_series.add_argument("--socket-timeout", type=float, default=20.0, help="Socket.IO connection timeout")
    p_series.add_argument("--socket-ready-wait", type=float, default=1.0, help="delay after gametable.ready before slot change")
    p_series.add_argument("--socket-change-wait", type=float, default=1.0, help="delay after slot change before start")
    p_series.add_argument("--initdata", default="", help="Botzone game initdata for gametable.start")
    p_series.add_argument("--include-own", action="store_true", help="include bots owned by the current account")
    p_series.add_argument("--include-disabled", action="store_true", help="include disabled candidates")
    add_captcha_args(p_series, default_mode="auto", default_attempts=DEFAULT_CAPTCHA_AUTO_ATTEMPTS)
    p_series.add_argument("--open-captcha", action="store_true", help="open local captcha preview with macOS open")
    p_series.add_argument("--dry-run", action="store_true", help="resolve bots and opponents without creating rooms")
    p_series.add_argument("--stop-on-error", action="store_true", help="stop the series on the first failed opponent")
    add_execute_args(p_series)
    p_series.set_defaults(func=command_run_room_series, yes=True)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    client = BotzoneClient(args.base_url, args.cookie_file, args.verbose)
    try:
        args.func(client, args)
        return 0
    except BotzoneError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
