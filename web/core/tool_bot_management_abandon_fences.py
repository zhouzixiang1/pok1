"""Companion for tool_bot_management: abandon-fence validation & historical reproof.

Every intra-companion call routes through the main module as ``_tbm`` so moved
symbols stay single-dispatch even when invoked from the main module.
"""

from __future__ import annotations

import tool_bot_management as _tbm


def _validate_active_abandon_claim(claim: dict) -> dict:
    """Reopen all live authority; never trust a merely re-signed sidecar."""
    _tbm.validate_abandon_claim_structure(claim)
    _tbm._validate_claim_first_strict_execution_fence(claim)
    _tbm._validate_external_bootstrap_contract_abandon_proof(claim)
    checkpoint_identity = claim['checkpoint']
    version = int(checkpoint_identity['next_v'])
    current_git = _tbm._current_abandon_git_state(version)
    if current_git != claim['git_state']:
        raise RuntimeError('recorded_abandon_active_git_state_changed')
    transaction_dir, quarantine = _tbm._claim_transaction_paths(claim)
    _tbm._assert_safe_existing_transaction_chain(transaction_dir)
    transaction_claim = transaction_dir / 'claim.json'
    if not _tbm.os.path.lexists(transaction_claim):
        raise RuntimeError('recorded_abandon_transaction_claim_missing')
    if _tbm._read_json_regular(transaction_claim) != claim:
        raise RuntimeError('recorded_abandon_transaction_claim_mismatch')
    candidate = _tbm.Path(_tbm.get_bot_dir(version))
    state = _tbm._validate_claim_candidate_state(claim, candidate, quarantine)
    rows = _tbm.load_abandoned_version_receipts(path=_tbm.Path(_tbm.RESULTS_DIR) / 'abandoned_versions.jsonl', project_root=_tbm.PROJECT_ROOT)
    abandon_receipt = _tbm.validate_abandon_ledger_history(claim, rows, require_active_head=True)
    from evolution_core import PIPELINE_STATE_FILE
    checkpoint_path = _tbm.Path(PIPELINE_STATE_FILE)
    checkpoint_exists = _tbm.os.path.lexists(checkpoint_path)
    if checkpoint_exists:
        checkpoint = _tbm.read_pipeline_checkpoint()
        if not isinstance(checkpoint, dict) or _tbm.canonical_digest(checkpoint) != checkpoint_identity['digest'] or _tbm._checkpoint_transaction_identity(checkpoint) != checkpoint_identity:
            raise RuntimeError('recorded_abandon_active_checkpoint_changed')
        if abandon_receipt is None and state not in {'source', 'absent'}:
            raise RuntimeError('recorded_abandon_phase_invalid_before_ledger')
    else:
        if abandon_receipt is None:
            raise RuntimeError('recorded_abandon_receipt_missing_after_checkpoint_clear')
        if claim['candidate']['present'] is True and state != 'quarantine':
            raise RuntimeError('recorded_abandon_source_invalid_after_checkpoint_clear')
        if claim['candidate']['present'] is False and state != 'absent':
            raise RuntimeError('recorded_abandon_absent_phase_invalid')
    finalize_path = transaction_dir / 'receipt.json'
    if _tbm.os.path.lexists(finalize_path):
        _tbm.validate_abandon_finalize_receipt(claim, _tbm._read_json_regular(finalize_path), rows)
    return claim


def _validate_claim_first_strict_execution_fence(claim: dict) -> dict | None:
    """Reopen the schema-3 journal receipt bound before checkpoint removal."""
    if claim.get('schema_version') != 3:
        return None
    fence = claim.get('first_strict_execution_fence')
    if not isinstance(fence, dict):
        raise RuntimeError('recorded_abandon_first_strict_fence_missing')
    try:
        from first_strict_execution_journal import read_abandoned_control_execution
        observed = read_abandoned_control_execution(fence.get('scope'), reason=str(claim.get('abandon_reason') or ''), expected_terminal_receipt=fence.get('terminal_receipt'))
    except Exception as exc:
        raise RuntimeError(f'recorded_abandon_first_strict_fence_unverifiable:{type(exc).__name__}:{str(exc)[:240]}') from exc
    if observed != fence.get('terminal_receipt'):
        raise RuntimeError('recorded_abandon_first_strict_fence_changed')
    return fence


def _validate_abandon_workflow_fences(*, workflow_run_id: str, abandon_reason: str, require_worker_outer_reason: bool, require_strict_authority: bool=True) -> dict:
    """Read-only proof of the Worker prefix and optional strict child fence."""
    from strict_authority_workflow import DEFINITION_VERSION, authority_run_id, strict_authority_abandon_event_identity
    from worker_workflow import WORKER_WORKFLOW_DEFINITION_VERSION, replay_worker_events, worker_abandon_event_identity
    from workflow_kernel import KERNEL_SCHEMA_VERSION, WorkflowEvent, canonical_json, content_digest
    workflow_run_id = str(workflow_run_id or '')
    if not workflow_run_id:
        raise RuntimeError('completed_abandon_workflow_run_id_missing')
    strict_run_id = authority_run_id(workflow_run_id)
    database = _tbm.Path(_tbm.RESULTS_DIR) / 'workflow' / 'events.sqlite3'
    if not _tbm.os.path.lexists(database):
        raise RuntimeError('completed_abandon_workflow_database_missing')
    metadata = _tbm.os.lstat(database)
    if _tbm.stat.S_ISLNK(metadata.st_mode) or not _tbm.stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError('completed_abandon_workflow_database_unsafe')
    expected = _tbm.Path(_tbm.RESULTS_DIR).resolve() / 'workflow' / 'events.sqlite3'
    try:
        resolved = database.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError('completed_abandon_workflow_database_unavailable') from exc
    if resolved != expected:
        raise RuntimeError('completed_abandon_workflow_database_escaped')
    connection = _tbm.sqlite3.connect(f'{resolved.as_uri()}?mode=ro', uri=True, timeout=30.0, isolation_level=None)
    connection.row_factory = _tbm.sqlite3.Row
    claim_outer_reason = str(abandon_reason)
    if not claim_outer_reason:
        raise RuntimeError('completed_abandon_outer_reason_missing')
    outer_reason = claim_outer_reason[:1000]
    try:
        connection.execute('PRAGMA query_only=ON')
        user_version = int(connection.execute('PRAGMA user_version').fetchone()[0])
        if user_version != KERNEL_SCHEMA_VERSION:
            raise RuntimeError('completed_abandon_workflow_schema_invalid')
        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('completed_abandon_workflow_foreign_key_invalid')
        connection.execute('BEGIN')

        def bounded_terminal_reason(payload: dict, *, event_type: str) -> str:
            reason = payload.get('reason')
            if not isinstance(reason, str) or not reason or len(reason) > _tbm._TERMINAL_REASON_MAX_CHARS:
                raise RuntimeError(f'completed_abandon_{event_type}_reason_invalid')
            return reason

        def terminal_projection(run_id: str, event_type: str, expected_definition_version: int, *, require_outer_reason: bool) -> dict:
            instance = connection.execute('SELECT definition_version, stream_version, status, fence_epoch FROM workflow_instances WHERE run_id = ?', (run_id,)).fetchone()
            if instance is None:
                raise RuntimeError(f'completed_abandon_{event_type}_instance_missing')
            history = connection.execute('SELECT seq, event_type, schema_version, payload, payload_digest, causation_id FROM workflow_events WHERE run_id = ? ORDER BY seq', (run_id,)).fetchall()
            stream_version = int(instance['stream_version'])
            if len(history) != stream_version or [int(row['seq']) for row in history] != list(range(1, stream_version + 1)):
                raise RuntimeError(f'completed_abandon_{event_type}_history_sequence_invalid')
            decoded_history = []
            for row in history:
                try:
                    row_payload = _tbm.json.loads(row['payload'])
                except (TypeError, _tbm.json.JSONDecodeError) as exc:
                    raise RuntimeError(f'completed_abandon_{event_type}_history_payload_invalid') from exc
                row_digest = _tbm.hashlib.sha256(canonical_json(row_payload).encode('utf-8')).hexdigest()
                if int(row['schema_version']) != 1 or row['payload_digest'] != row_digest:
                    raise RuntimeError(f'completed_abandon_{event_type}_history_digest_invalid')
                decoded_history.append((row, row_payload))
            events = [item for item in decoded_history if item[0]['event_type'] == event_type]
            if len(events) != 1:
                raise RuntimeError(f'completed_abandon_{event_type}_event_count_invalid')
            event, payload = events[0]
            if int(instance['definition_version']) != int(expected_definition_version) or instance['status'] != 'abandoned' or int(instance['fence_epoch']) < 1 or (int(event['seq']) != stream_version):
                raise RuntimeError(f'completed_abandon_{event_type}_terminal_invalid')
            terminal_reason = bounded_terminal_reason(payload, event_type=event_type)
            if require_outer_reason and terminal_reason != outer_reason:
                raise RuntimeError(f'completed_abandon_{event_type}_outer_reason_mismatch')
            if event_type == 'WorkerAbandoned':
                worker_events = [WorkflowEvent(run_id=run_id, seq=int(row['seq']), event_type=str(row['event_type']), schema_version=int(row['schema_version']), payload=row_payload, payload_digest=str(row['payload_digest']), causation_id=str(row['causation_id'])) for row, row_payload in decoded_history]
                try:
                    worker_state = replay_worker_events(run_id, worker_events)
                except Exception as exc:
                    raise RuntimeError('completed_abandon_WorkerAbandoned_replay_invalid') from exc
                cycle = int(worker_state.get('cycle') or 0)
                expected_payload, expected_causation = worker_abandon_event_identity(run_id, reason=terminal_reason, cycle=cycle)
                expected_causations = {expected_causation}
                if terminal_reason == outer_reason and claim_outer_reason != terminal_reason:
                    expected_causations.add(f'worker-abandoned:{run_id}:cycle-{cycle}:{content_digest(claim_outer_reason)}')
                if payload != expected_payload or event['causation_id'] not in expected_causations:
                    raise RuntimeError('completed_abandon_WorkerAbandoned_reason_unbound')
            if event_type == 'StrictAuthorityAbandoned':
                expected_payload, expected_causation = strict_authority_abandon_event_identity({'workflow_run_id': workflow_run_id}, reason=terminal_reason)
                if payload != expected_payload:
                    raise RuntimeError('completed_abandon_StrictAuthorityAbandoned_binding_invalid')
                if event['causation_id'] != expected_causation:
                    raise RuntimeError('completed_abandon_StrictAuthorityAbandoned_reason_unbound')
            live_effects = connection.execute("SELECT COUNT(*) FROM effects WHERE run_id = ? AND status NOT IN ('completed', 'exhausted', 'abandoned')", (run_id,)).fetchone()[0]
            if int(live_effects) != 0:
                raise RuntimeError(f'completed_abandon_{event_type}_effects_still_live')
            return {'run_id': run_id, 'stream_version': int(instance['stream_version']), 'fence_epoch': int(instance['fence_epoch']), 'terminal_event': event_type, 'terminal_reason': terminal_reason}
        main = terminal_projection(workflow_run_id, 'WorkerAbandoned', WORKER_WORKFLOW_DEFINITION_VERSION, require_outer_reason=require_worker_outer_reason)
        strict = terminal_projection(strict_run_id, 'StrictAuthorityAbandoned', DEFINITION_VERSION, require_outer_reason=True) if require_strict_authority else None
        connection.rollback()
    finally:
        connection.close()
    after = _tbm.os.lstat(database)
    if not _tbm.stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise RuntimeError('completed_abandon_workflow_database_changed')
    proof = {'worker': main}
    if strict is not None:
        proof['strict_authority'] = strict
    return proof


def _validate_completed_abandon_workflow_fences(claim: dict) -> dict:
    """Reprove historical/finalized claim fences, including legacy Worker reasons."""
    return _tbm._validate_abandon_workflow_fences(workflow_run_id=str(claim['checkpoint']['workflow_run_id']), abandon_reason=str(claim['abandon_reason']), require_worker_outer_reason=False, require_strict_authority=True)


def _terminal_gate_abandon_identity(checkpoint: dict, *, reason: str) -> tuple[str, str]:
    if not isinstance(checkpoint, dict):
        raise RuntimeError('terminal_gate_abandon_checkpoint_invalid')
    outcome = checkpoint.get('terminal_gate_outcome')
    if not isinstance(outcome, dict):
        raise RuntimeError('terminal_gate_abandon_outcome_missing')
    workflow_run_id = str(checkpoint.get('workflow_run_id') or '')
    receipt_digest = str(outcome.get('receipt_digest') or '')
    expected_reason = f'terminal_gate_outcome:{receipt_digest}'
    if checkpoint.get('stage') not in {'quality_rejected', 'review_rejected', 'critic_rejected'} or outcome.get('workflow_run_id') != workflow_run_id or outcome.get('terminal_stage') != checkpoint.get('stage') or (len(receipt_digest) != 64) or any((char not in '0123456789abcdef' for char in receipt_digest)) or (str(reason) != expected_reason):
        raise RuntimeError('terminal_gate_abandon_fence_identity_invalid')
    return (workflow_run_id, expected_reason)


def validate_terminal_gate_abandon_fences(checkpoint: dict, *, reason: str) -> dict:
    """Prove the exact already-fenced lifecycle of one terminal gate receipt.

    This is the narrow bridge needed by canonical abandon's second state guard
    and by a crash retry after both journals were fenced.  It does not create,
    repair, or relax either journal.
    """
    workflow_run_id, expected_reason = _tbm._terminal_gate_abandon_identity(checkpoint, reason=reason)
    return _tbm._validate_abandon_workflow_fences(workflow_run_id=workflow_run_id, abandon_reason=expected_reason, require_worker_outer_reason=True, require_strict_authority=True)


def terminal_gate_abandon_fence_proof_if_present(checkpoint: dict, *, reason: str) -> dict | None:
    """Return exact terminal fence proof, or ``None`` before fencing begins.

    Seeing either journal already abandoned is an irreversible lifecycle
    boundary.  The exact Worker-first prefix may finish the strict fence after
    a crash; every mismatched prefix or strict-first/partial shape fails closed
    rather than being mistaken for the ordinary pre-fence validation pass.
    """
    from strict_authority_workflow import DEFINITION_VERSION, authority_run_id
    if not isinstance(checkpoint, dict):
        raise RuntimeError('terminal_gate_abandon_checkpoint_invalid')
    workflow_run_id, expected_reason = _tbm._terminal_gate_abandon_identity(checkpoint, reason=reason)
    strict_run_id = authority_run_id(workflow_run_id)
    database = _tbm.Path(_tbm.RESULTS_DIR) / 'workflow' / 'events.sqlite3'
    if not _tbm.os.path.lexists(database):
        return None
    metadata = _tbm.os.lstat(database)
    if _tbm.stat.S_ISLNK(metadata.st_mode) or not _tbm.stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError('terminal_gate_abandon_workflow_database_unsafe')
    expected = _tbm.Path(_tbm.RESULTS_DIR).resolve() / 'workflow' / 'events.sqlite3'
    resolved = database.resolve(strict=True)
    if resolved != expected:
        raise RuntimeError('terminal_gate_abandon_workflow_database_escaped')
    connection = _tbm.sqlite3.connect(f'{resolved.as_uri()}?mode=ro', uri=True, timeout=30.0, isolation_level=None)
    try:
        connection.execute('PRAGMA query_only=ON')
        rows = connection.execute('SELECT run_id, definition_version, stream_version, status, fence_epoch FROM workflow_instances WHERE run_id IN (?, ?)', (workflow_run_id, strict_run_id)).fetchall()
        strict_event_count = int(connection.execute('SELECT COUNT(*) FROM workflow_events WHERE run_id = ?', (strict_run_id,)).fetchone()[0])
        strict_effect_count = int(connection.execute('SELECT COUNT(*) FROM effects WHERE run_id = ?', (strict_run_id,)).fetchone()[0])
    finally:
        connection.close()
    instances = {str(row[0]): row for row in rows}
    worker = instances.get(workflow_run_id)
    strict = instances.get(strict_run_id)
    worker_abandoned = worker is not None and str(worker[3]) == 'abandoned'
    strict_abandoned = strict is not None and str(strict[3]) == 'abandoned'
    if not worker_abandoned and (not strict_abandoned):
        return None
    exact_legacy_strict_tombstone = bool(strict is not None and int(strict[1]) == DEFINITION_VERSION and (int(strict[2]) == 0) and (str(strict[3]) == 'abandoned') and (int(strict[4]) == 0) and (strict_event_count == 0) and (strict_effect_count == 0))
    if worker_abandoned and exact_legacy_strict_tombstone:
        return _tbm._validate_abandon_workflow_fences(workflow_run_id=workflow_run_id, abandon_reason=expected_reason, require_worker_outer_reason=True, require_strict_authority=False)
    if worker_abandoned and (not strict_abandoned):
        if strict is not None and (int(strict[1]) != DEFINITION_VERSION or (int(strict[2]) == 0 and int(strict[4]) == 0 and (strict_event_count == 0) and (strict_effect_count == 0))):
            raise RuntimeError('terminal_gate_abandon_strict_prefix_invalid')
        return _tbm._validate_abandon_workflow_fences(workflow_run_id=workflow_run_id, abandon_reason=expected_reason, require_worker_outer_reason=True, require_strict_authority=False)
    return _tbm.validate_terminal_gate_abandon_fences(checkpoint, reason=reason)


def validate_completed_abandon_handoff(checkpoint: dict, result: dict) -> dict:
    """Reprove the exact finalized abandon returned to one provider stream."""
    if not isinstance(checkpoint, dict) or not isinstance(result, dict):
        raise RuntimeError('completed_abandon_handoff_material_invalid')
    transaction_id = str(result.get('abandon_transaction_id') or '')
    if len(transaction_id) != 64 or any((char not in '0123456789abcdef' for char in transaction_id)):
        raise RuntimeError('completed_abandon_transaction_id_invalid')
    transaction_dir = _tbm.Path(_tbm.RESULTS_DIR) / 'policy_epoch_abandon_transactions' / transaction_id
    _tbm._assert_safe_existing_transaction_chain(transaction_dir)
    claim = _tbm._read_json_regular(transaction_dir / 'claim.json')
    if claim.get('transaction_id') != transaction_id:
        raise RuntimeError('completed_abandon_transaction_identity_mismatch')
    baseline_identity = _tbm._checkpoint_transaction_identity(checkpoint)
    terminal_identity = claim.get('checkpoint')
    if not isinstance(terminal_identity, dict):
        raise RuntimeError('completed_abandon_checkpoint_identity_invalid')
    if any((terminal_identity.get(field) != baseline_identity.get(field) for field in ('workflow_run_id', 'next_v', 'source_v'))):
        raise RuntimeError('completed_abandon_checkpoint_identity_mismatch')
    baseline_revision = baseline_identity.get('checkpoint_revision')
    terminal_revision = terminal_identity.get('checkpoint_revision')
    if type(baseline_revision) is not int or baseline_revision < 1 or type(terminal_revision) is not int or (terminal_revision < baseline_revision):
        raise RuntimeError('completed_abandon_checkpoint_revision_invalid')
    _tbm._validate_active_abandon_claim(claim)
    workflow_fences = _tbm._validate_completed_abandon_workflow_fences(claim)
    finalize_path = transaction_dir / 'receipt.json'
    if not _tbm.os.path.lexists(finalize_path):
        raise RuntimeError('completed_abandon_finalize_receipt_missing')
    finalize_receipt = _tbm._read_json_regular(finalize_path)
    rows = _tbm.load_abandoned_version_receipts(path=_tbm.Path(_tbm.RESULTS_DIR) / 'abandoned_versions.jsonl', project_root=_tbm.PROJECT_ROOT)
    abandon_receipt = _tbm.validate_abandon_ledger_history(claim, rows, require_active_head=True)
    _tbm.validate_abandon_finalize_receipt(claim, finalize_receipt, rows)
    from evolution_core import PIPELINE_STATE_FILE
    live_claim = _tbm.Path(_tbm.RESULTS_DIR) / 'policy_epoch_reconciliation_claim.json'
    if _tbm.os.path.lexists(PIPELINE_STATE_FILE) or _tbm.os.path.lexists(live_claim):
        raise RuntimeError('completed_abandon_terminal_paths_still_live')
    expected_result = {'abandoned': True, 'cleared_checkpoint': True, 'workflow_run_id': baseline_identity['workflow_run_id'], 'abandon_transaction_id': transaction_id, 'abandon_receipt_digest': abandon_receipt.get('receipt_digest'), 'finalize_receipt_digest': finalize_receipt.get('receipt_digest'), 'abandon_checkpoint_identity': terminal_identity, 'first_strict_execution_fence': claim.get('first_strict_execution_fence') if claim.get('schema_version') == 3 else result.get('first_strict_execution_fence')}
    for field, value in expected_result.items():
        if result.get(field) != value:
            raise RuntimeError(f'completed_abandon_result_{field}_mismatch')
    return {'transaction_id': transaction_id, 'abandon_receipt_digest': abandon_receipt['receipt_digest'], 'finalize_receipt_digest': finalize_receipt['receipt_digest'], 'checkpoint_identity': terminal_identity, 'workflow_fences': workflow_fences, 'first_strict_execution_fence': claim.get('first_strict_execution_fence')}


def _historical_head_is_ancestor(ancestor_head: str, descendant_head: str) -> bool:
    """Return whether a recorded commit is in the checked-out main lineage."""
    if not ancestor_head or not descendant_head:
        return False
    try:
        proc = _tbm.subprocess.run(['git', 'merge-base', '--is-ancestor', ancestor_head, descendant_head], cwd=str(_tbm.PROJECT_ROOT), capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    return proc.returncode == 0


def _historical_completed_abandon_source_proof(claim: dict) -> dict:
    """Bind a finalized abandon to a clean, fetched descendant of its main.

    A completed terminal transaction has no live checkpoint to replay.  The
    only safe source transition is therefore from the exact commit recorded in
    the immutable claim to the current, fetched ``origin/main`` descendant.
    This is deliberately narrower than normal active-checkpoint head drift:
    it proves historical termination only and cannot resume the cleared plan.
    """
    recorded_head = str((claim.get('git_state') or {}).get('head') or '')
    remote_main_ref = f'refs/remotes/origin/{_tbm.EVOLUTION_BRANCH}'
    try:
        resolved_recorded = _tbm._evolution_git('rev-parse', f'{recorded_head}^{{commit}}')
        current_head = _tbm._evolution_git('rev-parse', 'HEAD')
        remote_main_head = _tbm._evolution_git('rev-parse', remote_main_ref)
        current_branch = _tbm._evolution_git('branch', '--show-current')
        tracked_status = _tbm._evolution_git('status', '--porcelain', '--untracked-files=no')
    except Exception as exc:
        raise RuntimeError('historical_completed_abandon_git_identity_unavailable') from exc
    if resolved_recorded != recorded_head:
        raise RuntimeError('historical_completed_abandon_recorded_head_invalid')
    if current_branch != _tbm.EVOLUTION_BRANCH:
        raise RuntimeError('historical_completed_abandon_not_on_main')
    if current_head != remote_main_head:
        raise RuntimeError('historical_completed_abandon_main_not_fetched')
    if tracked_status:
        raise RuntimeError('historical_completed_abandon_tracked_worktree_dirty')
    if not _tbm._historical_head_is_ancestor(recorded_head, current_head):
        raise RuntimeError('historical_completed_abandon_main_not_descendant')
    return {'recorded_git_head': recorded_head, 'current_git_head': current_head, 'remote_main_ref': remote_main_ref, 'remote_main_head': remote_main_head, 'source_descendant_verified': True}


def reprove_historical_completed_abandon(transaction_id: str) -> dict:
    """Read-only reproof for one finalized, checkpoint-free abandon.

    This recovery path intentionally has no ``checkpoint`` or provider-result
    argument.  It may consume only the existing schema-2 claim, finalized
    receipt, append-only ledger, and fenced workflow journals.  It refuses a
    live/unfinalized transaction, a later ledger head, a resurrected candidate,
    or a source checkout that is not a clean fetched descendant of the exact
    recorded main commit.  It never clears, rewrites, or synthesizes runtime
    state; a caller may use the returned proof solely to authorize a fresh
    post-terminal prepare on current main.
    """
    if not _tbm._is_autonomous_runtime_checkout():
        raise RuntimeError('historical_completed_abandon_requires_autonomous_runtime_checkout')
    transaction_id = str(transaction_id or '')
    if len(transaction_id) != 64 or any((char not in '0123456789abcdef' for char in transaction_id)):
        raise RuntimeError('historical_completed_abandon_transaction_id_invalid')
    transaction_dir = _tbm.Path(_tbm.RESULTS_DIR) / 'policy_epoch_abandon_transactions' / transaction_id
    _tbm._assert_safe_existing_transaction_chain(transaction_dir)
    claim = _tbm._read_json_regular(transaction_dir / 'claim.json')
    _tbm.validate_abandon_claim_structure(claim)
    _tbm._validate_claim_first_strict_execution_fence(claim)
    _tbm._validate_external_bootstrap_contract_abandon_proof(claim)
    if claim.get('transaction_id') != transaction_id:
        raise RuntimeError('historical_completed_abandon_transaction_identity_mismatch')
    from evolution_core import PIPELINE_STATE_FILE
    live_claim = _tbm.Path(_tbm.RESULTS_DIR) / 'policy_epoch_reconciliation_claim.json'
    if _tbm.os.path.lexists(PIPELINE_STATE_FILE) or _tbm.os.path.lexists(live_claim):
        raise RuntimeError('historical_completed_abandon_terminal_paths_live')
    finalize_path = transaction_dir / 'receipt.json'
    if not _tbm.os.path.lexists(finalize_path):
        raise RuntimeError('historical_completed_abandon_finalize_receipt_missing')
    finalize_receipt = _tbm._read_json_regular(finalize_path)
    rows = _tbm.load_abandoned_version_receipts(path=_tbm.Path(_tbm.RESULTS_DIR) / 'abandoned_versions.jsonl', project_root=_tbm.PROJECT_ROOT)
    abandon_receipt = _tbm.validate_abandon_ledger_history(claim, rows, require_active_head=True)
    if abandon_receipt is None:
        raise RuntimeError('historical_completed_abandon_receipt_missing')
    _tbm.validate_abandon_finalize_receipt(claim, finalize_receipt, rows)
    version = int(claim['checkpoint']['next_v'])
    candidate = _tbm.Path(_tbm.get_bot_dir(version))
    _transaction_dir, quarantine = _tbm._claim_transaction_paths(claim)
    candidate_state = _tbm._validate_claim_candidate_state(claim, candidate, quarantine)
    expected_candidate_state = 'quarantine' if claim['candidate']['present'] else 'absent'
    if candidate_state != expected_candidate_state:
        raise RuntimeError('historical_completed_abandon_candidate_not_finalized')
    if _tbm.git_dir_is_committed(version) or _tbm.git_has_publication_ref(version):
        raise RuntimeError('historical_completed_abandon_candidate_published')
    source = _tbm._historical_completed_abandon_source_proof(claim)
    workflow_fences = _tbm._validate_completed_abandon_workflow_fences(claim)
    return {'kind': 'national-policy-historical-completed-abandon-reproof-v1', 'authority': 'completed_abandon_terminal_evidence_only', 'prepare_authorized': False, 'next_tool': None, 'transaction_id': transaction_id, 'checkpoint_identity': dict(claim['checkpoint']), 'abandon_receipt_digest': abandon_receipt['receipt_digest'], 'finalize_receipt_digest': finalize_receipt['receipt_digest'], 'workflow_fences': workflow_fences, 'first_strict_execution_fence': claim.get('first_strict_execution_fence'), 'source': source}
