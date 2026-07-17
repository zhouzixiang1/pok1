import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "official_certify_cli_test",
        ROOT / "scripts" / "official_certify.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cli_has_no_legacy_default_opponent():
    module = _module()

    args = module.parse_args(["full", "bots/national_v143"])

    assert args.opponent is None


def test_cli_exposes_only_durable_job_commands():
    module = _module()

    assert module.parse_args(["jobs-status"]).cmd == "jobs-status"
    reconcile = module.parse_args(["reconcile-jobs", "--limit", "4"])
    assert reconcile.cmd == "reconcile-jobs"
    assert reconcile.limit == 4
    with pytest.raises(SystemExit):
        module.parse_args(["queue-status"])
    with pytest.raises(SystemExit):
        module.parse_args(["process-queue"])


def test_cli_help_describes_start_or_poll_durable_jobs(capsys):
    module = _module()

    with pytest.raises(SystemExit) as exc_info:
        module.parse_args(["--help"])
    assert exc_info.value.code == 0
    output = " ".join(capsys.readouterr().out.split()).replace("- ", "-")
    assert "Start or poll a durable short official quality-gate smoke job" in output
    assert "Start or poll a durable short official protocol-compliance job" in output
    assert "Start or poll a durable manual 5+3, 70-hand" in output
    assert "jobs-status" in output
    assert "reconcile-jobs" in output
    assert "queue-status" not in output
    assert "process-queue" not in output


def test_cli_fails_without_eligible_opponent(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {"valid": True, "issues": [], "entry_count": 0, "head": None},
    )
    monkeypatch.setattr(module, "select_official_opponent", lambda *_a, **_k: {
        "selected": False,
        "reason": "no_official_eligible_opponent",
        "considered": [],
    })
    monkeypatch.setattr(
        module,
        "build_formal_quality_admission",
        lambda *_a, **_k: {
            "valid": True,
            "issues": [],
            "admission": {"admission_digest": "a" * 64},
        },
    )

    exit_code = module.main(["full", "bots/national_v143"])

    assert exit_code == 2
    assert "opponent-selection-blocked" in capsys.readouterr().out


def test_cli_doctor_requires_platform_and_signer(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(
        module,
        "check_environment",
        lambda **kwargs: {
            "ok": kwargs.get("require_formal_sandbox") is True,
            "issues": [],
        },
    )
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {"valid": True, "issues": [], "entry_count": 0, "head": None},
    )
    monkeypatch.setattr(
        module,
        "signing_environment_report",
        lambda: {"ok": False, "issues": ["signer missing"]},
    )

    exit_code = module.main(["doctor"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert '"ok": false' in output
    assert "signer missing" in output


def test_cli_doctor_reports_missing_ledger_without_initializing(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(
        module,
        "check_environment",
        lambda **kwargs: {
            "ok": kwargs.get("require_formal_sandbox") is True,
            "issues": [],
        },
    )
    monkeypatch.setattr(
        module,
        "signing_environment_report",
        lambda: {"ok": True, "issues": []},
    )
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {
            "valid": False,
            "issues": ["official_verdict_ledger_missing"],
            "entry_count": 0,
            "head": None,
        },
    )
    monkeypatch.setattr(
        module,
        "initialize_verdict_ledger",
        lambda: (_ for _ in ()).throw(AssertionError("doctor must not initialize authority")),
    )

    exit_code = module.main(["doctor"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "official_verdict_ledger_missing" in output
    assert "python3 scripts/official_certify.py init-ledger" in output


def test_cli_init_ledger_is_explicit_and_idempotent(monkeypatch, capsys):
    module = _module()
    calls = []
    monkeypatch.setattr(
        module,
        "signing_environment_report",
        lambda: {"ok": True, "issues": []},
    )
    monkeypatch.setattr(
        module,
        "initialize_verdict_ledger",
        lambda: calls.append("initialize") or {
            "initialized": False,
            "valid": True,
            "entry_count": 3,
        },
    )
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {"valid": True, "issues": [], "entry_count": 3, "head": {}},
    )

    exit_code = module.main(["init-ledger"])

    assert exit_code == 0
    assert calls == ["initialize"]
    assert '"initialized": false' in capsys.readouterr().out


def test_cli_init_ledger_requires_signer_before_creating_genesis(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(
        module,
        "signing_environment_report",
        lambda: {"ok": False, "issues": ["signer missing"]},
    )
    monkeypatch.setattr(
        module,
        "initialize_verdict_ledger",
        lambda: (_ for _ in ()).throw(AssertionError("must not create unsigned genesis")),
    )
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {
            "valid": False,
            "issues": ["official_verdict_ledger_missing"],
            "entry_count": 0,
            "head": None,
        },
    )

    exit_code = module.main(["init-ledger"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "signer missing" in output
    assert '"initialized": false' in output


def test_cli_full_blocks_before_selection_when_ledger_is_unavailable(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {
            "valid": False,
            "issues": ["official_verdict_ledger_missing"],
            "entry_count": 0,
            "head": None,
        },
    )
    monkeypatch.setattr(
        module,
        "select_official_opponent",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("selection/job work must not start")
        ),
    )

    exit_code = module.main(["full", "bots/national_v143"])

    assert exit_code == 2
    output = capsys.readouterr().out
    assert "formal-preflight-blocked" in output
    assert "official_verdict_ledger_missing" in output


def test_cli_returns_nonzero_for_terminal_job_infrastructure_failure(monkeypatch):
    module = _module()
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {"valid": True, "issues": [], "entry_count": 0, "head": None},
    )
    monkeypatch.setattr(module, "select_official_opponent", lambda *_a, **_k: {
        "selected": True,
        "candidate": "bots/national_v143",
        "opponent": {
            "path": "bots/national_v142",
            "bot": "national_v142",
            "eligible": True,
            "reason": "official_certified",
        },
    })
    monkeypatch.setattr(module, "build_spec", lambda *_a, **_k: object())
    monkeypatch.setattr(
        module,
        "build_formal_quality_admission",
        lambda *_a, **_k: {
            "valid": True,
            "issues": [],
            "admission": {"admission_digest": "a" * 64},
        },
    )
    monkeypatch.setattr(module, "start_or_poll_job", lambda *_a, **_k: {
        "state": "failed",
        "pending": False,
        "failure_class": "infrastructure",
    })

    assert module.main(["full", "bots/national_v143"]) == 2


def test_cli_full_blocks_before_selection_when_dynamic_quality_admission_is_invalid(
    monkeypatch, capsys
):
    module = _module()
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {"valid": True, "issues": [], "entry_count": 1, "head": {}},
    )
    monkeypatch.setattr(
        module,
        "build_formal_quality_admission",
        lambda *_a, **_k: {
            "valid": False,
            "issues": ["official_formal_quality_gate_ledger_missing"],
            "admission": None,
        },
    )
    monkeypatch.setattr(
        module,
        "select_official_opponent",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("selection must not start without a current quality admission")
        ),
    )

    exit_code = module.main(["full", "bots/national_v144"])

    assert exit_code == 2
    output = capsys.readouterr().out
    assert "formal-quality-admission-blocked" in output
    assert "official_formal_quality_gate_ledger_missing" in output


def test_cli_full_binds_quality_admission_into_durable_spec(monkeypatch):
    module = _module()
    admission = {"admission_digest": "b" * 64, "candidate_hash": "c" * 64}
    seen = {}
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {"valid": True, "issues": [], "entry_count": 1, "head": {}},
    )
    monkeypatch.setattr(
        module,
        "build_formal_quality_admission",
        lambda *_a, **_k: {"valid": True, "issues": [], "admission": admission},
    )
    monkeypatch.setattr(
        module,
        "select_official_opponent",
        lambda *_a, **_k: {
            "selected": True,
            "candidate": "bots/national_v144",
            "opponent": {"path": "bots/national_v143", "eligible": True},
        },
    )
    monkeypatch.setattr(
        module,
        "build_spec",
        lambda mode, candidate, **kwargs: seen.update(
            {"mode": mode, "candidate": candidate, **kwargs}
        ) or object(),
    )
    monkeypatch.setattr(
        module,
        "start_or_poll_job",
        lambda *_a, **_k: {"state": "failed", "pending": False},
    )

    assert module.main(["full", "bots/national_v144"]) == 2
    assert seen["mode"] == "full"
    assert seen["quality_admission"] == admission


def test_cli_first_strict_requires_explicit_one_time_acknowledgement(monkeypatch, capsys):
    module = _module()
    monkeypatch.setattr(
        module,
        "select_first_strict_control",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("control must not be selected before acknowledgement")
        ),
    )

    exit_code = module.main([
        "bootstrap-first-strict",
        "bots/national_v143",
    ])

    assert exit_code == 2
    assert "bootstrap-acknowledgement-required" in capsys.readouterr().out


def test_cli_first_strict_binds_current_control_to_full_spec(monkeypatch):
    module = _module()
    control_id = "first_strict_control_v1"
    selection = {
        "selected": True,
        "bootstrap_control_id": control_id,
        "candidate": "bots/national_v143",
        "opponent": {"path": "controls/first_strict_control_v1", "eligible": True},
    }
    seen = {}
    monkeypatch.setattr(
        module,
        "ledger_integrity",
        lambda: {"valid": True, "issues": [], "entry_count": 1, "head": {}},
    )
    monkeypatch.setattr(
        module,
        "select_first_strict_control",
        lambda control, candidate: seen.update({"control": control, "candidate": candidate}) or selection,
    )
    monkeypatch.setattr(
        module,
        "select_official_opponent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("normal selector used")),
    )
    monkeypatch.setattr(
        module,
        "authorize_operator_bootstrap_selection",
        lambda selected, *_args, **_kwargs: {
            "valid": True,
            "selection": {
                **selected,
                "operator_bootstrap_authorization": {
                    "authorization_digest": "a" * 64,
                },
            },
        },
    )
    monkeypatch.setattr(
        module,
        "build_spec",
        lambda mode, candidate, **kwargs: seen.update({"mode": mode, "spec_candidate": candidate, **kwargs}) or object(),
    )
    monkeypatch.setattr(
        module,
        "start_or_poll_job",
        lambda *_args, **_kwargs: {"state": "failed", "pending": False},
    )

    exit_code = module.main([
        "bootstrap-first-strict",
        "bots/national_v143",
        "--control-id",
        control_id,
        "--acknowledge-one-time-first-strict-control",
    ])

    assert exit_code == 2
    assert seen["control"] == control_id
    assert seen["mode"] == "full"
    assert seen["bootstrap_control_id"] == control_id
