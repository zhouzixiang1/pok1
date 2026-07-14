from pathlib import Path


def test_strict_import_surface_is_policy_and_system_precompute(tmp_path):
    from code_verification import _production_import_modules

    (tmp_path / "national_bot.py").write_text("raise RuntimeError('must not import')\n")
    (tmp_path / "precompute.py").write_text("FACT = 1\n")
    (tmp_path / "policy.py").write_text(
        "def get_baseline_decision(context):\n    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n    return iter(())\n"
    )
    assert _production_import_modules(tmp_path) == ["precompute", "policy"]


def test_new_policy_helper_must_reach_typed_dispatch(tmp_path):
    from code_verification import detect_new_function_reachability_warnings

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    base = (
        "def get_baseline_decision(context):\n    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n    return iter(())\n"
    )
    (source / "policy.py").write_text(base)
    (child / "policy.py").write_text(base + "\ndef unused_helper():\n    return 1\n")
    warnings = detect_new_function_reachability_warnings(
        source,
        child,
        ["policy.py"],
    )
    assert len(warnings) == 1
    assert "unused_helper" in warnings[0]

    (child / "policy.py").write_text(
        "def used_helper():\n    return {'kind': 'pass'}\n\n"
        "def get_baseline_decision(context):\n    return used_helper()\n\n"
        "def iter_decisions(context, baseline, deadline):\n    return iter(())\n"
    )
    assert detect_new_function_reachability_warnings(
        source,
        child,
        ["policy.py"],
    ) == []


def test_materialized_system_control_passes_strict_compile_and_import():
    from code_verification import run_import_contract_test, verify_code
    from first_strict_control import materialize_control

    path = Path(materialize_control())
    assert verify_code(path) == []
    assert run_import_contract_test(path) == []
