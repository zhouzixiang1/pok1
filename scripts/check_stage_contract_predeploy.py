#!/usr/bin/env python3
"""Pre-deploy STAGE-CONTRACT gate (operator pause-deploy norm, 2026-10-24).

The rating-identity predeploy check covers only
``evaluation_data_identity.SEMANTIC_PATHS`` (18 rating-identity files). The
2026-10-24 00:38 incident showed the other half: a deploy that changes a path
in the ACTIVE CHECKPOINT's per-stage contract (``evaluation_contract
._STAGE_EXACT``) while the checkpoint sits at a
``requires_contract_unchanged`` stage wedges the runtime behind the
``repo_baseline_head_mismatch`` launch barrier — 6h35m frozen (00:39-07:15),
~206M tokens of saturator burn with zero pipeline value, an in-flight review
stream killed. That deploy passed the rating-identity preflight while breaking
the stage contract.

Run AFTER the runtime pull and BEFORE ``systemctl start``:

    python scripts/check_stage_contract_predeploy.py \
        --runtime-dir /home/ubuntu/pok1/.evolution_pok

The check imports the RUNTIME tree's ``pipeline_recovery`` (the exact
production decision the launch barrier uses), so its verdict matches what the
service will do on start.

Exit codes:
  0  safe to start (no active checkpoint, or recovery has no issues);
  1  the deployed HEAD breaks the active checkpoint's resume — do NOT start;
     revert the deploy or advance the checkpoint (rework transition refreshes
     the baseline) first;
  2  evaluation unavailable (missing checkpoint code / git failure) — treat
     as blocked until investigated.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_runtime_env(runtime: Path) -> list[str]:
    """Apply the runtime service env (KEY=VALUE lines) before any import.

    Load-bearing, not cosmetic: ``bot_name``/``bot_tag``/``EVALUATION_EPOCH``
    resolve the ACTIVE namespace from env (``POK_CLOUD_RUNTIME`` et al.). Run
    outside the service environment the identity validators compare against
    the wrong namespace and every checkpoint reports
    ``checkpoint_published_parent_identity_*_mismatch`` — the same class of
    failure the rating daemon's ``_assert_bot_namespace_matches_env`` guard
    exists for. Returns the list of applied keys.
    """
    env_file = runtime / "deploy" / "tencent-cloud" / "env.runtime"
    applied: list[str] = []
    if not env_file.is_file():
        return applied
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or "$" in value:
            # Shell-expansion values can't be honoured statically; leave them
            # to the caller's environment.
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--runtime-dir",
        default="/home/ubuntu/pok1/.evolution_pok",
        help="The autonomous runtime checkout to evaluate (default: the "
        "cloud dual-checkout runtime path).",
    )
    args = parser.parse_args()
    runtime = Path(args.runtime_dir).resolve()
    core = runtime / "web" / "core"
    if not core.is_dir():
        print(f"[stage-contract] FAIL: runtime core not found: {core}", file=sys.stderr)
        return 2
    applied = _load_runtime_env(runtime)
    sys.path.insert(0, str(core))
    try:
        from evolution_infra import read_pipeline_checkpoint
        from pipeline_recovery import checkpoint_recovery_diagnostics
    except Exception as exc:  # import failure = cannot decide = blocked
        print(f"[stage-contract] FAIL: runtime import failed: {exc}", file=sys.stderr)
        return 2

    try:
        checkpoint = read_pipeline_checkpoint()
    except Exception as exc:
        print(f"[stage-contract] FAIL: checkpoint unreadable: {exc}", file=sys.stderr)
        return 2

    if not isinstance(checkpoint, dict) or not checkpoint:
        print(
            "[stage-contract] OK: no active checkpoint — deploy window is free."
        )
        return 0

    try:
        diag = checkpoint_recovery_diagnostics(
            checkpoint, project_root=str(runtime)
        )
    except Exception as exc:
        print(f"[stage-contract] FAIL: diagnostics failed: {exc}", file=sys.stderr)
        return 2

    stage = diag.get("stage")
    next_v = diag.get("next_v")
    issues = list(diag.get("issues") or [])
    warnings = list(diag.get("warnings") or [])
    repo = diag.get("repo") or {}
    drift_paths = list(
        (repo.get("repo_baseline") or {}).get("baseline_head_contract_paths")
        or repo.get("baseline_head_contract_paths")
        or []
    )

    print(
        f"[stage-contract] checkpoint v{next_v} at stage={stage} "
        f"recoverable={diag.get('recoverable')}"
    )
    for warning in warnings[:8]:
        print(f"[stage-contract]   warning: {warning}")

    if "repo_baseline_head_mismatch" in issues:
        print(
            "[stage-contract] BLOCKED: the deployed HEAD changes this "
            "stage's contract paths, and this stage requires the contract "
            "unchanged — starting now wedges the runtime behind the "
            "repo_baseline_head_mismatch launch barrier."
        )
        for path in drift_paths[:20] or ["(drift paths unavailable — see diag)"]:
            print(f"[stage-contract]   contract path: {path}")
        print(
            "[stage-contract] remediation: revert the deploy and resume the "
            "generation until a rework transition refreshes the baseline, "
            "or wait for the checkpoint to clear; never hand-edit the "
            "checkpoint."
        )
        return 1

    if issues:
        # Other recovery blockers (branch mismatch, worktree entries, …):
        # report and block — the launch barrier would deny the start anyway.
        for issue in issues[:10]:
            print(f"[stage-contract]   issue: {issue}")
        print(
            "[stage-contract] BLOCKED: recovery issues above would deny the "
            "orchestrator start."
        )
        return 1

    print(
        "[stage-contract] OK: active checkpoint is resumable at the deployed "
        "HEAD — safe to start."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
