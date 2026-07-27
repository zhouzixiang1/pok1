"""Companion for system_strict_bootstrap: blueprint materialization & proposal validation.

Every intra-companion call routes through the main module as ``_ssb`` so moved
symbols stay single-dispatch even when invoked from the main module.
"""

from __future__ import annotations

from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
)

import system_strict_bootstrap as _ssb


def _runtime_core(policy_bytes: bytes) -> dict[str, bytes]:
    from national_native import NATIVE_BOT_TEMPLATE, NATIVE_PRECOMPUTE_TEMPLATE
    return {'national_bot.py': NATIVE_BOT_TEMPLATE.encode('utf-8'), 'policy.py': policy_bytes, 'precompute.py': NATIVE_PRECOMPUTE_TEMPLATE.encode('utf-8')}


def _materialized_payload(policy_bytes: bytes, *, version: int=FIRST_STRICT_POLICY_VERSION) -> dict[str, bytes]:
    from bot_namespace import NATIONAL_RUNTIME_MANIFEST, POLICY_EPOCH_RECEIPT, build_policy_epoch_receipt, build_runtime_manifest
    core = _ssb._runtime_core(policy_bytes)
    with _ssb.tempfile.TemporaryDirectory(prefix='pok-policy-identity-') as temporary:
        root = _ssb.Path(temporary)
        for relative, payload in core.items():
            (root / relative).write_bytes(payload)
        runtime_manifest = build_runtime_manifest(root)
        (root / NATIONAL_RUNTIME_MANIFEST).write_bytes(_ssb._json_bytes(runtime_manifest))
        epoch_receipt = build_policy_epoch_receipt(root, version, parent_versions=())
    return {**core, NATIONAL_RUNTIME_MANIFEST: _ssb._json_bytes(runtime_manifest), POLICY_EPOCH_RECEIPT: _ssb._json_bytes(epoch_receipt)}


def _write_payload(root: _ssb.Path, payload: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=False)
    for relative, content in sorted(payload.items()):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _payload_artifact_hash(payload: dict[str, bytes]) -> str:
    from bot_artifact import hash_path
    with _ssb.tempfile.TemporaryDirectory(prefix='pok-policy-artifact-') as temporary:
        root = _ssb.Path(temporary) / 'artifact'
        _ssb._write_payload(root, payload)
        return hash_path(root)


def materialize_fresh_candidate(candidate_dir: str | _ssb.Path, *, version: int=FIRST_STRICT_POLICY_VERSION, final_policy: bool=False) -> dict[str, _ssb.Any]:
    """Atomically create a fresh five-file policy artifact."""
    from bot_namespace import FIRST_STRICT_POLICY_VERSION
    if int(version) != _ssb.FIRST_STRICT_POLICY_VERSION:
        raise _ssb.SystemStrictBootstrapError(['fresh_bootstrap_target_version_mismatch'])
    candidate = _ssb.Path(candidate_dir)
    if candidate.exists() or candidate.is_symlink():
        raise _ssb.SystemStrictBootstrapError(['fresh_bootstrap_target_must_not_exist'])
    policy_name = 'policy.py' if final_policy else 'prepared_policy.py'
    policy_bytes = (_ssb.BLUEPRINT_DIR / policy_name).read_bytes()
    payload = _ssb._materialized_payload(policy_bytes, version=version)
    staging = candidate.with_name(f'.{candidate.name}.fresh-{_ssb.os.getpid()}-{_ssb.secrets.token_hex(4)}')
    try:
        _ssb._write_payload(staging, payload)
        _ssb.os.replace(staging, candidate)
    finally:
        if staging.exists():
            _ssb.shutil.rmtree(staging, ignore_errors=True)
    return {'artifact_hash': _ssb._payload_artifact_hash(payload), 'files': {relative: _ssb._sha256_bytes(content) for relative, content in payload.items()}, 'source_artifact_inherited': False, 'policy': policy_name}


def refresh_policy_identity(bot_dir: str | _ssb.Path, *, version: int, parent_versions: _ssb.Iterable[int]=()) -> dict[str, _ssb.Any]:
    """Regenerate system-owned manifests after an authorized policy edit."""
    from bot_namespace import refresh_policy_identity_documents
    try:
        return refresh_policy_identity_documents(bot_dir, int(version), parent_versions=tuple(map(int, parent_versions)))
    except Exception as exc:
        raise _ssb.SystemStrictBootstrapError([f'system_bootstrap_identity_refresh_failed:{type(exc).__name__}:{str(exc)[:300]}']) from exc


def _python_source_digest(root: _ssb.Path) -> str:
    digest = _ssb.hashlib.sha256()
    for path in sorted(root.rglob('*.py')):
        if not _ssb._regular_file(path) or '__pycache__' in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b'\x00')
        digest.update(path.read_bytes())
        digest.update(b'\x00')
    return digest.hexdigest()


def _symbol_graph(root: _ssb.Path) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(root.rglob('*.py')):
        if not _ssb._regular_file(path) or '__pycache__' in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        tree = _ssb.ast.parse(path.read_bytes(), filename=relative)
        for node in tree.body:
            if not isinstance(node, (_ssb.ast.FunctionDef, _ssb.ast.AsyncFunctionDef, _ssb.ast.ClassDef)):
                continue
            calls: set[str] = set()
            for child in _ssb.ast.walk(node):
                if isinstance(child, _ssb.ast.Call):
                    if isinstance(child.func, _ssb.ast.Name):
                        calls.add(child.func.id)
                    elif isinstance(child.func, _ssb.ast.Attribute):
                        calls.add(child.func.attr)
            graph[f'{relative}:{node.name}'] = calls
    return graph


def _prepared_graph() -> tuple[dict[str, set[str]], str, list[str]]:
    try:
        with _ssb.tempfile.TemporaryDirectory(prefix='pok-policy-prepared-') as temporary:
            root = _ssb.Path(temporary) / 'national_v143'
            _ssb._write_payload(root, _ssb._materialized_payload((_ssb.BLUEPRINT_DIR / 'prepared_policy.py').read_bytes()))
            return (_ssb._symbol_graph(root), _ssb._python_source_digest(root), [])
    except Exception as exc:
        return ({}, '', [f'system_bootstrap_prepared_graph_error:{type(exc).__name__}'])


def _chain_errors(graph: dict[str, set[str]], chain: _ssb.Any) -> list[str]:
    if not isinstance(chain, list) or not chain:
        return ['system_bootstrap_selected_chain_missing']
    symbols = list(map(str, chain))
    if any((symbol not in graph for symbol in symbols)):
        return ['system_bootstrap_selected_chain_symbol_missing']
    leaf_map: dict[str, list[str]] = {}
    for symbol in graph:
        leaf_map.setdefault(symbol.rsplit(':', 1)[-1].rsplit('.', 1)[-1], []).append(symbol)
    for caller, callee in zip(symbols, symbols[1:]):
        leaf = callee.rsplit(':', 1)[-1].rsplit('.', 1)[-1]
        if leaf not in graph.get(caller, set()) or leaf_map.get(leaf) != [callee]:
            return ['system_bootstrap_selected_chain_unreachable']
    return []


def validate_selected_proposal_for_blueprint(plan: _ssb.Any, *, manifest: dict[str, _ssb.Any] | None=None, prepared_baseline_dir: str | _ssb.Path | None=None) -> list[str]:
    manifest = manifest or _ssb.load_blueprint_manifest()
    if not isinstance(plan, dict):
        return ['system_bootstrap_master_plan_not_object']
    errors: list[str] = []
    selected_id = str(plan.get('selected_proposal_id') or '')
    binding = plan.get('proposal_binding')
    ensemble = plan.get('proposal_ensemble')
    if not selected_id:
        errors.append('system_bootstrap_selected_proposal_id_missing')
    if not isinstance(binding, dict):
        return [*errors, 'system_bootstrap_proposal_binding_missing']
    if binding.get('selected_proposal_id') != selected_id:
        errors.append('system_bootstrap_selected_proposal_binding_mismatch')
    if not isinstance(ensemble, dict):
        errors.append('system_bootstrap_proposal_ensemble_missing')
        ensemble = {}
    proposals = ensemble.get('ordered_proposals')
    proposal_ids = [str(item.get('proposal_id') or '') for item in proposals or [] if isinstance(item, dict)]
    allowed_ids = [str(item) for item in ensemble.get('allowed_proposal_ids') or []]
    if ensemble.get('valid') is not True or ensemble.get('proposal_count') != 3 or len(proposal_ids) != 3 or (len(set(proposal_ids)) != 3) or (selected_id not in proposal_ids) or (not allowed_ids) or (len(set(allowed_ids)) != len(allowed_ids)) or (not set(allowed_ids).issubset(set(proposal_ids))) or (selected_id not in allowed_ids):
        errors.append('system_bootstrap_three_proposal_ensemble_invalid')
    reviews = ensemble.get('critic_reviews')
    if ensemble.get('valid_critic_count') != 2 or not isinstance(reviews, list) or len(reviews) != 2:
        errors.append('system_bootstrap_two_critic_ballots_missing')
    packet_digest = _ssb._sha256_bytes(_ssb.json.dumps(ensemble, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())
    if binding.get('proposal_packet_digest') != packet_digest:
        errors.append('system_bootstrap_proposal_packet_digest_mismatch')
    selected = next((item for item in proposals or [] if isinstance(item, dict) and item.get('proposal_id') == selected_id), None)
    expected_selected = {key: value for key, value in selected.items() if key != 'direction'} if isinstance(selected, dict) else None
    if binding.get('selected_proposal') != expected_selected:
        errors.append('system_bootstrap_selected_proposal_packet_mismatch')
    files = set(map(str, binding.get('target_files') or []))
    if files != {'policy.py'}:
        errors.append('system_bootstrap_proposal_target_must_be_policy_only')
    falsifier = binding.get('falsifier') or {}
    test_name = str(falsifier.get('test_name') or '')
    if test_name not in set(manifest.get('allowed_falsifiers') or []):
        errors.append('system_bootstrap_selected_falsifier_not_blueprint_capability')
    try:
        from agent_master import _selected_proposal_binding
        expected_binding = _selected_proposal_binding(selected, ensemble)
    except Exception as exc:
        errors.append(f'system_bootstrap_proposal_binding_projection_error:{type(exc).__name__}:{str(exc)[:300]}')
        expected_binding = None
    if isinstance(expected_binding, dict):
        if binding != expected_binding:
            errors.append('system_bootstrap_proposal_contract_packet_mismatch')
        expected_contract = expected_binding.get('contract_digest')
        if binding.get('contract_digest') != expected_contract:
            errors.append('system_bootstrap_proposal_contract_digest_mismatch')
    else:
        expected_contract = ''
    if prepared_baseline_dir is None:
        graph, source_digest, graph_errors = _ssb._prepared_graph()
    else:
        root = _ssb.Path(prepared_baseline_dir)
        graph, source_digest, graph_errors = (_ssb._symbol_graph(root), _ssb._python_source_digest(root), [])
    errors.extend(graph_errors)
    if binding.get('source_code_digest') != source_digest:
        errors.append('system_bootstrap_prepared_source_code_digest_mismatch')
    source_symbols = list(map(str, binding.get('source_symbols') or []))
    if not source_symbols or any((symbol not in graph for symbol in source_symbols)):
        errors.append('system_bootstrap_selected_source_symbol_missing')
    errors.extend(_ssb._chain_errors(graph, binding.get('reachable_chain')))
    tasks = plan.get('tasks')
    if not isinstance(tasks, list) or len(tasks) != 1 or (not isinstance(tasks[0], dict)):
        return [*errors, 'system_bootstrap_master_requires_one_bound_task']
    task = tasks[0]
    writable = {str(value) for key in ('target_files', 'files_allowed') for value in task.get(key) or []}
    if writable != {'policy.py'}:
        errors.append('system_bootstrap_master_writable_file_set_mismatch')
    if any((field in task for field in ('worker_prompt_compiled', 'worker_prompt_original_chars', 'task_brief_file'))):
        errors.append('system_bootstrap_master_externalized_worker_prompt_forbidden')
    prompt = str(task.get('worker_prompt') or '')
    from plan_compiler import SELECTED_PROPOSAL_BEGIN, SELECTED_PROPOSAL_END
    if any((term not in prompt for term in (SELECTED_PROPOSAL_BEGIN, SELECTED_PROPOSAL_END, f'proposal_id={selected_id}', f'contract_digest={expected_contract}'))):
        errors.append('system_bootstrap_worker_selected_proposal_block_missing')
    return list(dict.fromkeys(errors))


def apply_blueprint(workspace: str | _ssb.Path, *, checkpoint: dict[str, _ssb.Any], envelope: dict[str, _ssb.Any]) -> tuple[dict[tuple[int, str], str | bytes], list[str], dict[str, _ssb.Any]]:
    """Replace only policy bytes, then re-sign the two system identities."""
    workspace = _ssb.Path(workspace)
    errors = _ssb.validate_system_worker_envelope(checkpoint, envelope, candidate_dir=workspace)
    if errors:
        raise _ssb.SystemStrictBootstrapError(errors)
    manifest = _ssb.load_blueprint_manifest()
    from bot_artifact import hash_path
    before_hash = hash_path(workspace)
    if before_hash != manifest.get('prepared_artifact_hash'):
        raise _ssb.SystemStrictBootstrapError(['system_bootstrap_workspace_prepared_hash_mismatch'])
    before_files = _ssb._artifact_file_map(workspace)
    snapshots: dict[tuple[int, str], str | bytes] = {}
    for relative in sorted(_ssb._WORKER_CHANGED_FILES):
        path = workspace / relative
        snapshots[0, relative] = path.read_bytes()
    policy_path = workspace / 'policy.py'
    temporary = policy_path.with_name(f'.{policy_path.name}.{_ssb.os.getpid()}.tmp')
    temporary.write_bytes((_ssb.BLUEPRINT_DIR / 'policy.py').read_bytes())
    _ssb.os.replace(temporary, policy_path)
    try:
        from candidate_hygiene import cleanup_transient_candidate_artifacts
        cleanup_transient_candidate_artifacts(workspace, include_task_context=False)
    except Exception as exc:
        raise _ssb.SystemStrictBootstrapError([f'system_bootstrap_workspace_transient_cleanup_failed:{type(exc).__name__}:{str(exc)[:300]}']) from exc
    _ssb.refresh_policy_identity(workspace, version=_ssb.FIRST_STRICT_POLICY_VERSION)
    after_files = _ssb._artifact_file_map(workspace)
    changed = {relative for relative in set(before_files) | set(after_files) if before_files.get(relative) != after_files.get(relative)}
    if changed != _ssb._WORKER_CHANGED_FILES:
        raise _ssb.SystemStrictBootstrapError([f'system_bootstrap_changed_file_set_mismatch:expected={sorted(_ssb._WORKER_CHANGED_FILES)}:actual={sorted(changed)}'])
    output_hash = hash_path(workspace)
    if output_hash != manifest.get('output_artifact_hash'):
        raise _ssb.SystemStrictBootstrapError([f'system_bootstrap_output_artifact_hash_mismatch:expected={manifest.get('output_artifact_hash')}:actual={output_hash}'])
    from national_capability_contract import evaluate_national_capabilities
    capabilities = evaluate_national_capabilities(workspace)
    checks = capabilities.get('checks_by_id') or {}
    if capabilities.get('ok') is not True:
        raise _ssb.SystemStrictBootstrapError(['system_bootstrap_output_capability_probe_not_ok', *list(capabilities.get('required_failures') or [])[:10]])
    selected_test = ((checkpoint.get('master_plan') or {}).get('proposal_binding') or {}).get('falsifier', {}).get('test_name')
    if not isinstance(checks.get(selected_test), dict) or checks[selected_test].get('passed') is not True:
        raise _ssb.SystemStrictBootstrapError([f'system_bootstrap_selected_capability_not_proven:{selected_test}'])
    worker_receipt = _ssb._receipt({'schema_version': 1, 'kind': _ssb.SYSTEM_WORKER_RECEIPT_KIND, 'executor': _ssb.EXECUTOR_ID, 'source_v': _ssb.ARCHIVED_VERSION_HIGH_WATER, 'next_v': _ssb.FIRST_STRICT_POLICY_VERSION, 'master_receipt_digest': (checkpoint.get('audit_context') or {}).get('system_strict_bootstrap', {}).get('receipt_digest'), 'envelope_digest': envelope.get('envelope_digest'), 'prepared_artifact_hash': before_hash, 'output_artifact_hash': output_hash, 'changed_files': sorted(changed), 'selected_capability': selected_test, 'selected_capability_evidence_digest': _ssb._canonical_digest(checks[selected_test]), **_ssb.blueprint_identity(manifest)})
    return (snapshots, ['System verifier: prove policy.py is the only candidate-owned edit, the two system identities were regenerated, and runtime bytes stayed exact.'], worker_receipt)
