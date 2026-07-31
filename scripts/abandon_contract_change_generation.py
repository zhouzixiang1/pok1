#!/usr/bin/env python3
"""Canonically abandon a generation stranded at a publication-family stage by a
contract-critical deploy.

Recovery gap this closes: a generation that reached ``verified`` /
``publishing`` / ``official_certifying`` cannot resume after a deploy that
changed an evaluation-contract path (``repo_baseline_head_mismatch`` with
``requires_contract_unchanged=True``) and cannot be generically abandoned
(those stages are ``never_disposable``). Before this script there was no
operator path off that deadlock for a non-bootstrap generation.

The authority it supplies (``_operator_contract_change_proof``) is strictly
opt-in: without it the default ``never_disposable`` guard is untouched. The
proof is rebuilt from the live checkpoint + Git state on every lock boundary
inside ``_do_abandon_generation``, so the digest an operator reviews in the
dry run must match the live proof at execute time. This script never edits the
checkpoint, the abandon ledger, or any CAS state file; it only invokes the
canonical publication-linearized abandon transaction (quarantine + exact-CAS
clear + terminal receipt).

Usage (run the dry run first, review the proof, then execute with its digest)::

    python scripts/abandon_contract_change_generation.py \\
        --expected-workflow-run-id generation:25:workflow-v1 \\
        --expected-next-v 25 --expected-source-v 1 \\
        --expected-checkpoint-revision 17 --expected-checkpoint-stage verified

    # review the printed proof + claim_digest, then:

    python scripts/abandon_contract_change_generation.py --execute \\
        --acknowledge-runtime-checkout --claim-digest <reviewed-digest> \\
        --expected-workflow-run-id generation:25:workflow-v1 \\
        --expected-next-v 25 --expected-source-v 1 \\
        --expected-checkpoint-revision 17 --expected-checkpoint-stage verified
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "core"
for entry in list(sys.path):
    try:
        resolved = Path(entry or ".").resolve()
    except OSError:
        continue
    if resolved in {CORE, ROOT}:
        sys.path.remove(entry)
sys.path.insert(0, str(CORE))
sys.path.insert(1, str(ROOT))

from bot_artifact import canonical_digest  # noqa: E402
from evaluation_contract import evaluate_head_drift  # noqa: E402
from evolution_infra import _git as _evolution_git, read_pipeline_checkpoint  # noqa: E402
from official_certification_job import _index_lock  # noqa: E402
from scripts.reconcile_national_policy_epoch import (  # noqa: E402
    _reconciliation_lock,
    _runtime_checkout_identity_errors,
    _runtime_process_errors,
)
from tool_bot_management import (  # noqa: E402
    _checkpoint_transaction_identity,
    _contract_change_abandon_reason,
    _CONTRACT_CHANGE_ABANDONABLE_STAGES,
    _do_abandon_generation,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-runtime-checkout", action="store_true")
    parser.add_argument("--claim-digest")
    parser.add_argument("--expected-workflow-run-id", required=True)
    parser.add_argument("--expected-next-v", required=True, type=int)
    parser.add_argument("--expected-source-v", required=True, type=int)
    parser.add_argument("--expected-checkpoint-revision", required=True, type=int)
    parser.add_argument(
        "--expected-checkpoint-stage",
        required=True,
        choices=sorted(_CONTRACT_CHANGE_ABANDONABLE_STAGES),
    )
    return parser


def _build_proof(checkpoint: dict) -> dict:
    """Rebuild the contract-change abandon proof from the live checkpoint + Git.

    Mirrors ``_contract_change_abandon_authority``'s re-derivation so the dry-run
    proof is exactly what the authority will recompute on every lock boundary.
    """
    baseline = checkpoint.get("repo_baseline")
    if not isinstance(baseline, dict) or not baseline.get("head"):
        raise RuntimeError(
            "checkpoint has no repo_baseline.head; not a head-drift deadlock"
        )
    baseline_head = str(baseline["head"])
    current_head = _evolution_git("rev-parse", "HEAD").strip()
    if not current_head:
        raise RuntimeError("could not resolve current HEAD")
    contract_unchanged, drift = evaluate_head_drift(
        ROOT,
        baseline_head,
        current_head,
        candidate_v=checkpoint.get("next_v"),
        source_v=checkpoint.get("source_v"),
        checkpoint=checkpoint,
        stage=checkpoint.get("stage"),
    )
    if contract_unchanged:
        raise RuntimeError(
            "evaluation contract is unchanged between baseline and current HEAD; "
            "this is not a contract-change deadlock — resume normally instead"
        )
    contract_paths = sorted(drift.get("head_contract_paths") or [])
    if not contract_paths:
        raise RuntimeError(
            "head drift changed no evaluation-contract paths; abandon is not warranted"
        )
    identity = _checkpoint_transaction_identity(checkpoint)
    proof = {
        "schema_version": 1,
        "kind": "national-contract-change-abandon-proof",
        "evaluation_epoch": checkpoint.get("evaluation_epoch"),
        "baseline_head": baseline_head,
        "current_head": current_head,
        "changed_contract_paths": contract_paths,
        "checkpoint": identity,
        "stage": checkpoint.get("stage"),
    }
    proof["claim_digest"] = canonical_digest(proof)
    return proof


def _identity_matches(checkpoint: dict, args: argparse.Namespace) -> None:
    """Fail fast if the live checkpoint is not the exact generation targeted."""
    expected = {
        "workflow_run_id": args.expected_workflow_run_id,
        "next_v": args.expected_next_v,
        "source_v": args.expected_source_v,
        "checkpoint_revision": args.expected_checkpoint_revision,
        "stage": args.expected_checkpoint_stage,
    }
    actual = {
        "workflow_run_id": str(
            checkpoint.get("workflow_run_id")
            or checkpoint.get("run_id")
            or ""
        ),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "checkpoint_revision": checkpoint.get("checkpoint_revision"),
        "stage": checkpoint.get("stage"),
    }
    if actual != expected:
        raise RuntimeError(
            "live checkpoint identity does not match the targeted generation: "
            f"expected={expected} actual={actual}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime_errors = [
        *_runtime_checkout_identity_errors(),
        *_runtime_process_errors(),
    ]
    if runtime_errors:
        raise RuntimeError("; ".join(runtime_errors))
    if args.execute and not args.acknowledge_runtime_checkout:
        raise RuntimeError("execution requires --acknowledge-runtime-checkout")
    if args.execute and not args.claim_digest:
        raise RuntimeError("execution requires the reviewed --claim-digest")

    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        raise RuntimeError("active pipeline checkpoint is missing or unreadable")
    _identity_matches(checkpoint, args)
    if checkpoint.get("stage") not in _CONTRACT_CHANGE_ABANDONABLE_STAGES:
        raise RuntimeError(
            f"stage {checkpoint.get('stage')!r} is not a publication-family stage "
            f"eligible for contract-change abandon; eligible: "
            f"{sorted(_CONTRACT_CHANGE_ABANDONABLE_STAGES)}"
        )

    proof = _build_proof(checkpoint)
    reason = _contract_change_abandon_reason(proof)

    if not args.execute:
        print(
            json.dumps(
                {
                    **proof,
                    "reason": reason,
                    "mode": "dry_run",
                    "mutates": False,
                    "directive": (
                        "Review changed_contract_paths and claim_digest, then re-run "
                        "with --execute --acknowledge-runtime-checkout "
                        "--claim-digest <digest> and the same --expected-* identity."
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.claim_digest != proof.get("claim_digest"):
        raise RuntimeError(
            "reviewed --claim-digest does not match the live proof; re-run the dry "
            "run and review the current proof (the deploy or checkpoint may have moved)"
        )

    # Preserve canonical lock order: the abandon owner acquires its workflow
    # actor lock before the publication mutex.  Holding publication here would
    # invert that order against ordinary terminal projections.
    with _reconciliation_lock(), _index_lock():
        locked_checkpoint = read_pipeline_checkpoint()
        if not isinstance(locked_checkpoint, dict):
            raise RuntimeError("checkpoint disappeared before migration lock")
        _identity_matches(locked_checkpoint, args)
        locked_proof = _build_proof(locked_checkpoint)
        if locked_proof != proof:
            raise RuntimeError(
                "contract-change proof changed under lock; re-run the dry run"
            )
        result = asyncio.run(
            _do_abandon_generation(
                reason=reason,
                _bypass_rate_limit=True,
                expected_workflow_run_id=args.expected_workflow_run_id,
                expected_next_v=args.expected_next_v,
                expected_source_v=args.expected_source_v,
                expected_checkpoint_revision=args.expected_checkpoint_revision,
                expected_checkpoint_stage=args.expected_checkpoint_stage,
                _operator_contract_change_proof=locked_proof,
            )
        )
        if result.get("abandoned") is not True:
            raise RuntimeError(
                "canonical contract-change abandon failed: "
                + json.dumps(result, ensure_ascii=False, sort_keys=True)[:1200]
            )

    print(
        json.dumps(
            {
                "status": "abandoned",
                "claim_digest": args.claim_digest,
                "abandon": result,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
