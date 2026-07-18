#!/usr/bin/env python3
"""Canonically abandon an unpublished parked v143 after a reviewed contract fix.

Run without ``--execute`` first.  Review the complete claim and then repeat the
same command with ``--execute --acknowledge-runtime-checkout --claim-digest``.
This command is available only in a stopped, clean, origin/main-synchronized
``.evolution_pok`` checkout.  It never edits the checkpoint or deletes state;
it durably publishes an external proof and invokes the existing canonical
workflow fence/quarantine/abandon transaction.
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

from bootstrap_contract_recovery import (  # noqa: E402
    abandon_reason,
    build_claim,
    finalized_claim_result,
    incomplete_claim_resume_identity,
    publish_claim,
    validate_claim_for_checkpoint,
)
from evolution_infra import read_pipeline_checkpoint  # noqa: E402
from official_certification_job import _index_lock  # noqa: E402
from scripts.reconcile_national_policy_epoch import (  # noqa: E402
    _reconciliation_lock,
    _runtime_checkout_identity_errors,
    _runtime_process_errors,
)
from tool_bot_management import _do_abandon_generation  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-runtime-checkout", action="store_true")
    parser.add_argument("--claim-digest")
    parser.add_argument("--expected-baseline-head", required=True)
    parser.add_argument("--expected-baseline-contract-hash", required=True)
    parser.add_argument("--expected-current-head", required=True)
    parser.add_argument("--expected-workflow-run-id", required=True)
    parser.add_argument("--expected-checkpoint-revision", required=True, type=int)
    parser.add_argument("--expected-candidate-hash", required=True)
    parser.add_argument("--expected-terminal-job-id", required=True)
    return parser


def _claim(args: argparse.Namespace, checkpoint: dict) -> dict:
    return build_claim(
        ROOT,
        checkpoint=checkpoint,
        expected_baseline_head=args.expected_baseline_head,
        expected_baseline_contract_hash=args.expected_baseline_contract_hash,
        expected_current_head=args.expected_current_head,
        expected_workflow_run_id=args.expected_workflow_run_id,
        expected_checkpoint_revision=args.expected_checkpoint_revision,
        expected_candidate_hash=args.expected_candidate_hash,
        expected_terminal_job_id=args.expected_terminal_job_id,
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
    if args.execute:
        completed = finalized_claim_result(ROOT, args.claim_digest)
        if completed is not None:
            print(json.dumps(completed, indent=2, ensure_ascii=False))
            return 0
        # A crash may happen after the canonical claim/fence/ledger/quarantine
        # but before checkpoint clear or finalize receipt.  Resume that prefix
        # before attempting to rebuild a pre-claim candidate proof.
        with _reconciliation_lock(), _index_lock():
            resume = incomplete_claim_resume_identity(ROOT, args.claim_digest)
            if resume is not None:
                result = asyncio.run(_do_abandon_generation(
                    reason=abandon_reason(args.claim_digest),
                    _bypass_rate_limit=True,
                    expected_workflow_run_id=resume["workflow_run_id"],
                    expected_next_v=resume["next_v"],
                    expected_source_v=resume["source_v"],
                    expected_checkpoint_revision=resume["checkpoint_revision"],
                    expected_checkpoint_stage=resume["stage"],
                    _operator_bootstrap_contract_change_claim_digest=(
                        args.claim_digest
                    ),
                ))
                if result.get("abandoned") is True:
                    print(json.dumps({
                        "status": "abandoned_after_crash_resume",
                        "claim_digest": args.claim_digest,
                        "abandon": result,
                    }, indent=2, ensure_ascii=False))
                    return 0
    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        raise RuntimeError("parked bootstrap checkpoint is missing or unreadable")
    claim = _claim(args, checkpoint)
    if not args.execute:
        print(json.dumps({**claim, "mode": "dry_run", "mutates": False}, indent=2, ensure_ascii=False))
        return 0
    if args.claim_digest != claim.get("claim_digest"):
        raise RuntimeError("reviewed dry-run claim digest does not match live proof")
    # Preserve canonical lock order: the abandon owner acquires its workflow
    # actor lock before the publication mutex.  Holding publication here would
    # invert that order against ordinary terminal projections.
    with _reconciliation_lock(), _index_lock():
        locked_checkpoint = read_pipeline_checkpoint()
        if not isinstance(locked_checkpoint, dict):
            raise RuntimeError("checkpoint disappeared before migration lock")
        locked_claim = _claim(args, locked_checkpoint)
        if locked_claim != claim:
            raise RuntimeError("bootstrap contract migration proof changed under lock")
        path = publish_claim(ROOT, locked_claim)
        validate_claim_for_checkpoint(ROOT, locked_checkpoint, args.claim_digest)
        result = asyncio.run(_do_abandon_generation(
            reason=abandon_reason(args.claim_digest),
            _bypass_rate_limit=True,
            expected_workflow_run_id=args.expected_workflow_run_id,
            expected_next_v=locked_checkpoint["next_v"],
            expected_source_v=locked_checkpoint["source_v"],
            expected_checkpoint_revision=args.expected_checkpoint_revision,
            expected_checkpoint_stage="official_bootstrap_required",
            _operator_bootstrap_contract_change_claim_digest=args.claim_digest,
        ))
        if result.get("abandoned") is not True:
            raise RuntimeError(
                "canonical bootstrap contract abandon failed: "
                + json.dumps(result, ensure_ascii=False, sort_keys=True)[:1200]
            )
    print(json.dumps({
        "status": "abandoned",
        "claim_digest": args.claim_digest,
        "claim_path": str(path),
        "abandon": result,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
