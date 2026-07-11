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
    monkeypatch.setattr(module, "check_environment", lambda: {"ok": True, "issues": []})
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


def test_cli_returns_nonzero_for_terminal_job_infrastructure_failure(monkeypatch):
    module = _module()
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
