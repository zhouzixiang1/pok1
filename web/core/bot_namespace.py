"""Bot naming and tag helpers for the active evolution epoch."""

from __future__ import annotations

import os
import re
from pathlib import Path


EVALUATION_EPOCH = os.environ.get("POK_EVALUATION_EPOCH", "national_native_v1")
ACTIVE_BOT_PREFIX = os.environ.get("POK_BOT_PREFIX", "national_v")
ACTIVE_TAG_PREFIX = os.environ.get("POK_BOT_TAG_PREFIX", "national-bot-v")
LEGACY_BOT_PREFIX = "claude_v"
LEGACY_TAG_PREFIX = "bot-v"
VERSION_WIDTH = int(os.environ.get("POK_BOT_VERSION_WIDTH", "0"))


def format_version(version: int | str) -> str:
    v = int(version)
    if VERSION_WIDTH <= 0:
        return str(v)
    return f"{v:0{VERSION_WIDTH}d}"


def bot_name(version: int | str) -> str:
    return f"{ACTIVE_BOT_PREFIX}{format_version(version)}"


def bot_dir(root: Path, version: int | str) -> Path:
    return root / "bots" / bot_name(version)


def bot_relpath(version: int | str) -> str:
    return f"bots/{bot_name(version)}"


def bot_tag(version: int | str) -> str:
    return f"{ACTIVE_TAG_PREFIX}{format_version(version)}"


def bot_tag_glob() -> str:
    return f"{ACTIVE_TAG_PREFIX}*"


def parse_bot_version(name: str | None) -> int | None:
    if not isinstance(name, str):
        return None
    base = Path(name.replace("\\", "/")).name
    for prefix in (ACTIVE_BOT_PREFIX, LEGACY_BOT_PREFIX, "v"):
        if base.startswith(prefix):
            suffix = base[len(prefix):]
            return int(suffix) if suffix.isdigit() else None
    return None


def parse_tag_version(tag: str | None) -> int | None:
    if not isinstance(tag, str):
        return None
    for prefix in (ACTIVE_TAG_PREFIX,):
        if tag.startswith(prefix):
            suffix = tag[len(prefix):]
            return int(suffix) if suffix.isdigit() else None
    return None


def active_bot_glob() -> str:
    return f"{ACTIVE_BOT_PREFIX}*"


def is_active_bot_name(name: str | None) -> bool:
    return isinstance(name, str) and name.startswith(ACTIVE_BOT_PREFIX) and parse_bot_version(name) is not None


def is_legacy_bot_name(name: str | None) -> bool:
    return isinstance(name, str) and name.startswith(LEGACY_BOT_PREFIX) and parse_bot_version(name) is not None


def version_sort_key(name: str) -> int:
    return parse_bot_version(name) or -1


def strip_bot_path_prefix(path: str) -> str:
    """Strip any generated bot path prefix from a worker target path."""

    raw = path.replace("\\", "/")
    prefixes = (re.escape(ACTIVE_BOT_PREFIX), re.escape(LEGACY_BOT_PREFIX))
    pattern = re.compile(rf"(?:\./)?(?:bots/)?(?:{'|'.join(prefixes)})\d+/(.+)$")
    while True:
        match = pattern.match(raw)
        if not match:
            return raw
        raw = match.group(1)
