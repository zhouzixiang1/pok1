"""Regression: priority_eval.json rewrite must not starve the eval-wait.

Root cause (2026-08-02): the daemon's H4 hot-reload dropped the entire
match_queue on ANY mtime change of ``priority_eval.json``, even when the
priority bot was unchanged. The orchestrator re-asserts the same eval-wait
(``_ensure_priority_eval_signal``) after each 600s timeout, rewriting the file
with the same bot → new mtime → daemon dropped queued matches → matches never
accumulated → eval-wait timed out again → rewrite → drop → starvation loop.

The fix: drop the queue ONLY when the priority bot actually changed. These
tests pin ``_should_drop_queue_for_priority_change``.
"""

import sys
from pathlib import Path

WEB_CORE = Path(__file__).resolve().parents[1] / "core"
if str(WEB_CORE) not in sys.path:
    sys.path.insert(0, str(WEB_CORE))

from elo_daemon import _should_drop_queue_for_priority_change  # noqa: E402


def test_same_bot_rewrite_does_not_drop():
    """A rewrite for the SAME bot (orchestrator re-asserting the eval-wait after
    a timeout) must preserve the queue — otherwise matches never accumulate."""
    drop, tracked_bot, tracked_mtime = _should_drop_queue_for_priority_change(
        prev_bot="national_cloud_v27",
        prev_mtime=1000.0,
        new_bot="national_cloud_v27",  # same bot
        new_mtime=2000.0,  # new mtime (rewrite)
    )
    assert drop is False
    assert tracked_bot == "national_cloud_v27"
    assert tracked_mtime == 2000.0


def test_bot_change_drops_queue():
    """A real priority-bot change (new commit redirected evaluation) drops the
    queue so the next refill uses the new priority."""
    drop, tracked_bot, tracked_mtime = _should_drop_queue_for_priority_change(
        prev_bot="national_cloud_v27",
        prev_mtime=1000.0,
        new_bot="national_cloud_v29",  # different bot
        new_mtime=2000.0,
    )
    assert drop is True
    assert tracked_bot == "national_cloud_v29"
    assert tracked_mtime == 2000.0


def test_none_to_bot_drops():
    """Initial signal (None → a real bot) should drop any seed queue so the
    priority bot's matches are picked first."""
    drop, tracked_bot, _ = _should_drop_queue_for_priority_change(
        prev_bot=None,
        prev_mtime=0.0,
        new_bot="national_cloud_v27",
        new_mtime=1000.0,
    )
    assert drop is True
    assert tracked_bot == "national_cloud_v27"


def test_bot_to_none_drops():
    """Signal expiring (bot → None, e.g. min_games reached) should drop so the
    queue refills with normal (non-priority) matchmaking."""
    drop, tracked_bot, _ = _should_drop_queue_for_priority_change(
        prev_bot="national_cloud_v27",
        prev_mtime=1000.0,
        new_bot=None,
        new_mtime=2000.0,
    )
    assert drop is True
    assert tracked_bot is None


def test_repeated_same_bot_rewrites_never_drop():
    """Simulate the starvation scenario: many same-bot rewrites across multiple
    eval-wait timeouts must never drop the queue."""
    bot = "national_cloud_v27"
    prev_bot, prev_mt = bot, 0.0
    for mt in range(1000, 10000, 600):  # 600s timeout cadence
        drop, prev_bot, prev_mt = _should_drop_queue_for_priority_change(
            prev_bot, prev_mt, bot, float(mt)
        )
        assert drop is False, f"same-bot rewrite at mtime {mt} must not drop"
    assert prev_bot == bot
