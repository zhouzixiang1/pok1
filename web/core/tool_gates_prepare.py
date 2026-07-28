"""Prepare-next-generation stage subsystem.

Extracted from tool_gates.py as a single business responsibility: the
prepare_next_gen stage runner that materializes a candidate directory from
a published strict parent (or the one-time fresh policy bootstrap).
Distinct stage-runner business from run_quality_gates / review / critic.

All public symbols are re-exported by tool_gates.py for backward compatibility;
parent-symbol references route through the _tg alias so test monkeypatches on
tool_gates (e.g. _matching_checkpoint, get_bot_dir) remain authoritative."""

from __future__ import annotations

import tool_gates as _tg  # parent; respects test monkeypatches


async def prepare_next_gen(args):
    _t0 = _tg.time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    active_ckpt = _tg.read_pipeline_checkpoint()
    using_active_checkpoint = False
    if active_ckpt and active_ckpt.get("next_v") is not None and active_ckpt.get("source_v") is not None:
        active_stage = active_ckpt.get("stage")
        active_next_v = int(active_ckpt.get("next_v"))
        active_source_v = int(active_ckpt.get("source_v"))
        active_next_tool = _tg.next_tool_for_checkpoint(active_ckpt)
        if active_stage == "selected" and active_ckpt.get("parent2_v") is not None:
            return _tg._json_tool_result({
                "blocked": True,
                "error": (
                    f"Active generation v{active_next_v} is a crossover from "
                    f"v{active_source_v} x v{active_ckpt.get('parent2_v')}; "
                    "call run_crossover instead of prepare_next_gen."
                ),
                "next_v": active_next_v,
                "source_v": active_source_v,
                "stage": active_stage,
                "next_tool": "run_crossover",
                "required_args": {
                    "version": active_next_v,
                    "parent_a": active_source_v,
                    "parent_b": active_ckpt.get("parent2_v"),
                },
            })
        requested_source = int(source_v) if source_v is not None else None
        requested_next = int(next_v) if next_v is not None else None
        if requested_source is None or requested_next is None:
            source_v = active_source_v
            next_v = active_next_v
            using_active_checkpoint = True
        elif requested_source != active_source_v or requested_next != active_next_v:
            if active_stage in {"selected", "preparing", "prepared"}:
                _tg.log_system_event(
                    "pipeline.prepare_args_overridden",
                    "warn",
                    (
                        f"prepare_next_gen ignored stale args v{requested_next}/source v{requested_source}; "
                        f"using active v{active_next_v}/source v{active_source_v}"
                    ),
                    {
                        "requested_next_v": requested_next,
                        "requested_source_v": requested_source,
                        "next_v": active_next_v,
                        "source_v": active_source_v,
                        "stage": active_stage,
                        "next_tool": "prepare_next_gen",
                    },
                )
                source_v = active_source_v
                next_v = active_next_v
                using_active_checkpoint = True
            else:
                return _tg._json_tool_result({
                    "blocked": True,
                    "error": (
                        f"Active pipeline is v{active_next_v}/source v{active_source_v} "
                        f"at stage {active_stage}; refusing stale prepare request "
                        f"v{requested_next}/source v{requested_source}."
                    ),
                    "next_v": active_next_v,
                    "source_v": active_source_v,
                    "stage": active_stage,
                    "next_tool": active_next_tool,
                })
        else:
            using_active_checkpoint = True
    if source_v is None or next_v is None:
        _v, source_v = _tg._resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return _tg._json_tool_result({"error": "Missing source_v/next_v and no active checkpoint"})

    _tg._set_pipeline_status(f"Preparing v{next_v}")

    if next_v <= source_v:
        return _tg._json_tool_result({"error": f"next_v ({next_v}) must be greater than source_v ({source_v})"})

    # Guard against clearly invalid version numbers (test artifacts)
    if next_v >= 900:
        return _tg._json_tool_result({"error": f"next_v ({next_v}) is invalid. Version numbers must be < 900."})

    current_v = _tg.find_current_v()
    if not using_active_checkpoint and next_v > current_v + 10:
        return _tg._json_tool_result({"error": f"next_v ({next_v}) is too far ahead of current_v ({current_v}). Use next_v = {current_v + 1}."})

    next_dir = _tg.get_bot_dir(next_v)

    source_checkpoint = _tg._matching_checkpoint(next_v, source_v) or {}
    source_audit = source_checkpoint.get("audit_context") or {}
    protocol_bootstrap_receipt = source_audit.get("protocol_bootstrap")
    fresh_policy_bootstrap = bool(
        isinstance(protocol_bootstrap_receipt, dict)
        and protocol_bootstrap_receipt.get("mode")
        == "fresh_national_policy_bootstrap"
        and protocol_bootstrap_receipt.get("source_artifact_inherited") is False
    )
    # In the empty-pool transition v142 is a numeric/tag high-water only.  Do
    # not even resolve its bot path; stale local debris must be unobservable.
    source_dir = None if fresh_policy_bootstrap else _tg.get_bot_dir(source_v)

    # Validate the transition authority before using its mode to bypass any
    # ordinary parent gate.  The one-time 142 -> 143 receipt binds an empty
    # strict pool and explicitly proves that no source artifact is inherited;
    # v142 therefore needs neither an active runtime tree nor a strict tag.
    from evolution_infra import copy_bot_tree_for_candidate, git_has_tag, git_dir_is_committed
    from evolution_infra import get_active_bots

    active_bots = list(get_active_bots())
    if protocol_bootstrap_receipt is not None:
        bootstrap_errors = []
        if fresh_policy_bootstrap:
            from system_strict_bootstrap import validate_fresh_bootstrap_receipt

            bootstrap_errors.extend(validate_fresh_bootstrap_receipt(
                protocol_bootstrap_receipt,
                active_bots=active_bots,
                require_live_epoch_reset=True,
            ))
        else:
            from bot_artifact import canonical_digest
            from bot_namespace import EVALUATION_EPOCH
            from generation_evidence import (
                live_protocol_bootstrap_allocation_errors,
            )

            unsigned = {
                key: value for key, value in protocol_bootstrap_receipt.items()
                if key != "receipt_digest"
            }
            if protocol_bootstrap_receipt.get("receipt_digest") != canonical_digest(unsigned):
                bootstrap_errors.append("policy_bootstrap_receipt_digest_mismatch")
            if protocol_bootstrap_receipt.get("mode") != "singleton_strict_bootstrap":
                bootstrap_errors.append("policy_bootstrap_mode_invalid")
            if protocol_bootstrap_receipt.get("epoch") != EVALUATION_EPOCH:
                bootstrap_errors.append("policy_bootstrap_epoch_mismatch")
            if protocol_bootstrap_receipt.get("next_v") != int(next_v):
                bootstrap_errors.append("policy_bootstrap_target_version_mismatch")
            if protocol_bootstrap_receipt.get("source_artifact_inherited") is not True:
                bootstrap_errors.append("policy_bootstrap_inheritance_mismatch")
            if sorted(protocol_bootstrap_receipt.get("active_bots") or []) != sorted(active_bots):
                bootstrap_errors.append("policy_bootstrap_active_pool_mismatch")
            bootstrap_errors.extend(
                live_protocol_bootstrap_allocation_errors(
                    source_checkpoint,
                    version=int(next_v),
                )
            )
        receipt_source_v = protocol_bootstrap_receipt.get("source_v")
        if receipt_source_v != int(source_v):
            bootstrap_errors.append("policy_bootstrap_source_version_mismatch")
        if bootstrap_errors:
            return _tg._json_tool_result({
                "error": "PROTOCOL_BOOTSTRAP_RECEIPT_INVALID",
                "source_v": source_v,
                "next_v": next_v,
                "validation_errors": bootstrap_errors,
                "directive": (
                    "The zero/one-bot policy transition changed after selection. "
                    "Abandon and reselect; never recover from archived source bytes."
                ),
            })

    if not fresh_policy_bootstrap and (
        source_dir is None or not source_dir.exists()
    ):
        return _tg._json_tool_result({"error": f"Source bot v{source_v} not found"})

    # Every inherited parent, including the one-bot singleton bootstrap, must
    # remain a normally published strict parent at prepare time.  Membership in
    # get_active_bots() revalidates the strict ABI, annotated completion tag,
    # signed full-v5 certificate, lifecycle, and parent-source role.
    if not fresh_policy_bootstrap and (
        source_dir is None or not (source_dir / ".completed").exists()
    ):
        return _tg._json_tool_result({"error": f"Source bot v{source_v} is not marked completed. Cannot use incomplete code as source."})
    if not fresh_policy_bootstrap and not git_has_tag(source_v):
        return _tg._json_tool_result({"error": f"Source bot v{source_v} has no git tag '{_tg.bot_tag(source_v)}'. Cannot evolve from uncommitted code. Try a different source version."})
    if not fresh_policy_bootstrap and _tg.bot_name(source_v) not in set(active_bots):
        return _tg._json_tool_result({
            "error": (
                f"Source bot v{source_v} is not eligible for the active national pool "
                "(strict parent role, publication tag, and signed full-v5 "
                "certificate are all required)."
            )
        })

    # Guard: refuse to overwrite a completed bot
    if next_dir.exists() and (next_dir / ".completed").exists():
        return _tg._json_tool_result({"error": f"Target v{next_v} already exists and is completed. Refusing to overwrite."})

    # Guard: refuse to overwrite a bare-committed target (root-cause fix for the
    # v117 repeated-regeneration loop, 2026-06-18; mirrors run_crossover).
    if next_dir.exists() and git_dir_is_committed(next_v) and not git_has_tag(next_v):
        return _tg._json_tool_result({
            "error": f"Target v{next_v} is git-committed but has no {_tg.bot_tag(next_v)} tag (bare commit bypassing commit_bot). "
                     f"Refusing to overwrite — re-preparing here causes infinite regeneration. "
                     f"Run commit_bot for v{next_v} to finalize it, or abandon/clear the untagged dir first."
        })

    # Guard: refuse to re-prepare if pipeline has already progressed past "prepared"
    _ckpt = _tg._matching_checkpoint(next_v, source_v)
    if _ckpt and _ckpt.get("stage") not in (
        None,
        "selected",
        "preparing",
        "prepared",
    ):
        return _tg._json_tool_result({"error": f"Pipeline for v{next_v} already at stage '{_ckpt['stage']}'. Refusing to overwrite worker output. Call abandon_generation first if you want to restart."})

    if next_dir.exists():
        prepared_contract = (
            ((_ckpt or {}).get("audit_context") or {}).get(
                "prepared_artifact_contract"
            )
        )
        if (_ckpt or {}).get("stage") == "prepared":
            from prepared_baseline_contract import (
                validate_prepared_artifact_contract,
            )

            retry_errors = validate_prepared_artifact_contract(
                prepared_contract,
                prepared_dir=next_dir,
                source_v=int(source_v),
                next_v=int(next_v),
                verify_live_content=True,
            )
            if not retry_errors:
                return _tg._json_tool_result({
                    "success": True,
                    "resumed": True,
                    "version": int(next_v),
                    "source_v": int(source_v),
                    "stage": "prepared",
                    "prepared_artifact_hash": prepared_contract.get(
                        "prepared_artifact_hash"
                    ),
                    "directive": (
                        "Exact checkpoint-bound prepared artifact already exists; "
                        "continue with run_direction_audit."
                    ),
                })
        # ``preparing`` is a crash-recovery lease, but bytes that appeared
        # before the prepared-artifact contract was committed can never be
        # adopted by filename.  Resolve that kill window here, inside the
        # system-owned prepare route, with the same exact workflow/revision
        # canonical-abandon transaction used by every other terminal path.
        try:
            from tool_bot_management import (
                _do_abandon_generation,
                expected_abandon_identity,
            )

            abandon_result = await _do_abandon_generation(
                reason="stale_blueprint_rejection:prepare_preimage_unbound",
                **expected_abandon_identity(_ckpt),
            )
        except Exception as exc:
            abandon_result = {
                "abandoned": False,
                "reason": "prepare_preimage_abandon_exception",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
        return _tg._json_tool_result({
            "error": "TARGET_PREIMAGE_REQUIRES_CANONICAL_ABANDON",
            "version": int(next_v),
            "source_v": int(source_v),
            "stage": (_ckpt or {}).get("stage"),
            "action": "abandon_generation",
            "abandoned": abandon_result.get("abandoned") is True,
            "abandon_result": abandon_result,
            "directive": (
                "The existing target directory is not bound by this workflow's "
                "exact prepared-artifact contract. The system-owned prepare "
                "route attempted the checkpoint-bound abandon/quarantine "
                "transaction; never rmtree or adopt filename-matched bytes."
            ),
        })

    from evolution_infra import write_pipeline_checkpoint
    if not write_pipeline_checkpoint(
        next_v,
        source_v,
        "preparing",
        worker_failure_count=0,
        expected_checkpoint_revision=(_ckpt or {}).get(
            "checkpoint_revision"
        ),
        expected_checkpoint_stage=(_ckpt or {}).get("stage"),
        expected_workflow_run_id=(_ckpt or {}).get("workflow_run_id"),
    ):
        return _tg._json_tool_result({
            "error": f"Failed to persist preparing checkpoint for v{next_v}; refusing to mutate bot directory."
        })
    preparing_checkpoint = _tg._matching_checkpoint(next_v, source_v)
    if (
        not isinstance(preparing_checkpoint, dict)
        or preparing_checkpoint.get("stage") != "preparing"
    ):
        return _tg._json_tool_result({
            "error": (
                f"Preparing checkpoint for v{next_v} could not be re-proven; "
                "refusing to mutate bot directory."
            )
        })

    workflow_profile = _tg.get_workflow_profile()
    native_tcp = getattr(workflow_profile, "national_execution_mode", None) == "native_tcp"
    if fresh_policy_bootstrap:
        from system_strict_bootstrap import materialize_fresh_candidate

        materialized = materialize_fresh_candidate(next_dir, version=int(next_v))
        hygiene = {
            "native_entry": str(next_dir / "national_bot.py"),
            "native_entry_refreshed": True,
            "fresh_policy_artifact": True,
            "artifact_hash": materialized["artifact_hash"],
        }
    else:
        assert source_dir is not None
        copy_bot_tree_for_candidate(source_dir, next_dir)

        # The parent's receipt is version- and lineage-bound.  Copying it into
        # vN+1 would freeze an invalid prepared baseline and make every later
        # policy edit unpublishable.  Preparation, not the Worker, owns this
        # deterministic version transition.
        try:
            from bot_namespace import refresh_policy_identity_documents

            prepared_lineage = _tg.strict_lineage_parent_versions(
                int(next_v), int(source_v), None
            )
            prepared_identity = refresh_policy_identity_documents(
                next_dir,
                int(next_v),
                parent_versions=prepared_lineage,
            )
        except Exception as exc:
            return _tg._json_tool_result({
                "error": "PREPARED_POLICY_IDENTITY_REFRESH_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
                "directive": (
                    "The system could not bind the copied parent to the new "
                    "version/lineage. Do not freeze or run this candidate."
                ),
            })

        from candidate_hygiene import sanitize_candidate_dir
        hygiene = sanitize_candidate_dir(
            next_dir,
            require_native_tcp=native_tcp,
        )
        hygiene["policy_identity_refreshed"] = True
        hygiene["policy_identity"] = prepared_identity
    protocol_bootstrap_prepare = None
    if native_tcp and isinstance(protocol_bootstrap_receipt, dict):
        from bot_namespace import (
            NATIONAL_RUNTIME_MANIFEST,
            POLICY_EPOCH_RECEIPT,
            epoch_receipt_errors,
            runtime_manifest_errors,
        )
        try:
            runtime_manifest = _tg.json.loads(
                (next_dir / NATIONAL_RUNTIME_MANIFEST).read_text(encoding="utf-8")
            )
            epoch_receipt = _tg.json.loads(
                (next_dir / POLICY_EPOCH_RECEIPT).read_text(encoding="utf-8")
            )
            prepared_runtime_errors = [
                *runtime_manifest_errors(next_dir, runtime_manifest),
                *epoch_receipt_errors(
                    next_dir, int(next_v), runtime_manifest, epoch_receipt
                ),
            ]
        except Exception as exc:
            prepared_runtime_errors = [
                f"policy_identity_unreadable:{type(exc).__name__}:{str(exc)[:160]}"
            ]
        if prepared_runtime_errors:
            return _tg._json_tool_result({
                "error": "PROTOCOL_BOOTSTRAP_RUNTIME_REPLACEMENT_FAILED",
                "next_v": next_v,
                "source_v": source_v,
                "validation_errors": prepared_runtime_errors[:20],
                "directive": (
                    "The system-owned current national runtime was not established "
                    "before the prepared snapshot. Do not continue to Master."
                ),
            })
        entry_path = next_dir / "national_bot.py"
        protocol_bootstrap_prepare = {
            "receipt_digest": protocol_bootstrap_receipt.get("receipt_digest"),
            "mode": protocol_bootstrap_receipt.get("mode"),
            "system_runtime_replaced": bool(hygiene.get("native_entry_refreshed")),
            "source_artifact_inherited": not fresh_policy_bootstrap,
            "national_bot_sha256": _tg.hashlib.sha256(entry_path.read_bytes()).hexdigest(),
            "runtime_manifest_digest": _tg.hashlib.sha256(
                (next_dir / NATIONAL_RUNTIME_MANIFEST).read_bytes()
            ).hexdigest(),
            "epoch_receipt_digest": _tg.hashlib.sha256(
                (next_dir / POLICY_EPOCH_RECEIPT).read_bytes()
            ).hexdigest(),
        }
    prepare_scope_files = (
        sorted(path.name for path in next_dir.iterdir() if path.is_file())
        if fresh_policy_bootstrap
        else [
            # ``source_dir`` is necessarily a published strict parent here.
            p for p in _tg._py_files_changed_between(source_dir, next_dir)
            if 'backup' not in p
        ]
    )
    if prepare_scope_files:
        _tg.log_system_event(
            "pipeline.prepare_scope_captured",
            "info",
            f"Prepare baseline for v{next_v} changed {len(prepare_scope_files)} file(s)",
            {
                "next_v": next_v,
                "source_v": source_v,
                "prepare_scope_files": prepare_scope_files[:20],
            },
        )
    if native_tcp:
        _tg.log_system_event(
            "pipeline.native_entry_prepared",
            "info",
            f"Prepared native national TCP entry for v{next_v}",
            {"next_v": next_v, "source_v": source_v, "entry": hygiene.get("native_entry")},
        )

    try:
        from prepared_baseline_contract import build_prepared_artifact_contract

        prepared_artifact_contract = build_prepared_artifact_contract(
            next_dir,
            source_v=source_v,
            next_v=next_v,
        )
    except Exception as exc:
        return _tg._json_tool_result({
            "error": "PREPARED_ARTIFACT_CONTRACT_BUILD_FAILED",
            "next_v": next_v,
            "source_v": source_v,
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
            "directive": "Do not run Direction Audit or Master without a frozen prepared artifact.",
        })

    # Write "prepared" checkpoint so a kill+restart shows "Workers not yet run → call run_direction_audit"
    if not write_pipeline_checkpoint(
        next_v,
        source_v,
        "prepared",
        worker_failure_count=0,
        prepare_scope_files=prepare_scope_files,
        audit_context={
            "prepared_artifact_contract": prepared_artifact_contract,
            **(
                {"protocol_bootstrap_prepare": protocol_bootstrap_prepare}
                if protocol_bootstrap_prepare is not None
                else {}
            ),
        },
        expected_checkpoint_revision=preparing_checkpoint.get(
            "checkpoint_revision"
        ),
        expected_checkpoint_stage="preparing",
        expected_workflow_run_id=preparing_checkpoint.get(
            "workflow_run_id"
        ),
    ):
        return _tg._json_tool_result({
            "error": f"Failed to persist prepared checkpoint for v{next_v}; generation recovery remains at preparing."
        })

    _tg.log_system_event("pipeline.prepare_done", "info", f"Prepared v{next_v} from v{source_v}",
                     {"next_v": next_v, "source_v": source_v, "elapsed_sec": round(_tg.time.time() - _t0, 2)})
    try:
        from repo_state import log_git_worktree_snapshot
        log_git_worktree_snapshot(
            "repo.worktree_snapshot",
            f"Worktree snapshot after preparing v{next_v}",
            next_v=next_v,
            source_v=source_v,
            stage="prepared",
            emit_delta=True,
        )
    except Exception:
        pass

    return _tg._json_tool_result({"prepared": True, "next_v": next_v, "source_v": source_v})
