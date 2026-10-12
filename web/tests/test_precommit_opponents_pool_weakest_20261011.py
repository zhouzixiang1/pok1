"""Pool-weakest precommit opponent injection regressions (2026-10-11).

Covers _select_precommit_opponents' pool-ratchet branch: weakest-member
injection from the frozen snapshot rows, parent-is-weakest degeneration,
fail-closed blockers on missing conservative_rating, deferral when the
weakest was just reaped, legacy zero-injection, and the shared mode-resolved
reap capture policy between the manual MCP path and the handoff path.
"""

import json

import pytest

import tool_helpers
from bot_namespace import bot_name


def _row(version, conservative, *, games=700, on_disk=True):
    return {
        "name": bot_name(version),
        "conservative_rating": conservative,
        "games": games,
        "leaderboard_score": 0.5,
        "selection_score": 0.5,
        "rating": conservative + 200.0,
        "rd": 100.0,
        "on_disk": on_disk,
    }


def _setup_snapshot(monkeypatch, tmp_path, rows, *, missing_disk=()):
    import evidence_snapshot

    names = [row["name"] for row in rows]
    for row in rows:
        if row["name"] in set(missing_disk):
            continue
        bot_dir = tmp_path / "bots" / row["name"]
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(tool_helpers, "PROJECT_ROOT", tmp_path)
    frozen = {
        "available": True,
        "selection": {"rows": rows, "active_bots": list(names)},
        "h2h": {},
        "manifest": {"manifest_digest": "d" * 64},
    }
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_evaluation_snapshot",
        lambda _version: frozen,
    )
    return frozen


def _select(monkeypatch, *, mode="pool_ratchet"):
    monkeypatch.setenv("POK_PUBLISH_MODE", mode)
    return tool_helpers._select_precommit_opponents(9, 8, checkpoint={})


def test_ratchet_injects_pool_weakest_member(monkeypatch, tmp_path):
    rows = [
        _row(8, 1500.0),   # parent
        _row(5, 1400.0),
        _row(11, 1000.0),  # weakest
        _row(27, 1200.0),
    ]
    _setup_snapshot(monkeypatch, tmp_path, rows)

    selected = _select(monkeypatch)

    by_name = {item["name"]: item["reason"] for item in selected}
    assert by_name[bot_name(8)] == "parent"
    assert by_name[bot_name(11)] == "pool_weakest_gate"


def test_parent_is_weakest_still_gates_against_next_weakest(monkeypatch, tmp_path):
    """Red-team fix (pool-gate degeneration): the parent is ALWAYS excluded.

    A freshly published parent carries default ratings (conservative 800,
    nominally pool-lowest until convergence); gating against it would
    degenerate the pool gate into a plain parent re-test exactly when the
    ratchet protection is most needed.  When the parent IS the nominal
    weakest, the gate falls through to the next-weakest ESTABLISHED member.
    """
    rows = [
        _row(8, 900.0),    # parent, nominally weakest
        _row(5, 1400.0),
        _row(11, 1200.0),  # next-weakest established member
    ]
    _setup_snapshot(monkeypatch, tmp_path, rows)

    selected = _select(monkeypatch)

    by_name = {item["name"]: item["reason"] for item in selected}
    assert by_name[bot_name(11)] == "pool_weakest_gate"
    assert by_name[bot_name(8)] == "parent"


def test_missing_conservative_rating_fails_closed_with_blocker(
    monkeypatch, tmp_path
):
    rows = [
        _row(8, 1500.0),
        {"name": bot_name(5), "games": 700, "leaderboard_score": 0.5},
    ]
    _setup_snapshot(monkeypatch, tmp_path, rows)

    selected = _select(monkeypatch)

    blockers = [item for item in selected if "selection_blocker" in item]
    assert len(blockers) == 1
    assert blockers[0]["selection_blocker"]["reason"] == (
        "pool_weakest_conservative_rating_missing"
    )
    # The normal parent selection still accompanies the blocker entry.
    assert any(item.get("reason") == "parent" for item in selected)


def test_reaped_weakest_defers_to_next_weakest(monkeypatch, tmp_path):
    rows = [
        _row(8, 1500.0),
        _row(5, 900.0),    # weakest on paper, but just reaped (no disk entry)
        _row(11, 1100.0),  # next weakest
        _row(27, 1300.0),
    ]
    _setup_snapshot(monkeypatch, tmp_path, rows, missing_disk={bot_name(5)})

    selected = _select(monkeypatch)

    by_name = {item["name"]: item["reason"] for item in selected}
    assert bot_name(5) not in by_name
    assert by_name[bot_name(11)] == "pool_weakest_gate"


def test_legacy_mode_injects_no_pool_weakest(monkeypatch, tmp_path):
    rows = [
        _row(8, 1500.0),
        _row(11, 1000.0),
    ]
    _setup_snapshot(monkeypatch, tmp_path, rows)

    selected = _select(monkeypatch, mode="legacy")

    assert [
        item for item in selected if item["reason"] == "pool_weakest_gate"
    ] == []
    assert {item["name"] for item in selected} <= {bot_name(8), bot_name(11)}


def test_singleton_parent_pool_injects_nothing(monkeypatch, tmp_path):
    rows = [_row(8, 1500.0)]
    _setup_snapshot(monkeypatch, tmp_path, rows)

    selected = _select(monkeypatch)

    assert [item["reason"] for item in selected] == ["parent"]


def test_manual_reap_capture_shares_the_mode_resolved_policy(
    monkeypatch, tmp_path
):
    import tool_bot_management as tbm

    root = tmp_path
    names = [bot_name(v) for v in (1, 2, 3)]
    for name in names:
        (root / "bots" / name).mkdir(parents=True)
        (root / "bots" / name / ".completed").touch()
    results_dir = root / "web" / "core" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "bot_stats.json").write_text(
        json.dumps({name: {"games": 700} for name in names}), encoding="utf-8"
    )
    (results_dir / "priority_eval.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tbm, "PROJECT_ROOT", root)
    monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(tbm, "get_active_bots", lambda: list(names))
    monkeypatch.setattr(
        tbm,
        "load_ratings",
        lambda: {name: tbm.Glicko2Player(r=1500, rd=100) for name in names},
    )
    monkeypatch.setattr(tbm, "load_h2h_avg_winrates", lambda: {})
    monkeypatch.setattr(tbm, "load_strength_scores", lambda: {})

    # Ratchet mode: the no-argument manual capture resolves the SAME
    # mode-resolved policy/profile the handoff plan builder uses.
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    ratchet_snapshot = tbm._capture_reap_selection_snapshot()
    assert ratchet_snapshot["selection_policy"] == (
        "conservative_glicko_pool_ratchet_v1"
    )
    assert ratchet_snapshot["protection_profile"] == "pool_ratchet"
    assert ratchet_snapshot["max_active_bots"] == 12

    # Legacy mode: byte-identical historical snapshot shape.
    monkeypatch.delenv("POK_PUBLISH_MODE", raising=False)
    monkeypatch.delenv("POK_POOL_RATCHET_TARGET_SIZE", raising=False)
    legacy_snapshot = tbm._capture_reap_selection_snapshot()
    assert legacy_snapshot["selection_policy"] == "conservative_glicko_v1"
    assert "protection_profile" not in legacy_snapshot
    assert legacy_snapshot["max_active_bots"] == 30


@pytest.mark.asyncio
async def test_selection_blocker_pauses_precommit_before_plan_freeze(
    monkeypatch, tmp_path
):
    """A fail-closed selection blocker reaches an infrastructure pause."""
    import tool_eval
    from web.tests.runtime_probe_fixtures import passing_runtime_probe
    import national_runtime_probe

    candidate = tmp_path / bot_name(9)
    candidate.mkdir(parents=True)
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    opponent = tmp_path / bot_name(8)
    opponent.mkdir(parents=True)
    (opponent / "national_bot.py").write_text("# native\n", encoding="utf-8")
    checkpoint = {
        "next_v": 9,
        "source_v": 8,
        "stage": "critic_checked",
        "audit_context": {},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
                "code_fingerprint": "a" * 64,
                "national_capability_contract": {
                    "dynamic_runtime_probe": passing_runtime_probe(),
                },
                **national_runtime_probe.runtime_probe_native_template_evidence(),
            },
            "review": {
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            },
            "critic": {
                "approved": True,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            },
        },
    }
    from types import SimpleNamespace

    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
        national_precommit_hands=70,
        national_precommit_matches=2,
    )

    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda _v: candidate)
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(
        tool_eval, "_prepare_official_profile_refresh", lambda *_: {"ok": True}
    )
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)
    monkeypatch.setattr(tool_eval, "candidate_observability_identity", None)
    monkeypatch.setattr("tool_gates._bot_code_fingerprint", lambda _p: "a" * 64)
    monkeypatch.setattr(
        tool_eval,
        "_select_precommit_opponents",
        lambda *_a, **_k: [
            {"name": bot_name(8), "reason": "parent"},
            {
                "name": "",
                "reason": "pool_weakest_gate",
                "selection_blocker": {
                    "reason": "pool_weakest_conservative_rating_missing",
                    "details": "pool_snapshot_conservative_rating_missing:x",
                },
            },
        ],
    )
    backend_calls = []

    async def backend(**_kwargs):
        backend_calls.append(True)
        pytest.fail("selection blocker must pause before the backend runs")

    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", backend)

    result = await tool_eval.run_precommit_eval.handler(
        {"version": 9, "source_v": 8}
    )
    payload = json.loads(result["content"][0]["text"])

    assert payload["passed"] is False
    assert payload["failure_class"] == "infrastructure"
    assert payload["intent"]["safe_to_auto_execute"] is False
    assert payload["error"].startswith("PRECOMMIT OPPONENT SELECTION FAILED")
    assert [item["reason"] for item in payload["blockers"]] == [
        "pool_weakest_conservative_rating_missing"
    ]
    assert backend_calls == []
