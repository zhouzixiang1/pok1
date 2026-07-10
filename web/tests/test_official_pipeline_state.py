from pipeline_state import STAGE_GATE_ALLOWLIST, route_policy, validate_stage_transition


def test_verified_stage_retains_content_bound_official_gate():
    assert "official_full" in STAGE_GATE_ALLOWLIST["verified"]


def test_official_failed_routes_to_worker_repair():
    route = route_policy({
        "stage": "official_failed",
        "next_v": 134,
        "source_v": 120,
        "gate_results": {"official_full": {"passed": False}},
    })

    assert route["next_tool"] == "execute_workers"
    assert route["intent"] == "official_rework"
    assert "Official EXE full certification" in route["directive"]
    assert validate_stage_transition("verified", "official_failed")[0] is True
    assert validate_stage_transition("official_failed", "repair_planned")[0] is True


def test_official_inconclusive_has_no_automatic_commit_retry():
    route = route_policy({
        "stage": "official_inconclusive",
        "next_v": 134,
        "source_v": 120,
        "gate_results": {"official_full": {"passed": False}},
    })

    assert route["next_tool"] is None
    assert route["allowed_tools"] == []
    assert "Do not call commit_bot" in route["directive"]


def test_official_inconclusive_recovery_is_blocked(tmp_path):
    from pipeline_recovery import checkpoint_recovery_diagnostics

    root = tmp_path
    (root / "bots" / "national_v134").mkdir(parents=True)
    checkpoint = {
        "stage": "official_inconclusive",
        "next_v": 134,
        "source_v": 120,
        "repo_baseline": {"branch": "main", "head": "abc123"},
    }
    snapshot = {"ok": True, "branch": "main", "head": "abc123", "entries": []}

    diag = checkpoint_recovery_diagnostics(checkpoint, snapshot=snapshot, project_root=root)

    assert diag["active"] is True
    assert diag["recoverable"] is False
    assert "official_inconclusive_requires_infra_intervention" in diag["issues"]
