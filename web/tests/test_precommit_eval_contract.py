from pathlib import Path
from types import SimpleNamespace

import pytest

import precommit_eval_contract as contract
import tool_eval


def _published(path: Path, *, artifact_hash: str = "a" * 64) -> dict:
    return {
        "published": True,
        "artifact_hash": artifact_hash,
        "tag": f"national-bot-v{path.name.removeprefix('national_v')}",
        "tag_type": "tag",
        "tag_object": "b" * 40,
        "commit_oid": "c" * 40,
        "completion_tree_oid": "d" * 40,
        "main_tree_oid": "d" * 40,
        "issues": [],
    }


def _make_plan(tmp_path, monkeypatch, *, matches=3):
    opponents = []
    identities = {}
    for version in (8, 5):
        path = tmp_path / f"national_v{version}"
        path.mkdir()
        (path / "national_bot.py").write_text("# native\n", encoding="utf-8")
        opponents.append({"name": path.name, "reason": "parent" if version == 8 else "top"})
        identities[str(path)] = _published(path, artifact_hash=str(version) * 64)

    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda path: dict(identities[str(Path(path))]),
    )
    plan = contract.create_precommit_plan(
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
        opponents=opponents,
        hands_per_match=70,
        matches_per_opponent=matches,
        path_resolver=lambda item: tmp_path / item["name"],
        require_published_opponents=True,
    )
    return plan, identities


def test_plan_freezes_order_identity_and_all_sample_seeds(tmp_path, monkeypatch):
    plan, _ = _make_plan(tmp_path, monkeypatch, matches=3)

    assert [row["name"] for row in plan["opponents"]] == ["national_v8", "national_v5"]
    assert len(plan["sample_plan"]) == 6
    assert plan["sample_plan"][0] == {
        "opponent": "national_v8",
        "opponent_index": 0,
        "repeat": 1,
        "deck_seed_base": 91_000,
        "bot_seed_base": 1_000_091_000,
    }
    assert plan["sample_plan"][-1]["deck_seed_base"] == 193_000
    assert contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    ) == []


def test_native_plan_rejects_shortened_strength_matches(tmp_path, monkeypatch):
    opponent = tmp_path / "national_v8"
    opponent.mkdir()
    (opponent / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda _path: _published(opponent),
    )

    with pytest.raises(
        contract.PrecommitEvalContractError,
        match="exactly 70 hands",
    ):
        contract.create_precommit_plan(
            candidate_version=9,
            source_version=8,
            profile_id="national_native",
            execution_mode="native_tcp",
            evaluation_protocol="national",
            opponents=[{"name": "national_v8", "reason": "parent"}],
            hands_per_match=3,
            matches_per_opponent=8,
            path_resolver=lambda _item: opponent,
            require_published_opponents=True,
        )


def test_plan_fails_closed_when_published_opponent_identity_drifts(tmp_path, monkeypatch):
    plan, identities = _make_plan(tmp_path, monkeypatch)
    identities[str(tmp_path / "national_v8")]["artifact_hash"] = "f" * 64

    issues = contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    )

    assert "precommit_opponent_national_v8_identity_drift" in issues


def test_evaluation_contract_binds_candidate_code_and_frozen_plan(tmp_path, monkeypatch):
    plan, _ = _make_plan(tmp_path, monkeypatch)
    first = contract.build_evaluation_contract(
        plan,
        candidate_code_fingerprint="candidate-a",
    )
    assert contract.validate_evaluation_contract(
        first,
        plan,
        candidate_code_fingerprint="candidate-a",
    ) == []
    assert contract.validate_evaluation_contract(
        first,
        plan,
        candidate_code_fingerprint="candidate-b",
    ) == ["precommit_evaluation_contract_mismatch"]


@pytest.mark.parametrize(
    ("asset_name", "before_bytes", "after_bytes"),
    [
        ("ranges.json", b'{"open": ["AA"]}\n', b'{"open": ["AA", "KK"]}\n'),
        ("policy.model", b"model-v1\x00weights", b"model-v2\x00weights"),
        ("equity_table.txt", b"AA=0.85\n", b"AA=0.81\n"),
    ],
)
def test_precommit_candidate_fingerprint_covers_non_python_decision_assets(
    tmp_path,
    asset_name,
    before_bytes,
    after_bytes,
):
    from tool_gates import _bot_code_fingerprint

    candidate = tmp_path / "national_v9"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    asset = candidate / asset_name
    asset.write_bytes(before_bytes)
    before = _bot_code_fingerprint(candidate)
    frozen = contract.build_evaluation_contract(
        {"plan_digest": "frozen-plan"},
        candidate_code_fingerprint=before,
    )

    asset.write_bytes(after_bytes)
    after = _bot_code_fingerprint(candidate)

    assert after != before
    assert contract.validate_evaluation_contract(
        frozen,
        {"plan_digest": "frozen-plan"},
        candidate_code_fingerprint=after,
    ) == ["precommit_evaluation_contract_mismatch"]


def test_precommit_candidate_fingerprint_ignores_runtime_markers_and_caches(tmp_path):
    from tool_gates import _bot_code_fingerprint

    candidate = tmp_path / "national_v9"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    before = _bot_code_fingerprint(candidate)

    (candidate / ".completed").write_text("runtime marker\n", encoding="utf-8")
    cache = candidate / "__pycache__"
    cache.mkdir()
    (cache / "national_bot.cpython-314.pyc").write_bytes(b"runtime cache")

    assert _bot_code_fingerprint(candidate) == before


def test_plan_digest_detects_seed_schedule_tampering(tmp_path, monkeypatch):
    plan, _ = _make_plan(tmp_path, monkeypatch)
    plan["sample_plan"][0]["deck_seed_base"] += 1

    issues = contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    )

    assert "precommit_plan_digest_mismatch" in issues
    assert "precommit_sample_plan_mismatch" in issues


@pytest.mark.asyncio
async def test_tool_reuses_frozen_opponents_when_live_selection_changes(tmp_path, monkeypatch):
    bots = tmp_path / "bots"
    for version in (9, 8, 5, 4):
        bot_dir = bots / f"national_v{version}"
        bot_dir.mkdir(parents=True)
        (bot_dir / "national_bot.py").write_text("# native\n", encoding="utf-8")

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
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
        national_precommit_hands=70,
        national_precommit_matches=2,
    )
    selected = [
        {"name": "national_v8", "reason": "parent"},
        {"name": "national_v5", "reason": "top_strength"},
    ]
    backend_calls = []

    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda version: bots / f"national_v{version}")
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(tool_eval, "_prepare_official_profile_refresh", lambda *_: {"ok": True})
    monkeypatch.setattr("tool_helpers._active_workflow_profile_info", lambda: ("national_native", "native_tcp"))
    monkeypatch.setattr(tool_eval, "_select_precommit_opponents", lambda *_: list(selected))
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)
    monkeypatch.setattr("tool_gates._bot_code_fingerprint", lambda _path: "candidate-code")
    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda path: _published(Path(path), artifact_hash=Path(path).name[-1] * 64),
    )

    def fake_write(_v, _source_v, stage, **kwargs):
        checkpoint["stage"] = stage
        if kwargs.get("audit_context"):
            checkpoint["audit_context"].update(kwargs["audit_context"])
        if kwargs.get("precommit_attempt") is not None:
            checkpoint["precommit_attempt"] = kwargs["precommit_attempt"]
        return True

    async def fake_backend(**kwargs):
        backend_calls.append(kwargs)
        return tool_eval._json_tool_result({"passed": True})

    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", fake_write)
    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", fake_backend)

    await tool_eval.run_precommit_eval.handler({"version": 9, "source_v": 8, "n_games": 2})
    assert [row["name"] for row in backend_calls[0]["opponents"]] == [
        "national_v8",
        "national_v5",
    ]

    selected[:] = [{"name": "national_v4", "reason": "new_live_leader"}]
    await tool_eval.run_precommit_eval.handler({"version": 9, "source_v": 8, "n_games": 16})

    assert [row["name"] for row in backend_calls[1]["opponents"]] == [
        "national_v8",
        "national_v5",
    ]
    assert backend_calls[1]["precommit_plan"] == backend_calls[0]["precommit_plan"]
    assert backend_calls[1]["effective_n_games"] == 4
