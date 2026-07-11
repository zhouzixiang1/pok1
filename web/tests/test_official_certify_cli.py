import importlib.util
from pathlib import Path


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
    monkeypatch.setattr(module, "start_or_poll_job", lambda *_a, **_k: {
        "state": "failed",
        "pending": False,
        "failure_class": "infrastructure",
    })

    assert module.main(["full", "bots/national_v143"]) == 2
