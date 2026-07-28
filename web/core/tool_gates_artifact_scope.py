"""Prepared-artifact delta + declared-scope + crossover helpers.

Extracted from tool_gates.py: the crossover/prepared-artifact file-delta
verdict, declared-scope task derivation, and scope-checkpoint helpers used
by run_quality_gates to authorize which changed files a generation may
introduce against its frozen prepared baseline.

All public symbols are re-exported by tool_gates.py for backward compatibility."""

from __future__ import annotations

import tool_gates as _tg  # parent; respects test monkeypatches


def _declared_scope_tasks_from_plan(
    master_plan,
    checkpoint=None,
    *,
    include_prepare_scope=True,
):
    tasks = []
    if isinstance(master_plan, dict):
        raw_tasks = master_plan.get("tasks", []) or []
        if isinstance(raw_tasks, list):
            tasks.extend(raw_tasks)
        raw_repair_scope = master_plan.get("repair_scope_files", []) or []
        if not isinstance(raw_repair_scope, list):
            raw_repair_scope = []
        repair_scope_files = [
            str(item).strip()
            for item in raw_repair_scope
            if str(item).strip()
        ]
        if repair_scope_files:
            tasks.append({
                "worker_id": "repair_scope_history",
                "role": "Scope Ledger",
                "target_files": [],
                "files_allowed": sorted(set(repair_scope_files)),
            })
    if include_prepare_scope and isinstance(checkpoint, dict):
        raw_prepare_scope = checkpoint.get("prepare_scope_files", []) or []
        if not isinstance(raw_prepare_scope, list):
            raw_prepare_scope = []
        prepare_scope_files = [
            str(item).strip()
            for item in raw_prepare_scope
            if str(item).strip()
        ]
        if prepare_scope_files:
            tasks.append({
                "worker_id": "prepare_scope_history",
                "role": "Prepare Scope Ledger",
                "target_files": [],
                "files_allowed": sorted(set(prepare_scope_files)),
            })
    return tasks


def _is_crossover_scope_checkpoint(ckpt, master_plan):
    if not isinstance(ckpt, dict):
        ckpt = {}
    if not isinstance(master_plan, dict):
        master_plan = {}
    work_item = master_plan.get("work_item") if isinstance(master_plan.get("work_item"), dict) else {}
    return (
        bool(ckpt.get("parent2_v"))
        or master_plan.get("strategy") == "crossover"
        or str(work_item.get("kind", "")).startswith("crossover_")
    )


def _master_plan_with_crossover_scope(master_plan, ckpt, changed_files):
    """Return the declared plan without deriving authority from the diff.

    Crossover preparation files are already frozen in the checkpoint's
    ``prepare_scope_files`` ledger and appended by
    :func:`_declared_scope_tasks_from_plan`.  Promoting every observed changed
    file into ``repair_scope_files`` made the final scope audit tautological:
    an out-of-band or recovery-time edit authorized itself merely by appearing
    in the diff.  Keep this compatibility helper side-effect free; authority is
    the prepared ledger plus explicit Master/repair task scope only.
    """
    return master_plan


def _crossover_post_master_delta(checkpoint, candidate_artifact_hash):
    """Verify Workers changed the common frozen prepared artifact."""
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    audit_context = checkpoint.get("audit_context") or {}
    prepared = (
        audit_context.get("prepared_artifact_contract")
        if isinstance(audit_context, dict)
        else None
    )
    if not isinstance(prepared, dict):
        crossover_baseline = (
            audit_context.get("prepared_baseline_contract")
            if isinstance(audit_context, dict)
            else None
        )
        if isinstance(crossover_baseline, dict):
            prepared = crossover_baseline.get("prepared_artifact_contract")
    required = bool(
        checkpoint.get("next_v") is not None
        and checkpoint.get("source_v") is not None
    )
    prepared = prepared if isinstance(prepared, dict) else {}
    prepared_hash = str(
        prepared.get("prepared_artifact_hash") or ""
        if isinstance(prepared, dict)
        else ""
    )
    candidate_hash = str(candidate_artifact_hash or "")
    ok = bool(
        not required
        or (prepared_hash and candidate_hash and candidate_hash != prepared_hash)
    )
    return required, ok, prepared_hash


def _prepared_artifact_delta_files(checkpoint, candidate_dir):
    """Diff the frozen prepared manifest against the final complete artifact."""
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    audit_context = checkpoint.get("audit_context") or {}
    contract = (
        audit_context.get("prepared_artifact_contract")
        if isinstance(audit_context, dict)
        else None
    )
    if not isinstance(contract, dict):
        crossover_baseline = (
            audit_context.get("prepared_baseline_contract")
            if isinstance(audit_context, dict)
            else None
        )
        if isinstance(crossover_baseline, dict):
            contract = crossover_baseline.get("prepared_artifact_contract")
    if not isinstance(contract, dict):
        return [], ["prepared_artifact_contract_missing_for_scope"]
    try:
        from prepared_baseline_contract import validate_prepared_artifact_contract

        contract_errors = validate_prepared_artifact_contract(
            contract,
            source_v=checkpoint.get("source_v"),
            next_v=checkpoint.get("next_v"),
            verify_live_content=False,
        )
    except Exception as exc:
        return [], [
            "prepared_baseline_contract_scope_validation_error:"
            f"{type(exc).__name__}: {str(exc)[:200]}"
        ]
    if contract_errors:
        return [], [f"prepared_scope:{error}" for error in contract_errors]
    prepared_manifest = contract.get("prepared_artifact_manifest")
    if not isinstance(prepared_manifest, dict):
        return [], ["prepared_artifact_manifest_missing_for_scope"]
    try:
        from bot_artifact import artifact_manifest

        current_manifest = artifact_manifest(candidate_dir)
    except Exception as exc:
        return [], [
            f"candidate_artifact_manifest_error:{type(exc).__name__}: {str(exc)[:200]}"
        ]

    def _entries(manifest):
        entries = {}
        for item in manifest.get("entries") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "file":
                continue
            path = str(item.get("path") or "")
            if not path or path == ".":
                continue
            entries[path] = {
                key: item.get(key)
                for key in ("type", "size", "sha256")
                if key in item
            }
        return entries

    prepared_entries = _entries(prepared_manifest)
    current_entries = _entries(current_manifest)
    changed = sorted(
        path
        for path in set(prepared_entries) | set(current_entries)
        if prepared_entries.get(path) != current_entries.get(path)
    )
    return changed, []


def _prepared_artifact_change_status(checkpoint, candidate_dir, candidate_artifact_hash):
    """Return the blocking post-prepare file-delta verdict and evidence."""
    required, hash_delta_ok, prepared_hash = _tg._crossover_post_master_delta(
        checkpoint,
        candidate_artifact_hash,
    )
    changed_files, scope_errors = _tg._prepared_artifact_delta_files(
        checkpoint,
        candidate_dir,
    )
    # Full hashes include directory entries, but empty directory churn is not
    # a decision innovation.  For a real generation, regular-file delta is the
    # only authoritative verdict.  The fallback preserves legacy no-source
    # diagnostic callers where no prepared contract is required.
    changed_ok = (
        bool(changed_files) and not scope_errors
        if required
        else bool(hash_delta_ok)
    )
    return {
        "required": required,
        "changed_ok": changed_ok,
        "prepared_artifact_hash": prepared_hash,
        "changed_files": changed_files,
        "scope_errors": scope_errors,
    }
