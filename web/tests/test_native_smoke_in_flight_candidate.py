"""Regression guard for the crossover smoke namespace-rejection deadlock.

The crossover smoke test (``agent_review.run_crossover``) stages the in-flight
candidate under ``RESULTS_DIR/crossover_workspaces/vN-attempt-K-<tmp>``, but
``resolve_bot`` requires the candidate's parent to equal ``<ROOT>/bots``. Every
crossover smoke attempt therefore failed with ``bot path is outside the active
strict namespace`` before any match logic ran, exhausting the 3 crossover
retries and abandoning the generation with ``crossover_llm_exhausted``.

``run_native_tcp_smoke`` now accepts an opt-in ``in_flight_candidate_dir``:
it validates the five strict ABI files structurally and runs the candidate
from its transient directory, bypassing the strict-namespace ``resolve_bot``
(which is reserved for published strict artifacts). The opponent is still
resolved through ``resolve_bot`` so a transient candidate can never pose as a
published bot.
"""

from pathlib import Path

import national_native_acceptance
import national_native as national_native_mod
from bot_namespace import STRICT_ARTIFACT_FILES


def test_in_flight_candidate_missing_abi_files_fails_structurally(tmp_path):
    """An in-flight candidate dir without the five strict ABI files is rejected
    with a structural candidate_failure — never a namespace error."""
    workspace = tmp_path / "crossover_workspaces" / "v23-attempt-1-abc"
    workspace.mkdir(parents=True)

    import asyncio

    report = asyncio.run(national_native_acceptance.run_native_tcp_smoke(
        workspace,
        source_v=11,
        opponent_token=None,
        hands=1,
        in_flight_candidate_dir=workspace,
    ))

    assert report["passed"] is False
    assert report["outcome"] == "candidate_failure"
    assert report["failure_side"] == "candidate"
    issues = " ".join(report["issues"])
    assert "missing_strict_artifacts" in issues
    # The strict-namespace rejection must NOT surface for an in-flight candidate.
    assert "outside the active strict namespace" not in issues


def test_in_flight_candidate_does_not_resolve_bot_for_candidate(monkeypatch, tmp_path):
    """With a complete ABI dir, the in-flight path never calls resolve_bot for
    the candidate (it runs structurally). The opponent is still resolved."""

    workspace = tmp_path / "crossover_workspaces" / "v23-attempt-1-abc"
    workspace.mkdir(parents=True)
    # Provide all five strict ABI files so structural validation passes.
    for name in STRICT_ARTIFACT_FILES:
        (workspace / name).write_text("# strict abi placeholder\n", encoding="utf-8")

    resolve_calls: list[str] = []

    def fake_resolve_bot(token):
        resolve_calls.append(str(token))
        # Only the opponent should be resolved; raise if the candidate reaches it.
        if str(token) == str(workspace):
            raise AssertionError("in-flight candidate must not go through resolve_bot")
        return ("national_cloud_v11", tmp_path / "bots" / "national_cloud_v11")

    monkeypatch.setattr(national_native_mod, "resolve_bot", fake_resolve_bot)

    async def fake_pair(candidate_dir, opponent_dir, hands, **_kwargs):
        # Prove the in-flight candidate dir is run directly, not via bots/.
        return {"passed": True, "candidate_dir": str(candidate_dir)}

    monkeypatch.setattr(national_native_mod, "run_native_tcp_pair", fake_pair)

    import asyncio

    report = asyncio.run(national_native_acceptance.run_native_tcp_smoke(
        workspace,
        source_v=11,
        opponent_token=tmp_path / "bots" / "national_cloud_v11",
        hands=1,
        in_flight_candidate_dir=workspace,
    ))

    # The candidate was NOT resolved through resolve_bot; only the opponent was.
    assert str(workspace) not in resolve_calls


def test_native_wrapper_forwards_in_flight_candidate_dir(monkeypatch, tmp_path):
    """The ``national_native.run_native_tcp_smoke`` wrapper (used by all three
    production call sites: agent_review, tool_gates, tool_gates_native_smoke)
    must accept and forward ``in_flight_candidate_dir`` to the real
    ``national_native_acceptance.run_native_tcp_smoke``.

    Regression anchor: commit 57a76b23 added the param to the real function but
    NOT to the delegating wrapper in ``national_native.py``, so every crossover
    smoke crashed with ``unexpected keyword argument 'in_flight_candidate_dir'``
    (v26 crash 2026-07-31).
    """
    forwarded: dict[str, object] = {}

    async def fake_smoke(candidate_token, **kwargs):
        forwarded["candidate_token"] = candidate_token
        forwarded["kwargs"] = kwargs
        return {"passed": True}

    # The wrapper calls the real function via the ``_nn`` module alias.
    monkeypatch.setattr(national_native_mod._nn, "run_native_tcp_smoke", fake_smoke)

    import asyncio

    workspace = tmp_path / "crossover_workspaces" / "v26-attempt-1-xyz"
    report = asyncio.run(national_native_mod.run_native_tcp_smoke(
        workspace,
        source_v=11,
        opponent_token=None,
        hands=1,
        in_flight_candidate_dir=workspace,
    ))
    assert report["passed"] is True
    # The wrapper forwarded the kwarg (the bug was that it did not).
    assert forwarded["kwargs"].get("in_flight_candidate_dir") == workspace


def test_in_flight_opponent_bypasses_resolve_bot(monkeypatch, tmp_path):
    """A frozen-parent opponent snapshot (artifact-store path) must bypass
    resolve_bot, mirroring the in-flight candidate path.

    Regression: the crossover smoke passed opponent_token=frozen_parent_a_dir
    (a RESULTS_DIR/workflow/artifacts/ path), but resolve_bot rejected it as
    "outside the active strict namespace" -> every crossover smoke failed
    deterministically (48 failures observed).  in_flight_opponent_dir lets the
    opponent run structurally, just like the candidate.
    """
    candidate_dir = tmp_path / "crossover_workspaces" / "v50-attempt-1"
    opponent_dir = tmp_path / "workflow" / "artifacts" / "frozen_parent_a"
    for d in (candidate_dir, opponent_dir):
        d.mkdir(parents=True)
        for name in STRICT_ARTIFACT_FILES:
            (d / name).write_text("# strict abi placeholder\n", encoding="utf-8")

    resolve_calls: list[str] = []

    def fake_resolve_bot(token):
        resolve_calls.append(str(token))
        raise AssertionError(
            f"resolve_bot must not be called for in-flight opponent: {token}"
        )

    monkeypatch.setattr(national_native_mod, "resolve_bot", fake_resolve_bot)

    async def fake_pair(candidate_dir, opponent_dir, hands, **_kwargs):
        return {"passed": True}

    monkeypatch.setattr(national_native_mod, "run_native_tcp_pair", fake_pair)

    import asyncio

    report = asyncio.run(national_native_acceptance.run_native_tcp_smoke(
        candidate_dir,
        source_v=27,
        hands=1,
        in_flight_candidate_dir=candidate_dir,
        in_flight_opponent_dir=opponent_dir,
    ))

    # Neither candidate nor opponent went through resolve_bot.
    assert resolve_calls == []
    assert report["passed"] is True


def test_in_flight_opponent_missing_artifacts_is_infra_failure(tmp_path):
    """An in-flight opponent dir missing strict ABI files fails closed."""
    candidate_dir = tmp_path / "candidate"
    opponent_dir = tmp_path / "opponent"
    candidate_dir.mkdir(parents=True)
    opponent_dir.mkdir(parents=True)
    for name in STRICT_ARTIFACT_FILES:
        (candidate_dir / name).write_text("#\n", encoding="utf-8")
    # opponent dir is EMPTY (missing ABI files)

    import asyncio

    report = asyncio.run(national_native_acceptance.run_native_tcp_smoke(
        candidate_dir,
        source_v=27,
        opponent_token=opponent_dir,
        hands=1,
        in_flight_candidate_dir=candidate_dir,
        in_flight_opponent_dir=opponent_dir,
    ))

    assert report["passed"] is False
    assert report["outcome"] == "infrastructure_failure"
    assert report["failure_side"] == "opponent"
    assert "missing_strict_artifacts" in " ".join(report["issues"])
