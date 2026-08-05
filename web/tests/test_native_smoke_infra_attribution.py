"""Regression guard for the watchdog/startup-failure misattribution deadlock.

The native smoke ``run_native_tcp_smoke`` attributed a failure to the
candidate whenever ``candidate_issues`` was non-empty.  But the fail-closed
name-handshake / process analyser (``national_native_analysis``) emits
``native_name_handshake_missing``, ``native_process_returncode=2`` and
``timeouts`` for BOTH players whenever the run is killed during startup
(startup-watchdog kill: both processes reaped before the TCP/name exchange
finished) or stalls during finalization (transport stall).  A pure
infrastructure stall therefore always looked like a deterministic
candidate defect, so the generation was permanently abandoned at
``workers_done`` instead of taking the bounded infrastructure retry path
(``QUALITY_INFRA_MAX_ATTEMPTS``).  This caused v30-v62 to abandon 16
generations at ``workers_done`` — each after spending its full
Master+Worker LLM budget — on what was purely harness flakiness (the bot
runtime is system-owned and byte-identical across every candidate and
baseline opponent, so a startup-phase failure can never be a candidate
policy defect).

These failures must now be classified as ``infrastructure_failure`` /
``failure_side="harness"`` so ``tool_gates.run_quality_gates`` routes them
through ``mark_quality_infrastructure("native_smoke_harness", ...)`` and
retries instead of abandoning.
"""

import asyncio

import national_native as national_native_mod
import national_native_acceptance

CANDIDATE_LABEL = "national_cloud_v63"
OPPONENT_LABEL = "national_cloud_v27"


def _per_player_issues(candidate_issues=None, opponent_issues=None):
    """Build a per_player block mirroring run_native_tcp_pair's shape."""
    return {
        CANDIDATE_LABEL: {
            "compliance_issues": list(candidate_issues or []),
            "passed_compliance": not candidate_issues,
        },
        OPPONENT_LABEL: {
            "compliance_issues": list(opponent_issues or []),
            "passed_compliance": not opponent_issues,
        },
    }


def _run_smoke_with_result(monkeypatch, result):
    """Drive ``run_native_tcp_smoke`` with a frozen pair result."""
    monkeypatch.setattr(
        national_native_mod,
        "resolve_bot",
        lambda token: (
            (CANDIDATE_LABEL, token) if str(token).endswith("v63")
            else (OPPONENT_LABEL, token)
        ),
    )
    monkeypatch.setattr(
        national_native_mod,
        "select_acceptance_opponents",
        lambda label, source_v, limit=1: [(OPPONENT_LABEL, "/bots/national_cloud_v27")],
    )
    monkeypatch.setattr(
        national_native_mod,
        "_acceptance_opponent_runtime_mode",
        lambda label, dir: "direct_content_bound_policy_artifact",
    )

    async def fake_pair(candidate_dir, opponent_dir, hands, **_kwargs):
        return result

    monkeypatch.setattr(national_native_mod, "run_native_tcp_pair", fake_pair)
    return asyncio.run(
        national_native_acceptance.run_native_tcp_smoke(
            "/bots/national_cloud_v63",
            source_v=27,
            opponent_token=None,
            hands=1,
        )
    )


def test_startup_watchdog_kill_classified_as_infrastructure(monkeypatch):
    """A startup-watchdog kill (both players get native_name_handshake_missing,
    zero hands played, no output) is a harness failure, not a candidate defect."""
    result = {
        "per_player": _per_player_issues(
            candidate_issues=[f"{CANDIDATE_LABEL}: native_name_handshake_missing"],
            opponent_issues=[f"{OPPONENT_LABEL}: native_name_handshake_missing"],
        ),
        "issues": [
            f"{CANDIDATE_LABEL}: native_name_handshake_missing",
            f"{OPPONENT_LABEL}: native_name_handshake_missing",
        ],
        "hands_played": 0,
        "native_match_timeout_phase": "startup_watchdog",
    }
    report = _run_smoke_with_result(monkeypatch, result)
    assert report["outcome"] == "infrastructure_failure"
    assert report["failure_side"] == "harness"
    assert report["passed"] is False
    # The timeout phase must be preserved in the issues for diagnostics.
    joined = " ".join(report["issues"])
    assert "startup_watchdog" in joined
    assert "hands_played=0" in joined


def test_finalizing_cleanup_timeout_classified_as_infrastructure(monkeypatch):
    """A transport stall during finalization is a harness failure even when
    the analyser produced per-player compliance issues."""
    result = {
        "per_player": _per_player_issues(
            candidate_issues=[f"{CANDIDATE_LABEL}: native_process_returncode=2"],
            opponent_issues=[f"{OPPONENT_LABEL}: native_process_returncode=2"],
        ),
        "issues": [
            f"{CANDIDATE_LABEL}: native_process_returncode=2",
            f"{OPPONENT_LABEL}: native_process_returncode=2",
        ],
        "hands_played": 3,
        "native_match_timeout_phase": "finalizing_cleanup",
    }
    report = _run_smoke_with_result(monkeypatch, result)
    assert report["outcome"] == "infrastructure_failure"
    assert report["failure_side"] == "harness"
    assert "finalizing_cleanup" in " ".join(report["issues"])


def test_zero_hands_played_without_timeout_phase_is_infrastructure(monkeypatch):
    """A run that produced zero hands but no explicit timeout_phase (e.g. both
    processes killed silently) is still treated as infrastructure."""
    result = {
        "per_player": _per_player_issues(
            candidate_issues=[f"{CANDIDATE_LABEL}: native_name_handshake_missing"],
        ),
        "issues": [f"{CANDIDATE_LABEL}: native_name_handshake_missing"],
        "hands_played": 0,
        "native_match_timeout_phase": "",
    }
    report = _run_smoke_with_result(monkeypatch, result)
    assert report["outcome"] == "infrastructure_failure"
    assert report["failure_side"] == "harness"
    assert "hands_played=0" in " ".join(report["issues"])


def test_real_candidate_illegal_actions_still_candidate_failure(monkeypatch):
    """Regression guard: a genuine candidate defect that produced at least one
    hand (illegal_actions during gameplay) is still candidate_failure and must
    NOT be retried as infrastructure."""
    result = {
        "per_player": _per_player_issues(
            candidate_issues=[f"{CANDIDATE_LABEL}: illegal_actions=1"],
        ),
        "issues": [f"{CANDIDATE_LABEL}: illegal_actions=1"],
        "hands_played": 1,
        "native_match_timeout_phase": "",
    }
    report = _run_smoke_with_result(monkeypatch, result)
    assert report["outcome"] == "candidate_failure"
    assert report["failure_side"] == "candidate"
    assert f"{CANDIDATE_LABEL}: illegal_actions=1" in report["issues"]


def test_passed_run_unchanged(monkeypatch):
    """A clean pass still reports passed."""
    result = {
        "per_player": _per_player_issues(),
        "issues": [],
        "hands_played": 1,
        "native_match_timeout_phase": "",
    }
    report = _run_smoke_with_result(monkeypatch, result)
    assert report["passed"] is True
    assert report["outcome"] == "passed"
