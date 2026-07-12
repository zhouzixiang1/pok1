import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace


LEGACY_ACTIVE = {
    111, 112, 114, 119, 120, 121, 122, 123, 135, 141, 142
}


def test_repository_quarantine_binds_all_eleven_published_identities():
    import national_protocol_quarantine as quarantine

    policy = quarantine.load_protocol_quarantine_policy()
    report = quarantine.protocol_quarantine_health(force_refresh=True)

    assert report["valid"] is True, report["issues"]
    assert set(report["quarantined_versions"]) == LEGACY_ACTIVE
    assert len(policy["quarantined_artifacts"]) == 11
    for entry in policy["quarantined_artifacts"]:
        assert len(entry["artifact_hash"]) == 64
        assert len(entry["tag_object"]) == 40
        assert len(entry["completion_tree_oid"]) == 40
        assert report["entries"][entry["bot"]] == entry


def test_active_contract_calls_complete_current_runtime_checker(monkeypatch, tmp_path):
    import evolution_infra
    import national_native

    bot_dir = tmp_path / "national_v999"
    bot_dir.mkdir()
    (bot_dir / "national_bot.py").write_text("# candidate\n", encoding="utf-8")
    calls = []

    def check(path, **kwargs):
        calls.append((Path(path), kwargs))
        return []

    monkeypatch.setattr(evolution_infra, "BOTS_DIR", tmp_path)
    monkeypatch.setattr(national_native, "check_native_contract", check)
    monkeypatch.setattr(
        "national_position_contract.detect_position_semantics_errors",
        lambda _path: [],
    )
    evolution_infra._ACTIVE_BOT_PROTOCOL_CACHE.clear()
    health = {
        "valid": True,
        "policy_digest": "p" * 64,
        "quarantined_versions": list(LEGACY_ACTIVE),
        "issues": [],
    }

    assert evolution_infra.active_bot_protocol_errors(
        999, quarantine_health=health
    ) == []
    assert calls == [
        (
            bot_dir,
            {
                "require_current_stream_decoder": True,
                "require_current_decision_runtime": True,
            },
        )
    ]


def test_national_epoch_cannot_disable_executable_contract(monkeypatch):
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "EVALUATION_EPOCH", "national_native_v1")
    monkeypatch.setenv("POK_ACTIVE_NATIVE_CONTRACT_FILTER", "0")
    assert evolution_infra.active_native_contract_filter_enabled() is True


def test_all_legacy_artifacts_remain_published_but_leave_executable_pool(monkeypatch):
    import evolution_infra
    import national_protocol_quarantine as quarantine
    from bot_artifact import published_bot_identity

    monkeypatch.setattr(evolution_infra, "BOTS_DIR", quarantine.BOTS_DIR)
    evolution_infra._ACTIVE_BOT_PROTOCOL_CACHE.clear()
    assert evolution_infra.get_active_bots_read_only() == []
    for version in sorted(LEGACY_ACTIVE):
        path = quarantine.BOTS_DIR / f"national_v{version}"
        assert path.is_dir()
        assert published_bot_identity(path)["published"] is True
        errors = evolution_infra.active_bot_protocol_errors(version)
        assert any("protocol_quarantined_historical_artifact" in item for item in errors)


def test_policy_identity_drift_fails_closed(tmp_path):
    import national_protocol_quarantine as quarantine

    policy = quarantine.load_protocol_quarantine_policy()
    policy["quarantined_artifacts"][-1]["artifact_hash"] = "0" * 64
    path = tmp_path / "quarantine.json"
    path.write_text(json.dumps(policy), encoding="utf-8")

    report = quarantine.protocol_quarantine_health(
        policy_path=path,
        bots_dir=quarantine.BOTS_DIR,
        force_refresh=True,
    )
    assert report["valid"] is False
    assert "national_v142:artifact_hash_mismatch" in report["issues"]
    selected = quarantine.select_protocol_bootstrap_source(
        [],
        policy_path=path,
        bots_dir=quarantine.BOTS_DIR,
        force_refresh=True,
    )
    assert selected["available"] is False
    assert selected["reason"] == "protocol_quarantine_policy_invalid"


def test_quarantine_matches_raw_entry_bytes_after_directory_rename(tmp_path):
    import national_protocol_quarantine as quarantine

    renamed = tmp_path / "innocent_name"
    renamed.mkdir()
    source = quarantine.BOTS_DIR / "national_v142" / "national_bot.py"
    (renamed / "national_bot.py").write_bytes(source.read_bytes())

    matches = quarantine.quarantined_native_entry_sources(renamed)
    assert "national_v142" in matches


def test_migration_seed_closes_at_first_strict_publication(monkeypatch):
    import national_protocol_quarantine as quarantine

    real = quarantine.protocol_quarantine_health(force_refresh=True)
    monkeypatch.setattr(
        quarantine,
        "protocol_quarantine_health",
        lambda **_kwargs: real,
    )
    strict = []
    monkeypatch.setattr(
        quarantine,
        "strict_published_bot_names",
        lambda **_kwargs: tuple(strict),
    )

    migration = quarantine.select_protocol_bootstrap_source([])
    assert migration["available"] is True
    assert migration["source_v"] == 142
    assert migration["receipt"]["mode"] == "legacy_strategy_migration"
    assert quarantine.validate_protocol_bootstrap_receipt(
        migration["receipt"], active_bots=[]
    ) == []

    strict.append("national_v150")
    monkeypatch.setattr(
        quarantine,
        "_published_identity",
        lambda path: {
            "published": True,
            "artifact_hash": "1" * 64,
            "tag": "national-bot-v150",
            "tag_object": "2" * 40,
            "completion_tree_oid": "3" * 40,
        },
    )
    assert quarantine.select_protocol_bootstrap_source([])["reason"] == (
        "strict_publication_active_pool_mismatch"
    )
    singleton = quarantine.select_protocol_bootstrap_source(["national_v150"])
    assert singleton["available"] is True
    assert singleton["source_v"] == 150
    assert singleton["receipt"]["mode"] == "singleton_strict_pool"
    assert quarantine.validate_protocol_bootstrap_receipt(
        migration["receipt"], active_bots=["national_v150"]
    )


def test_health_cache_reuses_and_invalidates_identity_checks(monkeypatch):
    import national_protocol_quarantine as quarantine

    policy = quarantine.load_protocol_quarantine_policy()
    by_label = {entry["bot"]: entry for entry in policy["quarantined_artifacts"]}
    cache_key = ["snapshot-a"]
    calls = []
    monkeypatch.setattr(
        quarantine,
        "_health_cache_key",
        lambda *_args: tuple(cache_key),
    )
    monkeypatch.setattr(
        quarantine,
        "load_protocol_quarantine_policy",
        lambda _path=None: policy,
    )

    def identity(path):
        calls.append(path.name)
        entry = by_label[path.name]
        return {
            "published": True,
            **{field: entry[field] for field in quarantine._IDENTITY_FIELDS},
            "issues": [],
        }

    monkeypatch.setattr(quarantine, "_published_identity", identity)
    with quarantine._HEALTH_CACHE_LOCK:
        quarantine._HEALTH_CACHE.update(key=None, checked_at=0.0, report=None)

    assert quarantine.protocol_quarantine_health()["valid"] is True
    assert len(calls) == 11
    assert quarantine.protocol_quarantine_health()["valid"] is True
    assert len(calls) == 11
    cache_key[0] = "snapshot-b"
    assert quarantine.protocol_quarantine_health()["valid"] is True
    assert len(calls) == 22
    assert quarantine.protocol_quarantine_health(force_refresh=True)["valid"] is True
    assert len(calls) == 33


def test_health_cache_cold_start_is_single_flight(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    import national_protocol_quarantine as quarantine

    policy = quarantine.load_protocol_quarantine_policy()
    by_label = {entry["bot"]: entry for entry in policy["quarantined_artifacts"]}
    calls = []
    monkeypatch.setattr(quarantine, "_health_cache_key", lambda *_args: ("stable",))
    monkeypatch.setattr(
        quarantine,
        "load_protocol_quarantine_policy",
        lambda _path=None: policy,
    )

    def identity(path):
        time.sleep(0.002)
        calls.append(path.name)
        entry = by_label[path.name]
        return {
            "published": True,
            **{field: entry[field] for field in quarantine._IDENTITY_FIELDS},
            "issues": [],
        }

    monkeypatch.setattr(quarantine, "_published_identity", identity)
    with quarantine._HEALTH_CACHE_LOCK:
        quarantine._HEALTH_CACHE.update(key=None, checked_at=0.0, report=None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(lambda _item: quarantine.protocol_quarantine_health(), range(4)))

    assert all(report["valid"] for report in reports)
    assert len(calls) == 11


def test_scheduler_zero_and_one_bot_bootstrap_never_waits_for_rating(monkeypatch):
    import evolution_infra
    import generation_scheduler
    import national_protocol_quarantine as quarantine

    writes = []
    monkeypatch.setattr(
        evolution_infra,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )
    receipt = quarantine.select_protocol_bootstrap_source([])["receipt"]
    monkeypatch.setattr(
        quarantine,
        "select_protocol_bootstrap_source",
        lambda active, **_kwargs: {
            "available": True,
            "source_v": 142 if not active else 150,
            "receipt": {
                **receipt,
                "mode": (
                    "legacy_strategy_migration"
                    if not active
                    else "singleton_strict_pool"
                ),
                "source": {
                    **receipt["source"],
                    "bot": "national_v142" if not active else "national_v150",
                    "version": 142 if not active else 150,
                },
            },
        },
    )

    zero = generation_scheduler._prepare_protocol_bootstrap_generation(
        active_bots=[],
        current_v=142,
        next_v=150,
        max_committed_v=149,
        abandoned_floor=149,
        workflow_run_id="generation:150:workflow-v1",
    )
    one = generation_scheduler._prepare_protocol_bootstrap_generation(
        active_bots=["national_v150"],
        current_v=150,
        next_v=151,
        max_committed_v=150,
        abandoned_floor=149,
        workflow_run_id="generation:151:workflow-v1",
    )
    assert zero.strategy == "protocol_migration_bootstrap"
    assert zero.source_v == 142
    assert one.strategy == "singleton_strict_bootstrap"
    assert one.source_v == 150
    assert [call[0][2] for call in writes] == ["selected", "selected"]
    assert [call[1]["workflow_run_id"] for call in writes] == [
        "generation:150:workflow-v1",
        "generation:151:workflow-v1",
    ]
    assert all(
        call[1]["audit_context"]["selection"]["bootstrap_without_strength_evidence"]
        for call in writes
    )


def test_prepare_migration_replaces_runtime_before_artifact_snapshot(
    monkeypatch, tmp_path
):
    import evolution_infra
    import fix_injection
    import prepared_baseline_contract
    import tool_gates
    from national_native import check_native_contract

    source = tmp_path / "national_v142"
    target = tmp_path / "national_v150"
    source.mkdir()
    (source / "national_bot.py").write_text(
        "LEGACY_RUNTIME_MUST_NOT_REACH_SNAPSHOT = True\n", encoding="utf-8"
    )
    (source / "main.py").write_text(
        "def sanitize_action(action, state, my_chips):\n    return int(action)\n",
        encoding="utf-8",
    )
    (source / "state.py").write_text(
        "def infer_remaining_hands_from_requests(requests):\n"
        "    return max(0, 70-len(requests))\n\n"
        "def reconstruct_state(req):\n    return dict(req)\n",
        encoding="utf-8",
    )
    (source / "strategy.py").write_text(
        "def get_action(req, requests):\n    return 0\n",
        encoding="utf-8",
    )
    receipt = {
        "schema_version": 1,
        "kind": "national-native-protocol-bootstrap-source",
        "mode": "legacy_strategy_migration",
        "source": {"version": 142},
        "receipt_digest": "r" * 64,
    }
    checkpoint = {
        "next_v": 150,
        "source_v": 142,
        "stage": "selected",
        "audit_context": {"protocol_bootstrap": receipt},
    }
    writes = []
    snapshot_observations = []

    monkeypatch.setattr(tool_gates, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tool_gates, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_gates,
        "get_bot_dir",
        lambda version: source if int(version) == 142 else target,
    )
    monkeypatch.setattr(tool_gates, "find_current_v", lambda: 149)
    monkeypatch.setattr(tool_gates, "log_system_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tool_gates,
        "get_workflow_profile",
        lambda: SimpleNamespace(national_execution_mode="native_tcp"),
    )
    monkeypatch.setattr(evolution_infra, "git_has_tag", lambda _version: True)
    monkeypatch.setattr(evolution_infra, "git_dir_is_committed", lambda _version: False)
    monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: [])
    monkeypatch.setattr(
        evolution_infra,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        "national_protocol_quarantine.validate_protocol_bootstrap_receipt",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
    monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_args: None)

    def snapshot(path, **_kwargs):
        text = (Path(path) / "national_bot.py").read_text(encoding="utf-8")
        snapshot_observations.append(text)
        return {"artifact_hash": "a" * 64}

    monkeypatch.setattr(
        prepared_baseline_contract,
        "build_prepared_artifact_contract",
        snapshot,
    )

    raw_result = asyncio.run(
        tool_gates.prepare_next_gen.handler({"source_v": 142, "next_v": 150})
    )
    result = json.loads(raw_result["content"][0]["text"])
    assert result["prepared"] is True
    assert len(snapshot_observations) == 1
    assert "LEGACY_RUNTIME_MUST_NOT_REACH_SNAPSHOT" not in snapshot_observations[0]
    assert check_native_contract(
        target,
        require_current_stream_decoder=True,
        require_current_decision_runtime=True,
    ) == []
    prepared_write = writes[-1][1]
    prepare_receipt = prepared_write["audit_context"]["protocol_bootstrap_prepare"]
    assert prepare_receipt["system_runtime_replaced"] is True
    assert len(prepare_receipt["national_bot_sha256"]) == 64


def test_master_accepts_bootstrap_receipt_without_fabricating_h2h_snapshot(
    monkeypatch, tmp_path
):
    import evolution_infra
    import tool_planning
    from national_native import ensure_native_entry

    candidate = tmp_path / "national_v150"
    candidate.mkdir()
    (candidate / "main.py").write_text(
        "def sanitize_action(action, state, my_chips):\n    return int(action)\n",
        encoding="utf-8",
    )
    (candidate / "state.py").write_text(
        "def infer_remaining_hands_from_requests(requests):\n"
        "    return max(0, 70-len(requests))\n\n"
        "def reconstruct_state(req):\n    return dict(req)\n",
        encoding="utf-8",
    )
    (candidate / "strategy.py").write_text(
        "def get_action(req, requests):\n    return 0\n",
        encoding="utf-8",
    )
    entry = ensure_native_entry(candidate, overwrite=True)
    entry_hash = __import__("hashlib").sha256(entry.read_bytes()).hexdigest()
    receipt = {
        "mode": "legacy_strategy_migration",
        "receipt_digest": "r" * 64,
    }
    checkpoint = {
        "source_v": 142,
        "audit_context": {
            "protocol_bootstrap": receipt,
            "protocol_bootstrap_prepare": {
                "receipt_digest": receipt["receipt_digest"],
                "system_runtime_replaced": True,
                "national_bot_sha256": entry_hash,
            },
            "selection": {"bootstrap_without_strength_evidence": True},
            "master_context": {},
        },
    }
    monkeypatch.setattr(tool_planning, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: [])
    monkeypatch.setattr(
        "national_protocol_quarantine.validate_protocol_bootstrap_receipt",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "evidence_snapshot.load_generation_snapshot_identity",
        lambda _version: (_ for _ in ()).throw(
            AssertionError("bootstrap must not request a fabricated strength snapshot")
        ),
    )

    assert tool_planning._master_snapshot_binding_errors(checkpoint, 150) == []
    checkpoint["audit_context"]["protocol_bootstrap_prepare"][
        "national_bot_sha256"
    ] = "0" * 64
    assert "protocol_bootstrap_runtime_hash_mismatch" in (
        tool_planning._master_snapshot_binding_errors(checkpoint, 150)
    )


def test_bootstrap_direction_audit_is_deterministic_and_never_calls_history_llm(
    monkeypatch,
):
    import tool_planning

    checkpoint = {
        "next_v": 150,
        "source_v": 142,
        "stage": "prepared",
        "audit_context": {
            "protocol_bootstrap": {"receipt_digest": "a" * 64},
            "protocol_bootstrap_prepare": {"receipt_digest": "a" * 64},
            "prepared_artifact_contract": {"artifact_hash": "b" * 64},
        },
    }
    writes = []
    events = []

    class UI:
        def log_history(self, *_args, **_kwargs):
            return None

        def get_output(self):
            return ""

    async def forbidden_history_audit(*_args, **_kwargs):
        raise AssertionError("bootstrap Direction must not load historical evidence")

    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(tool_planning, "_run_direction_audit", forbidden_history_audit)
    monkeypatch.setattr(tool_planning, "_get_ui", UI)
    monkeypatch.setattr(tool_planning, "_set_pipeline_status", lambda *_args: None)
    monkeypatch.setattr(
        tool_planning,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        tool_planning,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    raw = asyncio.run(
        tool_planning.run_direction_audit.handler(
            {"source_v": 142, "next_v": 150}
        )
    )
    result = json.loads(raw["content"][0]["text"])

    audit = result["direction_audit"]
    assert audit["protocol_bootstrap_no_strength"] is True
    assert audit["confidence"] == "not_applicable"
    assert audit["exhausted_directions"] == []
    assert audit["prepared_artifact_hash"] == "b" * 64
    assert len(audit["receipt_digest"]) == 64
    assert writes[0][0][:3] == (150, 142, "direction_audited")
    assert events[0][0][0] == "pipeline.direction_audit_protocol_bootstrap_neutral"
