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
    on an active-pool bot."""

    def raise_for(name):
        if name == "national_cloud_v29":
            raise RuntimeError("ineligible")
        return f"/bots/{name}"

    monkeypatch.setattr(elo_daemon, "bot_path", raise_for)
    assert elo_daemon._safe_bot_path("national_cloud_v27") == "/bots/national_cloud_v27"
    assert elo_daemon._safe_bot_path("national_cloud_v29") is None
