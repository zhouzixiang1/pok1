import asyncio
import json
from pathlib import Path

import pytest

import national_native
from bot_artifact import hash_path
from bot_namespace import (
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_EPOCH_RECEIPT,
    ROLE_CANDIDATE,
    build_policy_epoch_receipt,
    build_runtime_manifest,
    resolve_national_bot_spec,
)


POLICY = """\
def get_baseline_decision(context):
    return {"kind": "pass"}


def iter_decisions(context, baseline, deadline):
    if False:
        yield baseline
"""


def _strict_bot(repo: Path, version: int, *, parents=()) -> Path:
    bot = repo / "bots" / f"national_v{version}"
    bot.mkdir(parents=True)
    national_native.ensure_native_entry(bot)
    (bot / "policy.py").write_text(POLICY, encoding="utf-8")
    manifest = build_runtime_manifest(bot)
    (bot / NATIONAL_RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    receipt = build_policy_epoch_receipt(
        bot,
        version,
        parent_versions=list(parents),
    )
    (bot / POLICY_EPOCH_RECEIPT).write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return bot


def test_resolve_bot_accepts_only_active_strict_policy_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot = _strict_bot(tmp_path, 143)

    assert national_native.resolve_bot("national_v143") == (
        "national_v143",
        bot.absolute(),
    )
    assert national_native.resolve_bot(bot / "national_bot.py") == (
        "national_v143",
        bot.absolute(),
    )

    for token in ("143", "v143", "bot143", "claude_v143"):
        with pytest.raises(ValueError):
            national_native.resolve_bot(token)
    archived = tmp_path / "archive" / "national_v142"
    with pytest.raises(ValueError, match="outside the active strict namespace"):
        national_native.resolve_bot(archived)


def test_native_spec_binds_original_artifact_without_copy_or_overlay(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot = _strict_bot(tmp_path, 143)
    before = hash_path(bot)

    spec = national_native._prepare_native_spec("national_v143", bot)

    assert spec.path == bot.absolute()
    assert spec.entry == bot.absolute() / "national_bot.py"
    assert spec.artifact_hash == before == hash_path(bot)
    identity = spec.execution_identity()
    assert identity["mode"] == "direct_content_bound_policy_artifact"
    assert identity["artifact_hash"] == before
    assert not hasattr(spec, "temp_root")


def test_candidate_abi_rejects_every_sixth_artifact_file(tmp_path):
    bot = _strict_bot(tmp_path, 143)
    assert resolve_national_bot_spec(
        bot,
        ROLE_CANDIDATE,
        repo_root=tmp_path,
    ).eligible

    (bot / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    spec = resolve_national_bot_spec(
        bot,
        ROLE_CANDIDATE,
        repo_root=tmp_path,
    )
    assert not spec.eligible
    assert "artifact_extra_file_forbidden:helper.py" in spec.issues


def test_strength_runner_passes_exact_content_bound_artifacts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(national_native, "ROOT", tmp_path)
    bot_a = _strict_bot(tmp_path, 143)
    bot_b = _strict_bot(tmp_path, 144, parents=(143,))
    captured = {}

    class Lease:
        def release(self):
            captured["released"] = True

    async def acquire(*_args, **_kwargs):
        return Lease()

    async def execute(spec_a, spec_b, **_kwargs):
        captured["paths"] = (spec_a.path, spec_b.path)
        return {
            "artifact_execution": {
                "schema_version": 1,
                "mode": "direct_content_bound_policy_artifact",
                "by_player": {
                    spec_a.label: spec_a.execution_identity(),
                    spec_b.label: spec_b.execution_identity(),
                },
            }
        }

    monkeypatch.setattr(national_native, "acquire_match_slots_async", acquire)
    monkeypatch.setattr(national_native, "_run_tcp_server_with_processes", execute)

    result = asyncio.run(
        national_native.run_native_strength_pair(bot_a, bot_b, 70)
    )

    assert captured["paths"] == (bot_a.absolute(), bot_b.absolute())
    assert captured["released"] is True
    assert national_native._artifact_execution_is_valid(
        result["artifact_execution"],
        {
            "national_v143": hash_path(bot_a),
            "national_v144": hash_path(bot_b),
        },
    )
