import json

import pytest

from scripts import reset_national_tcp_policy_epoch as reset
from evaluation_contract import ALWAYS_CRITICAL_EXACT


def _git_at_v142(*args):
    if args and args[0] == "for-each-ref":
        return (
            "tag\tcommit\tnational-bot-v141\n"
            "tag\tcommit\tnational-bot-v142\n"
            "tag\tcommit\tnational-high-water-v142"
        )
    if args[:3] == ("tag", "-l", "national-bot-v*"):
        return "national-bot-v141\nnational-bot-v142"
    if args[:3] == ("tag", "-l", "national-high-water-v*"):
        return "national-high-water-v142"
    if args in {
        ("rev-parse", "refs/tags/national-bot-v142^{commit}"),
        ("rev-parse", "refs/tags/national-high-water-v142^{commit}"),
    }:
        return "b" * 40
    if args[:2] == ("rev-parse", "HEAD"):
        return "a" * 40
    return ""


def test_reset_script_is_always_evaluation_contract_critical():
    assert "scripts/reset_national_tcp_policy_epoch.py" in ALWAYS_CRITICAL_EXACT


def test_policy_epoch_reset_has_no_source_seed(monkeypatch, tmp_path):
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", tmp_path / "web" / "core")
    monkeypatch.setattr(reset, "_git", _git_at_v142)
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
    assert receipt["first_target_version"] == 143
    assert receipt["version_authority_high_water"] == 142
    assert receipt["source_code_inherited"] is False
    assert receipt["seed_bot"] is None
    assert receipt["schema_version"] == 2
    assert len(receipt["receipt_digest"]) == 64
    assert not (tmp_path / reset.RESET_ARCHIVE_RELATIVE).exists()


def test_policy_epoch_reset_refuses_rerun_after_strict_tag(monkeypatch):
    def git(*args):
        if args and args[0] == "for-each-ref":
            return (
                "tag\tcommit\tnational-bot-v142\n"
                "tag\tcommit\tnational-bot-v143\n"
                "tag\tcommit\tnational-high-water-v143"
            )
        if args[:3] == ("tag", "-l", "national-bot-v*"):
            return "national-bot-v142\nnational-bot-v143"
        if args[:3] == ("tag", "-l", "national-high-water-v*"):
            return "national-high-water-v143"
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
            "tag\tcommit\tnational-bot-v142\n"
            "tag\tcommit\tnational-high-water-v142\n"
            "commit\t\tnational-bot-v999"
        ) if args and args[0] == "for-each-ref" else (
            "a" * 40 if args and args[0] == "rev-parse" else ""
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

    assert receipt["version_authority_high_water"] == 142


def test_policy_epoch_reset_fails_without_annotated_version_authority(monkeypatch):
    monkeypatch.setattr(
        reset,
        "_git",
        lambda *args: (
            "commit\t\tnational-high-water-v142"
            if args and args[0] == "for-each-ref"
            else ""
        ),
    )

    with pytest.raises(RuntimeError, match="annotated completion/high-water"):
        reset.run(execute=False)


def test_reset_plan_archives_pre_policy_and_untagged_high_version_debris(
    monkeypatch, tmp_path
):
    bots = tmp_path / "bots"
    (bots / "national_v142").mkdir(parents=True)
    (bots / "national_v143").mkdir()
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "RUNTIME_DIRS", ())

    plan = reset.build_plan("stamp")

    assert [item["source"].name for item in plan["archived_bot_dirs"]] == [
        "national_v142",
        "national_v143",
    ]
    assert [item["disposition"] for item in plan["archived_bot_dirs"]] == [
        "retired_epoch_bot",
        "stale_unpublished_high_version_candidate",
    ]


def test_reset_receipt_marks_stale_v155_untrusted(monkeypatch, tmp_path):
    bots = tmp_path / "bots"
    (bots / "national_v155").mkdir(parents=True)
    monkeypatch.setattr(reset, "ROOT", tmp_path)
    monkeypatch.setattr(reset, "CORE", tmp_path / "web" / "core")
    monkeypatch.setattr(reset, "RUNTIME_DIRS", ())
    monkeypatch.setattr(reset, "_git", _git_at_v142)

    receipt = reset.run(execute=False)

    assert receipt["first_target_version"] == 143
    assert len(receipt["archived_bot_debris"]) == 1
    item = receipt["archived_bot_debris"][0]
    assert item["from"] == "bots/national_v155"
    assert item["to"].endswith("/bot_debris/national_v155")
    assert item["trust"] == "archived_non_executable"
    assert item["disposition"] == "stale_unpublished_high_version_candidate"


def test_execute_archives_stale_v155_and_checkpoint_before_fresh_v143(
    monkeypatch, tmp_path
):
    core = tmp_path / "web" / "core"
    results = core / "results"
    candidate = tmp_path / "bots" / "national_v155"
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
    monkeypatch.setattr(reset, "_git", _git_at_v142)
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
    assert not (tmp_path / "bots" / "national_v143").exists()
    reset_receipt = results / "policy_epoch_reset_receipt.json"
    assert reset_receipt.is_file()
    archived_v155 = tmp_path / receipt["archived_bot_debris"][0]["to"]
    archived_results = tmp_path / receipt["archived_runtime"][0]["to"]
    assert (archived_v155 / "main.py").is_file()
    assert (archived_results / "pipeline_state.json").is_file()
    assert receipt["first_target_version"] == 143


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
    monkeypatch.setattr(reset, "_git", _git_at_v142)
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
    monkeypatch.setattr(reset, "_git", _git_at_v142)
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
    monkeypatch.setattr(reset, "_git", _git_at_v142)
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
    monkeypatch.setattr(reset, "_git", _git_at_v142)
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
    monkeypatch.setattr(reset, "_git", _git_at_v142)

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
