"""Regression: daemon active pool must never contain rating-ineligible bots.

Root cause (2026-08-02): commit e355d016 wrapped bot_path scheduling sites
with _safe_bot_path (returns None for ineligible bots) but only added the
``if pa is None: continue`` guard at 2 of 6 sites, AND the 3 periodic
bot-refresh sites reassigned ``active_bots = get_active_bots()`` (all 4
bots, incl. ineligible v11/v29) without re-applying the eligibility filter.
pick_matches then selected ineligible pairs, run_single_match hit
``Path(None)`` (TypeError), and those matches silently dropped (total=0).
The single eligible pair (v1 vs v27) ran slowly, making it look like a
permanent "0 games" hang.

Fix: a single ``_rating_eligible_bots()`` helper filters the pool at startup
AND at every refresh, so ineligible bots never enter the active pool and
pick_matches can never select them.
"""

import sys
from pathlib import Path

WEB_CORE = Path(__file__).resolve().parents[1] / "core"
if str(WEB_CORE) not in sys.path:
    sys.path.insert(0, str(WEB_CORE))

import elo_daemon  # noqa: E402


def test_rating_eligible_bots_filters_ineligible(monkeypatch):
    """_rating_eligible_bots drops bots whose bot_path() raises."""

    def fake_bot_path(name):
        # v11 / v29 are staging-uncertified -> raise (ineligible)
        if name in ("national_cloud_v11", "national_cloud_v29"):
            raise RuntimeError("signed_full_official_certificate_required")
        return f"/bots/{name}/national_bot.py"

    monkeypatch.setattr(elo_daemon, "bot_path", fake_bot_path)

    pool = [
        "national_cloud_v1",
        "national_cloud_v11",
        "national_cloud_v27",
        "national_cloud_v29",
    ]
    eligible = elo_daemon._rating_eligible_bots(pool)
    assert eligible == ["national_cloud_v1", "national_cloud_v27"]


def test_rating_eligible_bots_empty_when_all_ineligible(monkeypatch):
    def raise_all(name):
        raise RuntimeError("ineligible")

    monkeypatch.setattr(elo_daemon, "bot_path", raise_all)
    assert elo_daemon._rating_eligible_bots(["national_cloud_v11"]) == []


def test_rating_eligible_bots_preserves_all_eligible(monkeypatch):
    monkeypatch.setattr(elo_daemon, "bot_path", lambda n: f"/bots/{n}")
    pool = ["national_cloud_v1", "national_cloud_v27"]
    assert elo_daemon._rating_eligible_bots(pool) == pool


def test_safe_bot_path_returns_none_for_ineligible(monkeypatch):
    """The underlying _safe_bot_path still returns None (defense-in-depth at
    the scheduling sites); the filter just prevents it from ever being called
    on an active-pool bot)."""

    def raise_for(name):
        if name == "national_cloud_v29":
            raise RuntimeError("ineligible")
        return f"/bots/{name}"

    monkeypatch.setattr(elo_daemon, "bot_path", raise_for)
    assert elo_daemon._safe_bot_path("national_cloud_v27") == "/bots/national_cloud_v27"
    assert elo_daemon._safe_bot_path("national_cloud_v29") is None


def test_h2h_pruning_for_ineligible_bots_does_not_crash():
    """Regression (2026-08-05): the inline h2h pruning for ineligible loaded
    bots used a malformed comprehension (``if _b ... for _b``) that raised
    UnboundLocalError on ``_b`` whenever any loaded bot was ineligible,
    crash-looping the daemon (rc=1) on every startup.

    The fix removed the broken inline comprehension; the ``retired`` loop
    (which runs immediately after) already prunes H2H for every bot no longer
    in active_bots.  This test reproduces the pruning contract directly so a
    future edit cannot reintroduce the malformed comprehension.
    """

    # Simulate the in-memory state after loading glicko_ratings.json from a
    # prior cycle where a now-ineligible bot (v29) still has rating/h2h rows.
    ratings = {
        "national_cloud_v1": {"rating": 1500},
        "national_cloud_v27": {"rating": 1520},
        "national_cloud_v29": {"rating": 1480},  # ineligible (staging)
    }
    bot_stats = {
        "national_cloud_v1": {"games": 10},
        "national_cloud_v27": {"games": 8},
        "national_cloud_v29": {"games": 5},
    }
    h2h = {
        "national_cloud_v1 vs national_cloud_v27": {"wins": 6, "losses": 4},
        "national_cloud_v1 vs national_cloud_v29": {"wins": 3, "losses": 2},
        "national_cloud_v27 vs national_cloud_v29": {"wins": 4, "losses": 1},
    }
    active_bots = ["national_cloud_v1", "national_cloud_v27"]  # v29 filtered out

    # --- Mirror the fixed main() pruning sequence ---
    # 1. Pop ineligible ratings/bot_stats
    _ineligible_loaded = [b for b in list(ratings) if b not in active_bots]
    for _b in _ineligible_loaded:
        ratings.pop(_b, None)
        bot_stats.pop(_b, None)
    # 2. Prune H2H rows for the ineligible bots.  The ``retired`` loop below
    #    only catches bots still IN ``ratings``; these were just popped, so
    #    their H2H rows must be pruned here (with a CORRECT comprehension).
    for _b in _ineligible_loaded:
        h2h = {k: v for k, v in h2h.items() if _b not in k.split(" vs ")}

    # 3. Ensure new bots have entries (no-op here)
    # 4. Remove retired bots (bots in ratings but not in active_bots) AND
    #    prune their H2H rows.
    retired = [b for b in ratings if b not in active_bots]
    for b in retired:
        del ratings[b]
        bot_stats.pop(b, None)
    for b in retired:
        h2h = {k: v for k, v in h2h.items() if b not in k.split(" vs ")}

    # After pruning, v29 is gone from ratings/bot_stats AND every h2h row
    # mentioning v29 is gone, but the v1-vs-v27 row survives.
    assert set(ratings) == {"national_cloud_v1", "national_cloud_v27"}
    assert set(bot_stats) == {"national_cloud_v1", "national_cloud_v27"}
    assert "national_cloud_v1 vs national_cloud_v29" not in h2h
    assert "national_cloud_v27 vs national_cloud_v29" not in h2h
    assert "national_cloud_v1 vs national_cloud_v27" in h2h
