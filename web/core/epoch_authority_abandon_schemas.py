"""Companion module for epoch_authority: schema-2/3 abandon claim & receipt validators.

Every intra-companion call routes through ``epoch_authority as _ea`` so moved
symbols stay single-dispatch even when invoked from the main module.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import epoch_authority as _ea


def _validate_schema3_first_strict_fence(fence: Any) -> dict[str, Any]:
    required = {'present', 'abandoned', 'scope', 'terminal_receipt', 'proof_digest'}
    if not isinstance(fence, dict) or set(fence) != required or fence.get('present') is not True or (fence.get('abandoned') is not True):
        raise RuntimeError('recorded_abandon_first_strict_fence_invalid')
    scope = fence.get('scope')
    scope_fields = {'workflow_run_id', 'checkpoint_revision', 'candidate_version', 'candidate_label', 'candidate_artifact_hash', 'control_id', 'control_artifact_hash', 'control_receipt_digest', 'precommit_plan_digest', 'evaluation_contract_digest', 'native_match_timing_plan_digest', 'precommit_attempt'}
    if not isinstance(scope, dict) or set(scope) != scope_fields:
        raise RuntimeError('recorded_abandon_first_strict_scope_invalid')
    if not isinstance(scope.get('workflow_run_id'), str) or not scope.get('workflow_run_id') or type(scope.get('checkpoint_revision')) is not int or (int(scope['checkpoint_revision']) < 1) or (type(scope.get('candidate_version')) is not int) or (int(scope['candidate_version']) < _ea.FIRST_STRICT_POLICY_VERSION) or (type(scope.get('precommit_attempt')) is not int) or (int(scope['precommit_attempt']) < 1) or (not isinstance(scope.get('candidate_label'), str)) or (not scope.get('candidate_label')) or (not isinstance(scope.get('control_id'), str)) or (not scope.get('control_id')) or any((not _ea._is_hex_digest(scope.get(field)) for field in ('candidate_artifact_hash', 'control_artifact_hash', 'control_receipt_digest', 'precommit_plan_digest', 'evaluation_contract_digest', 'native_match_timing_plan_digest'))):
        raise RuntimeError('recorded_abandon_first_strict_scope_invalid')
    scope_digest = _ea._canonical_object_digest(scope)
    receipt = fence.get('terminal_receipt')
    receipt_fields = {'schema_version', 'kind', 'outcome', 'authority_run_id', 'scope_digest', 'terminal_event_seq', 'terminal_event_payload_digest', 'stream_version', 'fence_epoch', 'effects', 'receipt_digest'}
    unsigned_receipt = {key: value for key, value in receipt.items() if key != 'receipt_digest'} if isinstance(receipt, dict) else {}
    if not isinstance(receipt, dict) or set(receipt) != receipt_fields or receipt.get('schema_version') != 1 or (receipt.get('kind') != 'first-strict-control-execution-terminal-receipt') or (receipt.get('outcome') != 'abandoned') or (receipt.get('scope_digest') != scope_digest) or (receipt.get('authority_run_id') != f'first-strict-control:{scope_digest}') or (type(receipt.get('terminal_event_seq')) is not int) or (int(receipt['terminal_event_seq']) < 1) or (not _ea._is_hex_digest(receipt.get('terminal_event_payload_digest'))) or (type(receipt.get('stream_version')) is not int) or (int(receipt['stream_version']) < int(receipt['terminal_event_seq'])) or (type(receipt.get('fence_epoch')) is not int) or (int(receipt['fence_epoch']) < 1) or (not isinstance(receipt.get('effects'), dict)) or (set(receipt['effects']) != {'completed', 'abandoned', 'exhausted', 'nonterminal'}) or any((not isinstance(receipt['effects'].get(key), list) or len(receipt['effects'].get(key)) > 8 for key in receipt['effects'])) or (receipt['effects'].get('nonterminal') != []) or (not _ea._is_hex_digest(receipt.get('receipt_digest'))) or (receipt.get('receipt_digest') != _ea._canonical_object_digest(unsigned_receipt)):
        raise RuntimeError('recorded_abandon_first_strict_receipt_invalid')
    expected_proof_digest = _ea._canonical_object_digest({'scope': scope, 'terminal_receipt': receipt})
    if fence.get('proof_digest') != expected_proof_digest:
        raise RuntimeError('recorded_abandon_first_strict_proof_invalid')
    return fence


def validate_schema2_abandon_claim_structure(claim: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact self-contained schema-2 abandon claim envelope.

    A digest alone is not authenticity: an operator can accidentally (or an
    attacker can deliberately) re-sign an altered object.  This validator
    therefore reconstructs every canonical identity and rejects all unknown,
    omitted, unbounded, or path-bearing variants before callers inspect live
    filesystem state.
    """
    if not isinstance(claim, dict) or set(claim) != _ea._SCHEMA2_ABANDON_CLAIM_KEYS:
        raise RuntimeError('recorded_abandon_claim_fields_invalid')
    unsigned = {key: value for key, value in claim.items() if key != 'claim_digest'}
    if claim.get('schema_version') != 2 or claim.get('kind') != 'national-policy-recorded-abandon-finalize-claim' or claim.get('evaluation_epoch') != _ea.EVALUATION_EPOCH or (claim.get('checkout_role') != 'autonomous_evolution_runtime') or (not _ea._is_hex_digest(claim.get('claim_digest'))) or (claim.get('claim_digest') != _ea._claim_payload_digest(unsigned)):
        raise RuntimeError('recorded_abandon_claim_envelope_invalid')
    checkpoint = claim.get('checkpoint')
    if not isinstance(checkpoint, dict) or set(checkpoint) != _ea._ABANDON_CHECKPOINT_KEYS:
        raise RuntimeError('recorded_abandon_checkpoint_identity_invalid')
    next_v = checkpoint.get('next_v')
    source_v = checkpoint.get('source_v')
    revision = checkpoint.get('checkpoint_revision')
    stage = checkpoint.get('stage')
    workflow_run_id = checkpoint.get('workflow_run_id')
    if not _ea._is_hex_digest(checkpoint.get('digest')) or type(next_v) is not int or next_v < _ea.FIRST_STRICT_POLICY_VERSION or (type(source_v) is not int) or (source_v < 0) or (type(revision) is not int) or (revision < 1) or (not isinstance(stage, str)) or (not stage) or (len(stage.encode('utf-8')) > 256) or (not isinstance(workflow_run_id, str)) or (not workflow_run_id) or (len(workflow_run_id.encode('utf-8')) > 1024):
        raise RuntimeError('recorded_abandon_checkpoint_identity_invalid')
    reason = claim.get('abandon_reason')
    if not isinstance(reason, str) or not reason or reason != reason.strip() or (len(reason.encode('utf-8')) > 4 * 1024):
        raise RuntimeError('recorded_abandon_reason_invalid')
    candidate = claim.get('candidate')
    if not isinstance(candidate, dict) or set(candidate) != _ea._ABANDON_CANDIDATE_KEYS:
        raise RuntimeError('recorded_abandon_candidate_identity_invalid')
    present = candidate.get('present')
    entry_count = candidate.get('entry_count')
    total_bytes = candidate.get('total_bytes')
    if type(present) is not bool or candidate.get('path') != f'bots/{_ea.bot_name(next_v)}' or type(entry_count) is not int or (not 0 <= entry_count <= _ea._ABANDON_CLAIM_MAX_TREE_ENTRIES) or (type(total_bytes) is not int) or (not 0 <= total_bytes <= _ea._ABANDON_CLAIM_MAX_TREE_BYTES):
        raise RuntimeError('recorded_abandon_candidate_identity_invalid')
    if present:
        if not _ea._is_hex_digest(candidate.get('manifest_digest')):
            raise RuntimeError('recorded_abandon_candidate_manifest_invalid')
    elif candidate.get('manifest_digest') is not None or entry_count != 0 or total_bytes != 0:
        raise RuntimeError('recorded_abandon_absent_candidate_invalid')
    quarantine = claim.get('quarantine')
    if not isinstance(quarantine, dict) or set(quarantine) != _ea._ABANDON_QUARANTINE_KEYS or quarantine != _ea.schema2_abandon_quarantine_contract():
        raise RuntimeError('recorded_abandon_quarantine_contract_invalid')
    git_state = claim.get('git_state')
    if not isinstance(git_state, dict) or set(git_state) != _ea._ABANDON_GIT_STATE_KEYS:
        raise RuntimeError('recorded_abandon_git_state_invalid')
    expected_refs = {_ea.bot_tag(next_v): False, _ea.high_water_tag(next_v): False}
    if not _ea._is_hex_digest(git_state.get('head'), lengths=(40, 64)) or claim.get('git_head') != git_state.get('head') or git_state.get('tracked_worktree_clean') is not True or (git_state.get('candidate_tracked') is not False) or (git_state.get('publication_refs') != expected_refs):
        raise RuntimeError('recorded_abandon_git_state_invalid')
    ledger = claim.get('ledger')
    if not isinstance(ledger, dict) or set(ledger) != _ea._ABANDON_LEDGER_KEYS:
        raise RuntimeError('recorded_abandon_ledger_binding_invalid')
    prior_count = ledger.get('prior_receipt_count')
    prior_head = ledger.get('prior_receipt_head_digest')
    receipt_identity = ledger.get('receipt_identity')
    if ledger.get('path_contract') != _ea._ABANDON_LEDGER_PATH_CONTRACT or type(prior_count) is not int or (not 0 <= prior_count <= 1000000) or (prior_head is not None and (not _ea._is_hex_digest(prior_head))) or ((prior_count == 0) != (prior_head is None)) or (not isinstance(receipt_identity, dict)) or (set(receipt_identity) != _ea._ABANDON_RECEIPT_IDENTITY_KEYS) or (receipt_identity != _ea.schema2_abandon_receipt_identity(checkpoint, reason)):
        raise RuntimeError('recorded_abandon_ledger_binding_invalid')
    expected_transaction_id = _ea._canonical_object_digest(_ea.schema2_abandon_transaction_preimage(claim))
    if not _ea._is_hex_digest(claim.get('transaction_id')) or claim.get('transaction_id') != expected_transaction_id:
        raise RuntimeError('recorded_abandon_transaction_id_invalid')
    return claim


def validate_schema3_abandon_claim_structure(claim: dict[str, Any]) -> dict[str, Any]:
    """Validate a schema-3 claim while retaining schema-2 compatibility."""
    if not isinstance(claim, dict) or set(claim) != _ea._SCHEMA3_ABANDON_CLAIM_KEYS:
        raise RuntimeError('recorded_abandon_claim_fields_invalid')
    unsigned = {key: value for key, value in claim.items() if key != 'claim_digest'}
    if claim.get('schema_version') != 3 or claim.get('kind') != 'national-policy-recorded-abandon-finalize-claim' or claim.get('evaluation_epoch') != _ea.EVALUATION_EPOCH or (claim.get('checkout_role') != 'autonomous_evolution_runtime') or (not _ea._is_hex_digest(claim.get('claim_digest'))) or (claim.get('claim_digest') != _ea._claim_payload_digest(unsigned)):
        raise RuntimeError('recorded_abandon_claim_envelope_invalid')
    _ea._validate_schema3_first_strict_fence(claim.get('first_strict_execution_fence'))
    fence = claim['first_strict_execution_fence']
    scope = fence['scope']
    checkpoint = claim.get('checkpoint') or {}
    if scope.get('workflow_run_id') != checkpoint.get('workflow_run_id') or scope.get('candidate_version') != checkpoint.get('next_v') or scope.get('candidate_label') != f'{_ea.ACTIVE_BOT_PREFIX}{checkpoint.get('next_v')}' or (int(scope.get('checkpoint_revision') or 0) > int(checkpoint.get('checkpoint_revision') or 0)):
        raise RuntimeError('recorded_abandon_first_strict_checkpoint_mismatch')
    base = {key: value for key, value in claim.items() if key not in {'first_strict_execution_fence', 'claim_digest'}}
    base['schema_version'] = 2
    base['transaction_id'] = _ea._canonical_object_digest(_ea.schema2_abandon_transaction_preimage(base))
    base['claim_digest'] = _ea._claim_payload_digest(base)
    _ea.validate_schema2_abandon_claim_structure(base)
    expected_transaction_id = _ea._canonical_object_digest(_ea.schema3_abandon_transaction_preimage(claim))
    if claim.get('transaction_id') != expected_transaction_id:
        raise RuntimeError('recorded_abandon_transaction_id_invalid')
    return claim


def validate_abandon_claim_structure(claim: dict[str, Any]) -> dict[str, Any]:
    if isinstance(claim, dict) and claim.get('schema_version') == 3:
        return _ea.validate_schema3_abandon_claim_structure(claim)
    return _ea.validate_schema2_abandon_claim_structure(claim)


def validate_schema2_abandon_ledger_history(claim: dict[str, Any], rows: list[dict[str, Any]], *, require_active_head: bool) -> dict[str, Any] | None:
    """Cross-bind a claim to its exact prior chain and six-field receipt."""
    _ea.validate_schema2_abandon_claim_structure(claim)
    if not isinstance(rows, list):
        raise RuntimeError('recorded_abandon_ledger_history_invalid')
    ledger = claim['ledger']
    prior_count = ledger['prior_receipt_count']
    prior_head = ledger['prior_receipt_head_digest']
    if len(rows) < prior_count:
        raise RuntimeError('recorded_abandon_ledger_prefix_missing')
    observed_prior_head = rows[prior_count - 1].get('receipt_digest') if prior_count else None
    if observed_prior_head != prior_head:
        raise RuntimeError('recorded_abandon_ledger_prefix_changed')
    identity = ledger['receipt_identity']
    matches = [row for row in rows if all((row.get(key) == value for key, value in identity.items()))]
    if len(matches) > 1:
        raise RuntimeError('recorded_abandon_receipt_not_unique')
    receipt = matches[0] if matches else None
    if receipt is not None:
        try:
            receipt_index = next((index for index, row in enumerate(rows) if row is receipt))
        except StopIteration as exc:
            raise RuntimeError('recorded_abandon_receipt_history_invalid') from exc
        if receipt_index != prior_count or receipt.get('previous_receipt_digest') != prior_head:
            raise RuntimeError('recorded_abandon_receipt_history_invalid')
    if require_active_head:
        expected_count = prior_count + (1 if receipt is not None else 0)
        if len(rows) != expected_count:
            raise RuntimeError('recorded_abandon_active_ledger_advanced')
    return receipt


def _schema3_common_schema2_claim(claim: dict[str, Any]) -> dict[str, Any]:
    base = {key: value for key, value in claim.items() if key not in {'first_strict_execution_fence', 'claim_digest'}}
    base['schema_version'] = 2
    base['transaction_id'] = _ea._canonical_object_digest(_ea.schema2_abandon_transaction_preimage(base))
    base['claim_digest'] = _ea._claim_payload_digest(base)
    return base


def validate_schema2_abandon_finalize_receipt(claim: dict[str, Any], receipt: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate a completed transaction without pinning today's Git HEAD.

    Historical completion remains valid after later code commits and later
    legitimate abandon rows.  Its immutable claim, exact ledger prefix/row and
    finalize receipt stay bound; no live filesystem bytes are adopted.
    """
    _ea.validate_schema2_abandon_claim_structure(claim)
    abandon_receipt = _ea.validate_abandon_ledger_history(claim, rows, require_active_head=False)
    required = {'schema_version', 'kind', 'evaluation_epoch', 'mode', 'claim_digest', 'workflow_run_id', 'abandon_receipt_digest', 'checkpoint_cleared', 'candidate_state', 'candidate_manifest_digest', 'receipt_digest'}
    unsigned = {key: value for key, value in receipt.items() if key != 'receipt_digest'} if isinstance(receipt, dict) else {}
    expected_state = 'quarantine' if claim['candidate']['present'] else 'absent'
    if not isinstance(receipt, dict) or set(receipt) != required or receipt.get('schema_version') != 2 or (receipt.get('kind') != 'national-policy-recorded-abandon-finalize') or (receipt.get('evaluation_epoch') != _ea.EVALUATION_EPOCH) or (receipt.get('mode') != 'execute') or (receipt.get('claim_digest') != claim.get('claim_digest')) or (receipt.get('workflow_run_id') != claim['checkpoint']['workflow_run_id']) or (abandon_receipt is None) or (receipt.get('abandon_receipt_digest') != abandon_receipt.get('receipt_digest')) or (receipt.get('checkpoint_cleared') is not True) or (receipt.get('candidate_state') != expected_state) or (receipt.get('candidate_manifest_digest') != claim['candidate']['manifest_digest']) or (not _ea._is_hex_digest(receipt.get('receipt_digest'))) or (receipt.get('receipt_digest') != _ea._canonical_object_digest(unsigned)):
        raise RuntimeError('recorded_abandon_finalize_receipt_invalid')
    return receipt


def validate_schema3_abandon_finalize_receipt(claim: dict[str, Any], receipt: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    _ea.validate_schema3_abandon_claim_structure(claim)
    abandon_receipt = _ea.validate_abandon_ledger_history(claim, rows, require_active_head=False)
    required = {'schema_version', 'kind', 'evaluation_epoch', 'mode', 'claim_digest', 'workflow_run_id', 'abandon_receipt_digest', 'checkpoint_cleared', 'candidate_state', 'candidate_manifest_digest', 'first_strict_execution_fence_digest', 'receipt_digest'}
    unsigned = {key: value for key, value in receipt.items() if key != 'receipt_digest'} if isinstance(receipt, dict) else {}
    expected_state = 'quarantine' if claim['candidate']['present'] else 'absent'
    if not isinstance(receipt, dict) or set(receipt) != required or receipt.get('schema_version') != 3 or (receipt.get('kind') != 'national-policy-recorded-abandon-finalize') or (receipt.get('evaluation_epoch') != _ea.EVALUATION_EPOCH) or (receipt.get('mode') != 'execute') or (receipt.get('claim_digest') != claim.get('claim_digest')) or (receipt.get('workflow_run_id') != claim['checkpoint']['workflow_run_id']) or (abandon_receipt is None) or (receipt.get('abandon_receipt_digest') != abandon_receipt.get('receipt_digest')) or (receipt.get('checkpoint_cleared') is not True) or (receipt.get('candidate_state') != expected_state) or (receipt.get('candidate_manifest_digest') != claim['candidate']['manifest_digest']) or (receipt.get('first_strict_execution_fence_digest') != claim['first_strict_execution_fence']['proof_digest']) or (not _ea._is_hex_digest(receipt.get('receipt_digest'))) or (receipt.get('receipt_digest') != _ea._canonical_object_digest(unsigned)):
        raise RuntimeError('recorded_abandon_finalize_receipt_invalid')
    return receipt


def validate_abandon_finalize_receipt(claim: dict[str, Any], receipt: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(claim, dict) and claim.get('schema_version') == 3:
        return _ea.validate_schema3_abandon_finalize_receipt(claim, receipt, rows)
    return _ea.validate_schema2_abandon_finalize_receipt(claim, receipt, rows)


def _read_bounded_regular_json(path: Path, *, limit: int=1024 * 1024) -> dict:
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, limit + 1)
        opened_after = os.fstat(descriptor)
        live = os.lstat(path)
        if len(raw) > limit or not stat.S_ISREG(opened.st_mode) or (not stat.S_ISREG(opened_after.st_mode)) or (not stat.S_ISREG(live.st_mode)) or (opened.st_nlink != 1) or (opened_after.st_nlink != 1) or (live.st_nlink != 1) or (opened.st_size != len(raw)) or ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (opened_after.st_dev, opened_after.st_ino, opened_after.st_size, opened_after.st_mtime_ns, opened_after.st_ctime_ns)) or ((opened_after.st_dev, opened_after.st_ino, opened_after.st_size, opened_after.st_mtime_ns, opened_after.st_ctime_ns) != (live.st_dev, live.st_ino, live.st_size, live.st_mtime_ns, live.st_ctime_ns)):
            raise RuntimeError('claim_json_unsafe')
        os.lseek(descriptor, 0, os.SEEK_SET)
        reread = os.read(descriptor, limit + 1)
        if reread != raw:
            raise RuntimeError('claim_json_unsafe')
    finally:
        os.close(descriptor)
    value = json.loads(raw.decode('utf-8'))
    if not isinstance(value, dict):
        raise RuntimeError('claim_json_not_object')
    return value


def _schema2_candidate_tree_identity(path: Path) -> dict[str, Any]:
    root = Path(path)
    root_stat = os.lstat(root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError('recorded_abandon_candidate_root_unsafe')
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for child in sorted(root.rglob('*'), key=lambda item: item.as_posix()):
        relative = child.relative_to(root).as_posix()
        if len(entries) >= _ea._ABANDON_CLAIM_MAX_TREE_ENTRIES or len(Path(relative).parts) > 32:
            raise RuntimeError('recorded_abandon_candidate_tree_limit')
        metadata = os.lstat(child)
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError('recorded_abandon_candidate_symlink')
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({'path': relative, 'kind': 'directory'})
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError('recorded_abandon_candidate_file_unsafe')
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(child, flags)
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > _ea._ABANDON_CLAIM_MAX_TREE_BYTES:
                    raise RuntimeError('recorded_abandon_candidate_tree_limit')
            opened_after = os.fstat(descriptor)
            live = os.lstat(child)
            raw = b''.join(chunks)
            if opened.st_nlink != 1 or opened_after.st_nlink != 1 or live.st_nlink != 1 or ((opened.st_dev, opened.st_ino) != (opened_after.st_dev, opened_after.st_ino)) or ((opened_after.st_dev, opened_after.st_ino) != (live.st_dev, live.st_ino)) or ((opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (opened_after.st_size, opened_after.st_mtime_ns, opened_after.st_ctime_ns)) or (opened.st_size != len(raw)):
                raise RuntimeError('recorded_abandon_candidate_changed_while_read')
        finally:
            os.close(descriptor)
        entries.append({'path': relative, 'kind': 'file', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    return {'manifest_digest': _ea._canonical_object_digest(entries), 'entry_count': len(entries), 'total_bytes': total_bytes}


def _assert_schema2_transaction_chain_safe(results_dir: Path, transaction_dir: Path) -> None:
    try:
        relative = transaction_dir.relative_to(results_dir)
    except ValueError as exc:
        raise RuntimeError('recorded_abandon_transaction_path_escape') from exc
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    flags |= getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(results_dir, flags)
    try:
        for part in relative.parts:
            if part in {'', '.', '..'}:
                raise RuntimeError('recorded_abandon_transaction_path_invalid')
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except OSError as exc:
        raise RuntimeError('recorded_abandon_transaction_path_unsafe') from exc
    finally:
        os.close(descriptor)


def _validate_schema2_active_claim_state(claim: dict[str, Any], *, results_dir: Path, bots_dir: Path, infra: Any) -> None:
    """Read-only live recovery validation used by the canonical epoch view."""
    _ea.validate_abandon_claim_structure(claim)
    from bootstrap_contract_recovery import validate_canonical_abandon_external_binding
    validate_canonical_abandon_external_binding(Path(infra.PROJECT_ROOT), claim)
    version = int(claim['checkpoint']['next_v'])
    current_git_state = {'head': infra._git('rev-parse', 'HEAD'), 'tracked_worktree_clean': infra._git('status', '--porcelain', '--untracked-files=no') == '', 'candidate_tracked': bool(infra.git_dir_is_committed(version)), 'publication_refs': {_ea.bot_tag(version): bool(infra.git_has_publication_ref(version)), _ea.high_water_tag(version): bool(infra.git_has_publication_ref(version))}}
    if current_git_state != claim['git_state']:
        raise RuntimeError('recorded_abandon_active_git_state_changed')
    if os.path.lexists(bots_dir) and (stat.S_ISLNK(os.lstat(bots_dir).st_mode) or not stat.S_ISDIR(os.lstat(bots_dir).st_mode)):
        raise RuntimeError('recorded_abandon_bots_root_unsafe')
    candidate = bots_dir / _ea.bot_name(version)
    transaction_dir = results_dir / 'policy_epoch_abandon_transactions' / claim['transaction_id']
    quarantine = transaction_dir / _ea._ABANDON_QUARANTINE_LEAF
    _ea._assert_schema2_transaction_chain_safe(results_dir, transaction_dir)
    transaction_claim = transaction_dir / 'claim.json'
    if not os.path.lexists(transaction_claim) or _ea._read_bounded_regular_json(transaction_claim) != claim:
        raise RuntimeError('recorded_abandon_transaction_claim_mismatch')
    source_exists = os.path.lexists(candidate)
    quarantine_exists = os.path.lexists(quarantine)
    if source_exists and quarantine_exists:
        raise RuntimeError('recorded_abandon_source_quarantine_xor_invalid')
    expected = claim['candidate']
    if expected['present'] is False:
        if source_exists or quarantine_exists:
            raise RuntimeError('recorded_abandon_unexpected_candidate')
        phase = 'absent'
    else:
        if not source_exists and (not quarantine_exists):
            raise RuntimeError('recorded_abandon_claimed_candidate_missing')
        observed = _ea._schema2_candidate_tree_identity(candidate if source_exists else quarantine)
        if any((observed[field] != expected[field] for field in ('manifest_digest', 'entry_count', 'total_bytes'))):
            raise RuntimeError('recorded_abandon_candidate_preimage_changed')
        phase = 'source' if source_exists else 'quarantine'
    rows = infra.load_abandoned_version_receipts(path=results_dir / 'abandoned_versions.jsonl', project_root=infra.PROJECT_ROOT)
    abandon_receipt = _ea.validate_abandon_ledger_history(claim, rows, require_active_head=True)
    # Abandon authority is definitionally PRIMARY-scoped: an abandon finalizes
    # the primary generation's checkpoint, never a concurrent draft/consumer
    # slot.  Read under no_slot_override() so an ambient draft override (e.g.
    # this validator reached via strict_epoch_projection under
    # active_slot_override("draft")) cannot make the primary checkpoint
    # observation disagree with the existence check or the abandon claim's
    # bound checkpoint digest.
    checkpoint_path = Path(infra.PIPELINE_STATE_FILE)
    with infra.no_slot_override():
        checkpoint_exists = os.path.lexists(checkpoint_path)
        checkpoint = infra.read_pipeline_checkpoint() if checkpoint_exists else None
    if checkpoint_exists:
        if not isinstance(checkpoint, dict) or _ea._canonical_object_digest(checkpoint) != claim['checkpoint']['digest']:
            raise RuntimeError('recorded_abandon_active_checkpoint_changed')
        if abandon_receipt is None and phase not in {'source', 'absent'}:
            raise RuntimeError('recorded_abandon_phase_invalid_before_ledger')
    else:
        if abandon_receipt is None:
            raise RuntimeError('recorded_abandon_receipt_missing_after_checkpoint_clear')
        if expected['present'] is True and phase != 'quarantine':
            raise RuntimeError('recorded_abandon_source_invalid_after_checkpoint_clear')
    finalize = transaction_dir / 'receipt.json'
    if os.path.lexists(finalize):
        _ea.validate_abandon_finalize_receipt(claim, _ea._read_bounded_regular_json(finalize), rows)
