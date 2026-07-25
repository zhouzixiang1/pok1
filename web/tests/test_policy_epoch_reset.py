import json

import pytest

from bot_namespace import bot_name, bot_tag, high_water_tag
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V, strict_bot_name, strict_bot_tag
from scripts import reset_national_tcp_policy_epoch as reset
from evaluation_contract import ALWAYS_CRITICAL_EXACT


def _git_at_archived_high_water(*args):
    """Simulate the empty strict-policy namespace at the archived high-water floor.

    On this branch the archived high-water is STRICT_SOURCE_V (0 on cloud, 142 on
    main) and the first strict target is STRICT_TARGET_V.  A bootstrap-time reset
    sees NO paired completion/high-water tags yet (an empty namespace), which the
    reset script treats as sitting at ARCHIVED_VERSION_HIGH_WATER, ready for a
    fresh first-strict reset.  Tag v0 is not representable (the parser rejects a
    leading zero), so the empty-namespace case is the canonical pre-reset state.
    """
    if args[:2] == ("rev-parse", "HEAD"):
        return "a" * 40
    return ""


def test_reset_script_is_always_evaluation_contract_critical():
    assert "scripts/reset_national_tcp_policy_epoch.py" in ALWAYS_CRITICAL_EXACT


def test_policy_epoch_reset_has_no_source_seed(monkeypatch, tmp_path):
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", tmp_path / "web" / "core")
    monkeypatch.setattr(reset, "_git", _git_at_archived_high_water)
    monkeypatch.setattr(
        reset,
        "build_plan",
        lambda _stamp: {
            "archive_root": tmp_path / "archive",
            "runtime": [],
            "archived_bot_dirs": [],
        },
    )

    receipt = reset.run(execute=False)

    assert receipt["epoch"] == "national_tcp_policy_v1"
    assert receipt["first_target_version"] == STRICT_TARGET_V
    assert receipt["version_authority_high_water"] == STRICT_SOURCE_V
    assert receipt["source_code_inherited"] is False
    assert receipt["seed_bot"] is None
    assert receipt["schema_version"] == 2
    assert len(receipt["receipt_digest"]) == 64
    assert not (tmp_path / reset.RESET_ARCHIVE_RELATIVE).exists()


def test_policy_epoch_reset_refuses_rerun_after_strict_tag(monkeypatch):
    def git(*args):
        if args and args[0] == "for-each-ref":
            return (
                f"tag\tcommit\t{strict_bot_tag()}\n"
                f"tag\tcommit\t{high_water_tag(STRICT_TARGET_V)}"
            )
        if args[:3] == ("tag", "-l", "national-bot-v*"):
            return strict_bot_tag()
        if args[:3] == ("tag", "-l", "national-high-water-v*"):
            return high_water_tag(STRICT_TARGET_V)
        return "a" * 40

    monkeypatch.setattr(reset, "_git", git)

    with pytest.raises(RuntimeError, match="cannot rerun"):
        reset.run(execute=False)


def test_policy_epoch_reset_ignores_lightweight_version_claim(monkeypatch, tmp_path):
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", tmp_path / "web" / "core")
    monkeypatch.setattr(
        reset,
        "_git",
        lambda *args: (
            # An annotated strict completion tag plus a lightweight (unpeeled)
            # claim on a far-future version.  Only the annotated paired tag is
            # version authority; the lightweight claim is debris.
            f"commit\t\t{bot_tag(10 * STRICT_TARGET_V)}\n"
            if args and args[0] == "for-each-ref"
            else ("a" * 40 if args and args[0] == "rev-parse" else "")
        ),
    )
    monkeypatch.setattr(
        reset,
        "build_plan",
        lambda _stamp: {
            "archive_root": tmp_path / "archive",
            "runtime": [],
            "archived_bot_dirs": [],
        },
    )

    receipt = reset.run(execute=False)

    assert receipt["version_authority_high_water"] == STRICT_SOURCE_V


def test_policy_epoch_reset_treats_lightweight_only_namespace_as_archived_floor(
    monkeypatch,
):
    # A lightweight (unpeeled, objecttype=commit) high-water tag is NOT paired
    # annotated version authority, so resolve_version_namespace_authority raises.
    # On this branch an empty/lightweight-only namespace is the legitimate
    # bootstrap start for an isolated deployment namespace: the reset proceeds at
    # the archived high-water floor rather than failing.  Only an annotated
    # paired tag advances the namespace and blocks the one-time reset (covered
    # by test_policy_epoch_reset_refuses_rerun_after_strict_tag).
    monkeypatch.setattr(
        reset,
        "_git",
        lambda *args: (
            f"commit\t\t{high_water_tag(STRICT_TARGET_V + 5)}"
            if args and args[0] == "for-each-ref"
            else ("a" * 40 if args and args[0] == "rev-parse" else "")
        ),
    )

    receipt = reset.run(execute=False)
    assert receipt["version_authority_high_water"] == STRICT_SOURCE_V
    assert receipt["first_target_version"] == STRICT_TARGET_V


def test_reset_plan_archives_pre_policy_and_untagged_high_version_debris(
    monkeypatch, tmp_path
):
    bots = tmp_path / "bots"
    # Use active-namespace bot directories so they parse on this branch.  The
    # archived high-water floor is STRICT_SOURCE_V (0 on cloud), so every
    # parseable strict bot (version >= STRICT_TARGET_V) is untagged high-version
    # debris relative to a fresh first-strict reset.
    (bots / strict_bot_name(STRICT_TARGET_V)).mkdir(parents=True)
    (bots / strict_bot_name(STRICT_TARGET_V + 1)).mkdir()
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "RUNTIME_DIRS", ())

    plan = reset.build_plan("stamp")

    assert [item["source"].name for item in plan["archived_bot_dirs"]] == [
        strict_bot_name(STRICT_TARGET_V),
        strict_bot_name(STRICT_TARGET_V + 1),
    ]
    assert [item["disposition"] for item in plan["archived_bot_dirs"]] == [
        "stale_unpublished_high_version_candidate",
        "stale_unpublished_high_version_candidate",
    ]


def test_reset_receipt_marks_stale_v155_untrusted(monkeypatch, tmp_path):
    stale_dir = strict_bot_name(10 * STRICT_TARGET_V + 4)
    bots = tmp_path / "bots"
    (bots / stale_dir).mkdir(parents=True)
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", tmp_path / "web" / "core")
    monkeypatch.setattr(reset, "RUNTIME_DIRS", ())
    monkeypatch.setattr(reset, "_git", _git_at_archived_high_water)

    receipt = reset.run(execute=False)

    assert receipt["first_target_version"] == STRICT_TARGET_V
    assert len(receipt["archived_bot_debris"]) == 1
    item = receipt["archived_bot_debris"][0]
    assert item["from"] == f"bots/{stale_dir}"
    assert item["to"].endswith(f"/bot_debris/{stale_dir}")
    assert item["trust"] == "archived_non_executable"
    assert item["disposition"] == "stale_unpublished_high_version_candidate"


def test_execute_archives_stale_v155_and_checkpoint_before_fresh_v143(
    monkeypatch, tmp_path
):
    core = tmp_path / "web" / "core"
    results = core / "results"
    stale_dir = strict_bot_name(10 * STRICT_TARGET_V + 4)
    candidate = tmp_path / "bots" / stale_dir
    results.mkdir(parents=True)
    candidate.mkdir(parents=True)
    (results / "pipeline_state.json").write_text(
        '{"next_v":155,"stage":"direction_audited"}\n',
        encoding="utf-8",
    )
    (candidate / "main.py").write_text("# retired wrapper\n", encoding="utf-8")
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", core)
    monkeypatch.setattr(
        reset,
        "RUNTIME_DIRS",
        (("web_core_results", results),),
    )
    monkeypatch.setattr(reset, "_git", _git_at_archived_high_water)
    monkeypatch.setattr(reset, "_runtime_checkout_identity_errors", lambda: [])

    receipt = reset.run(execute=True, acknowledge_runtime_checkout=True)
    from system_strict_bootstrap import (
        validate_policy_epoch_reset_archive,
        validate_policy_epoch_reset_receipt,
    )

    assert validate_policy_epoch_reset_receipt(receipt) == []
    assert validate_policy_epoch_reset_archive(receipt, project_root=tmp_path) == []
    assert not candidate.exists()
    assert not (results / "pipeline_state.json").exists()
    assert not (tmp_path / "bots" / strict_bot_name()).exists()
    reset_receipt = results / "policy_epoch_reset_receipt.json"
    assert reset_receipt.is_file()
    archived_stale = tmp_path / receipt["archived_bot_debris"][0]["to"]
    archived_results = tmp_path / receipt["archived_runtime"][0]["to"]
    assert (archived_stale / "main.py").is_file()
    assert (archived_results / "pipeline_state.json").is_file()
    assert receipt["first_target_version"] == STRICT_TARGET_V


def test_execute_archives_web_logs_and_binds_fresh_log_directory(
    monkeypatch, tmp_path
):
    core = tmp_path / "web" / "core"
    results = core / "results"
    logs = tmp_path / "web" / "logs"
    results.mkdir(parents=True)
    logs.mkdir(parents=True)
    old_log = logs / "orchestrator_20260701_000000.txt"
    old_log.write_text("retired conversation\n", encoding="utf-8")
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", core)
    monkeypatch.setattr(
        reset,
        "RUNTIME_DIRS",
        (("web_core_results", results), ("web_logs", logs)),
    )
    monkeypatch.setattr(reset, "_git", _git_at_archived_high_water)
    monkeypatch.setattr(reset, "_runtime_checkout_identity_errors", lambda: [])

    receipt = reset.run(execute=True, acknowledge_runtime_checkout=True)

    from log_epoch import (
        LOG_EPOCH_MARKER_FILENAME,
        load_current_log_epoch_identity,
    )

    archived_logs = next(
        row for row in receipt["archived_runtime"] if row["label"] == "web_logs"
    )
    assert (tmp_path / archived_logs["to"] / old_log.name).is_file()
    assert not old_log.exists()
    assert (logs / LOG_EPOCH_MARKER_FILENAME).is_file()
    identity = load_current_log_epoch_identity(results, logs)
    assert identity is not None
    assert identity["policy_epoch_reset_receipt_digest"] == receipt["receipt_digest"]


def test_execute_refuses_second_receipt_before_v143_is_tagged(monkeypatch, tmp_path):
    core = tmp_path / "web" / "core"
    results = core / "results"
    results.mkdir(parents=True)
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", core)
    monkeypatch.setattr(reset, "RUNTIME_DIRS", (("web_core_results", results),))
    monkeypatch.setattr(reset, "_git", _git_at_archived_high_water)
    monkeypatch.setattr(reset, "_runtime_checkout_identity_errors", lambda: [])

    first = reset.run(execute=True, acknowledge_runtime_checkout=True)

    with pytest.raises(RuntimeError, match="refusing to mint a second receipt"):
        reset.run(execute=True, acknowledge_runtime_checkout=True)

    live = json.loads(
        (results / reset.RESET_RECEIPT_FILENAME).read_text(encoding="utf-8")
    )
    assert live == first


def test_interrupted_claim_blocks_reexecution(monkeypatch, tmp_path):
    core = tmp_path / "web" / "core"
    results = core / "results"
    results.mkdir(parents=True)
    archive = tmp_path / reset.RESET_ARCHIVE_RELATIVE / "interrupted"
    archive.mkdir(parents=True)
    (archive / reset.RESET_CLAIM_FILENAME).write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", core)
    monkeypatch.setattr(reset, "RUNTIME_DIRS", (("web_core_results", results),))
    monkeypatch.setattr(reset, "_git", _git_at_archived_high_water)
    monkeypatch.setattr(reset, "_runtime_checkout_identity_errors", lambda: [])

    with pytest.raises(RuntimeError, match="interrupted"):
        reset.run(execute=True, acknowledge_runtime_checkout=True)


def test_failed_final_receipt_publish_leaves_claim_and_blocks_retry(
    monkeypatch, tmp_path
):
    core = tmp_path / "web" / "core"
    results = core / "results"
    results.mkdir(parents=True)
    (results / "pipeline_state.json").write_text(
        '{"next_v":155,"stage":"direction_audited"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", core)
    monkeypatch.setattr(reset, "RUNTIME_DIRS", (("web_core_results", results),))
    monkeypatch.setattr(reset, "_git", _git_at_archived_high_water)
    monkeypatch.setattr(reset, "_runtime_checkout_identity_errors", lambda: [])

    def fail_final_publish(_path, _payload):
        raise OSError("injected final receipt publication failure")

    monkeypatch.setattr(reset, "_replace_json", fail_final_publish)

    with pytest.raises(RuntimeError, match="interrupted after the durable"):
        reset.run(execute=True, acknowledge_runtime_checkout=True)

    live_claim = json.loads(
        (results / reset.RESET_RECEIPT_FILENAME).read_text(encoding="utf-8")
    )
    assert live_claim["kind"] == "national_tcp_policy_epoch_reset_claim"
    archive_root = tmp_path / live_claim["archive_root"]
    assert (archive_root / reset.RESET_CLAIM_FILENAME).is_file()
    assert (archive_root / reset.ARCHIVE_RESET_RECEIPT_FILENAME).is_file()
    assert (archive_root / "web_core_results" / "pipeline_state.json").is_file()

    with pytest.raises(RuntimeError, match="refusing to mint a second receipt"):
        reset.run(execute=True, acknowledge_runtime_checkout=True)


def test_execute_requires_runtime_checkout_ack_and_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", tmp_path / "web" / "core")
    monkeypatch.setattr(reset, "RUNTIME_DIRS", ())
    monkeypatch.setattr(reset, "_git", _git_at_archived_high_water)

    with pytest.raises(RuntimeError, match="acknowledge-runtime-checkout"):
        reset.run(execute=True)
    with pytest.raises(
        RuntimeError,
        match="requires_autonomous_runtime_checkout",
    ):
        reset.run(execute=True, acknowledge_runtime_checkout=True)


def test_reset_plan_does_not_archive_tracked_directory_marker(monkeypatch, tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / ".gitkeep").write_text("", encoding="utf-8")
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "RUNTIME_DIRS", (("results", results),))

    plan = reset.build_plan("stamp")

    assert plan["runtime"] == []
