"""Companion for bootstrap_contract_recovery_diagnosis: called-allin failure-diagnosis envelopes & oracles.

Every intra-companion call routes through the main module as ``_bcd`` so moved
symbols stay single-dispatch even when invoked from the main module.
"""

from __future__ import annotations

import bootstrap_contract_recovery_diagnosis as _bcd


def _expected_called_allin_oracle_observations() -> list[dict[str, _bcd.Any]]:
    return [{'round': item['slot'], 'hand': item['hand'], 'stage': item['stage'], 'public_cards_observed': item['public_cards_observed'], 'wire_events_sha256': item['wire_events_sha256'], 'record_seq': list(item['record_seq']), 'observation_seq': list(item['observation_seq']), 'semantic_sequence': ['allin', 'call', 'earnChips', 'earnChips', 'oppo_hands', 'oppo_hands']} for item in _bcd._CALLED_ALLIN_FALSE_FAILURES]


def _expected_called_allin_incident_identity() -> dict[str, _bcd.Any]:
    return {'baseline_head': _bcd._CALLED_ALLIN_BASELINE_HEAD, 'baseline_contract_version': _bcd.PARKED_EVALUATION_CONTRACT_VERSION, 'baseline_contract_hash': _bcd._CALLED_ALLIN_BASELINE_CONTRACT_HASH, 'repair_contract_version': 41, 'workflow_run_id': _bcd._CALLED_ALLIN_WORKFLOW_RUN_ID, 'checkpoint_revision': _bcd._CALLED_ALLIN_CHECKPOINT_REVISION, 'candidate_artifact_hash': _bcd._CALLED_ALLIN_CANDIDATE_HASH, 'job_id': _bcd._CALLED_ALLIN_JOB_ID, 'job_result_digest': _bcd._CALLED_ALLIN_JOB_RESULT_DIGEST, 'rounds_requested': 8, 'rounds_completed': 8, 'rounds_run': 8, 'passed_rounds': 5, 'failed_rounds': 3}


def _called_allin_incident_identity_issues(value: _bcd.Any) -> list[str]:
    if not isinstance(value, dict) or set(value) != _bcd._CALLED_ALLIN_INCIDENT_IDENTITY_FIELDS or value != _bcd._expected_called_allin_incident_identity():
        return ['bootstrap_contract_called_allin_incident_identity_mismatch']
    return []


def _validate_called_allin_failure_diagnosis_envelope(value: _bcd.Any) -> dict[str, _bcd.Any]:
    """Validate the one exact workflow-v64 5/3 harness incident proof."""
    if not isinstance(value, dict) or set(value) != _bcd._CALLED_ALLIN_DIAGNOSIS_FIELDS:
        raise _bcd.BootstrapContractRecoveryError(['bootstrap_contract_called_allin_diagnosis_fields_invalid'])
    payload = {key: item for key, item in value.items() if key != 'proof_digest'}
    incident = value.get('incident_identity')
    oracle = value.get('oracle_identity')
    receipts = value.get('round_receipts')
    failures = value.get('false_failures')
    expected_observations = _bcd._expected_called_allin_oracle_observations()
    digest_fields = ('baseline_wire_probe_sha256', 'repair_wire_probe_sha256', 'baseline_harness_sha256', 'repair_harness_sha256', 'evidence_sha256', 'evidence_archive_sha256', 'evidence_archive_manifest_digest', 'suite_summary_sha256', 'attribution_digest')
    failure_digest_fields = ('receipt_sha256', 'wire_events_sha256', 'replay_summary_sha256', 'stored_summary_digest', 'corrected_summary_digest', 'omitted_runout_boundaries_digest')
    invalid = bool(value.get('schema_version') != 1 or value.get('kind') != _bcd._CALLED_ALLIN_DIAGNOSIS_KIND or value.get('profile_id') != _bcd._CALLED_ALLIN_PROFILE_ID or (value.get('defect_id') != _bcd._CALLED_ALLIN_DEFECT_ID) or _bcd._called_allin_incident_identity_issues(incident) or (value.get('proof_digest') != _bcd.canonical_digest(payload)) or (value.get('strength_evaluation') != 'not_applicable') or (value.get('disposition') != 'abandon_and_reprepare_only_without_evidence_reuse') or any((not _bcd._HEX64.fullmatch(str(value.get(field) or '')) for field in digest_fields)) or (value.get('baseline_wire_probe_sha256') == value.get('repair_wire_probe_sha256')) or (value.get('baseline_harness_sha256') == value.get('repair_harness_sha256')) or (not isinstance(oracle, dict)) or (set(oracle) != _bcd._CALLED_ALLIN_ORACLE_IDENTITY_FIELDS) or (oracle.get('document_path') != _bcd._CALLED_ALLIN_ORACLE_DOC) or (oracle.get('document_sha256') != _bcd._CALLED_ALLIN_ORACLE_DOC_SHA256) or (oracle.get('fixture_path') != _bcd._CALLED_ALLIN_ORACLE_FIXTURE) or (oracle.get('fixture_sha256') != _bcd._CALLED_ALLIN_ORACLE_FIXTURE_SHA256) or (oracle.get('oracle_id') != _bcd._CALLED_ALLIN_DEFECT_ID) or (oracle.get('authority_scope') != 'official_exe_wire_compliance_only') or (oracle.get('strength_weight') != 0) or (oracle.get('official_exe_sha256') != _bcd._CALLED_ALLIN_EXE_SHA256) or (oracle.get('control_artifact_sha256') != _bcd._CALLED_ALLIN_CONTROL_HASH) or (oracle.get('observations_digest') != _bcd.canonical_digest(expected_observations)) or (value.get('authority_absence') != _bcd._CALLED_ALLIN_AUTHORITY_ABSENCE) or (not isinstance(receipts, list)) or (len(receipts) != 8) or any((not isinstance(item, dict) or set(item) != _bcd._CALLED_ALLIN_ROUND_RECEIPT_FIELDS for item in receipts)) or (tuple((item.get('slot') for item in receipts)) != _bcd._CALLED_ALLIN_EXPECTED_SLOTS) or (tuple((item.get('passed') for item in receipts)) != _bcd._CALLED_ALLIN_PASS_PATTERN) or any((not isinstance(item.get('round_id'), str) or not item['round_id'].startswith(f'{item['slot']}_') or (not _bcd._HEX64.fullmatch(str(item.get('receipt_sha256') or ''))) for item in receipts)) or (not isinstance(failures, list)) or (len(failures) != 3) or any((not isinstance(item, dict) or set(item) != _bcd._CALLED_ALLIN_FALSE_FAILURE_FIELDS for item in failures)))
    if not invalid:
        receipt_by_slot = {item['slot']: item for item in receipts}
        for observed, expected in zip(failures, _bcd._CALLED_ALLIN_FALSE_FAILURES):
            invalid = bool(observed.get('slot') != expected['slot'] or observed.get('round_id') != receipt_by_slot[expected['slot']]['round_id'] or observed.get('hand') != expected['hand'] or (observed.get('stage') != expected['stage']) or (observed.get('public_cards_observed') != expected['public_cards_observed']) or (observed.get('wire_events_sha256') != expected['wire_events_sha256']) or (observed.get('receipt_sha256') != receipt_by_slot[expected['slot']]['receipt_sha256']) or (observed.get('corrected_hands_started') != expected['corrected_hands_started']) or (observed.get('corrected_settlements') != expected['corrected_settlements']) or (observed.get('corrected_pending_count') != 1) or (type(observed.get('event_count')) is not int) or (observed['event_count'] < max(expected['record_seq'])) or any((not _bcd._HEX64.fullmatch(str(observed.get(field) or '')) for field in failure_digest_fields)))
            if invalid:
                break
    if invalid:
        raise _bcd.BootstrapContractRecoveryError(['bootstrap_contract_called_allin_diagnosis_invalid'])
    return value


def _called_allin_oracle_identity(root: _bcd.Path, *, expected_repair_head: str, require_live_repair_source: bool) -> dict[str, _bcd.Any]:
    from bootstrap_contract_recovery import _git as _git
    from bootstrap_contract_recovery import _read_regular_exact as _read_regular_exact
    document_raw = _git(root, 'show', f'{expected_repair_head}:{_bcd._CALLED_ALLIN_ORACLE_DOC}', binary=True)
    fixture_raw = _git(root, 'show', f'{expected_repair_head}:{_bcd._CALLED_ALLIN_ORACLE_FIXTURE}', binary=True)
    if not isinstance(document_raw, bytes) or not isinstance(fixture_raw, bytes):
        raise ValueError('called-allin oracle Git blobs are unavailable')
    if _bcd._sha256_bytes(document_raw) != _bcd._CALLED_ALLIN_ORACLE_DOC_SHA256 or _bcd._sha256_bytes(fixture_raw) != _bcd._CALLED_ALLIN_ORACLE_FIXTURE_SHA256:
        raise ValueError('called-allin oracle Git identity changed')
    if require_live_repair_source:
        live_document = _read_regular_exact(root / _bcd._CALLED_ALLIN_ORACLE_DOC, max_bytes=256 * 1024)
        live_fixture = _read_regular_exact(root / _bcd._CALLED_ALLIN_ORACLE_FIXTURE, max_bytes=256 * 1024)
        if live_document != document_raw or live_fixture != fixture_raw:
            raise ValueError('live called-allin oracle is not the reviewed repair')
    fixture = _bcd.json.loads(fixture_raw.decode('utf-8'))
    expected_observations = _bcd._expected_called_allin_oracle_observations()
    if not isinstance(fixture, dict) or fixture.get('schema_version') != 1 or fixture.get('oracle_id') != _bcd._CALLED_ALLIN_DEFECT_ID or (fixture.get('authority_scope') != 'official_exe_wire_compliance_only') or (fixture.get('strength_weight') != 0) or (fixture.get('official_exe_sha256') != _bcd._CALLED_ALLIN_EXE_SHA256) or (fixture.get('job_id') != _bcd._CALLED_ALLIN_JOB_ID) or (fixture.get('job_result_digest') != _bcd._CALLED_ALLIN_JOB_RESULT_DIGEST) or (fixture.get('candidate_artifact_sha256') != _bcd._CALLED_ALLIN_CANDIDATE_HASH) or (fixture.get('control_artifact_sha256') != _bcd._CALLED_ALLIN_CONTROL_HASH) or (fixture.get('observations') != expected_observations) or (fixture.get('accepted_board_prefixes') != {'preflop': 0, 'flop': 3, 'turn': 4}) or ((fixture.get('required_local_terminal_proof') or {}).get('action_suffix') != ['allin', 'call']) or ((fixture.get('required_local_terminal_proof') or {}).get('action_suffix_same_stage') is not True) or ((fixture.get('required_local_terminal_proof') or {}).get('pot') != 40000) or ((fixture.get('required_cross_connection_proof') or {}).get('all_prior_streets_closed_or_called_allin_runout') is not True) or ((fixture.get('required_cross_connection_proof') or {}).get('settlement_values') != [[-20000, 20000], [0, 0]]) or ((fixture.get('required_strict_thp_proof') or {}).get('complete_public_board_cards') != 5) or ((fixture.get('natural_hand_70') or {}).get('dual_showdown_reveal_required_for_called_allin') is not True) or ('accept_actions_from_a_prior_street_as_the_terminal_suffix' not in (fixture.get('forbidden_inferences') or [])) or ('accept_an_unclosed_prior_street_before_the_allin_street' not in (fixture.get('forbidden_inferences') or [])) or ('treat_official_exe_oracle_as_strength' not in (fixture.get('forbidden_inferences') or [])):
        raise ValueError('called-allin oracle semantic identity changed')
    return {'document_path': _bcd._CALLED_ALLIN_ORACLE_DOC, 'document_sha256': _bcd._CALLED_ALLIN_ORACLE_DOC_SHA256, 'fixture_path': _bcd._CALLED_ALLIN_ORACLE_FIXTURE, 'fixture_sha256': _bcd._CALLED_ALLIN_ORACLE_FIXTURE_SHA256, 'oracle_id': fixture['oracle_id'], 'authority_scope': fixture['authority_scope'], 'strength_weight': fixture['strength_weight'], 'official_exe_sha256': fixture['official_exe_sha256'], 'control_artifact_sha256': fixture['control_artifact_sha256'], 'observations_digest': _bcd.canonical_digest(expected_observations)}


def _called_allin_authority_absence(root: _bcd.Path, *, candidate: _bcd.Path, control_consumption: dict[str, _bcd.Any], require_live: bool) -> dict[str, _bcd.Any]:
    if require_live:
        certificate = root / 'official_certificates' / f'{_bcd.bot_name(_bcd.FIRST_STRICT_POLICY_VERSION)}.json'
        tags = (_bcd.bot_tag(_bcd.FIRST_STRICT_POLICY_VERSION), _bcd.high_water_tag(_bcd.FIRST_STRICT_POLICY_VERSION))
        from evolution_core import get_active_bots
        from national_runtime_authority import strict_published_bot_names
        if _bcd.os.path.lexists(certificate) or _bcd.os.path.lexists(candidate / '.completed') or any((not _bcd._git_absence(root, 'show-ref', '--verify', '--quiet', f'refs/tags/{tag}') for tag in tags)) or list(get_active_bots()) or list(strict_published_bot_names()) or (control_consumption.get('successful_count') != 0) or (control_consumption.get('max_successful_consumptions') != 1):
            raise ValueError('called-allin publication authority is not absent')
    return {**_bcd._CALLED_ALLIN_AUTHORITY_ABSENCE, 'completion_tags': [], 'active_bots': [], 'strict_published_bots': []}


def _called_allin_runout_failure_diagnosis(root: _bcd.Path, directory: _bcd.Path, *, request: dict[str, _bcd.Any], state: dict[str, _bcd.Any], status: dict[str, _bcd.Any], candidate_hash: str, workflow_run_id: str, checkpoint_revision: int, job_result_digest: str, expected_evaluation_contract_version: int, expected_evaluation_contract_hash: str, expected_repair_contract_version: int, expected_baseline_head: str, expected_repair_head: str, control_consumption: dict[str, _bcd.Any], require_live_repair_source: bool=True) -> dict[str, _bcd.Any]:
    """Reopen only the v64 5-pass/3-false-fail called-all-in incident."""
    from bootstrap_contract_recovery import _git as _git
    from bootstrap_contract_recovery import _read_regular_exact as _read_regular_exact
    from official_evidence_archive import validate_evidence_archive
    from official_wire_probe import replay_events
    incident_identity = {'baseline_head': expected_baseline_head, 'baseline_contract_version': expected_evaluation_contract_version, 'baseline_contract_hash': expected_evaluation_contract_hash, 'repair_contract_version': expected_repair_contract_version, 'workflow_run_id': workflow_run_id, 'checkpoint_revision': checkpoint_revision, 'candidate_artifact_hash': candidate_hash, 'job_id': directory.name, 'job_result_digest': job_result_digest, 'rounds_requested': 8, 'rounds_completed': 8, 'rounds_run': 8, 'passed_rounds': 5, 'failed_rounds': 3}
    if _bcd._called_allin_incident_identity_issues(incident_identity):
        raise ValueError('called-allin incident identity is not exact')
    if state.get('attempt') != 1 or state.get('result_digest') != _bcd._CALLED_ALLIN_JOB_RESULT_DIGEST:
        raise ValueError('called-allin job attempt/result identity changed')
    identity = request.get('identity')
    identity = identity if isinstance(identity, dict) else {}
    platform = identity.get('platform')
    platform = platform if isinstance(platform, dict) else {}
    if request.get('job_id') != _bcd._CALLED_ALLIN_JOB_ID or identity.get('candidate_hash') != _bcd._CALLED_ALLIN_CANDIDATE_HASH or identity.get('opponent_hash') != _bcd._CALLED_ALLIN_CONTROL_HASH or (platform.get('exe_sha256') != _bcd._CALLED_ALLIN_EXE_SHA256):
        raise ValueError('called-allin request identity changed')
    source_identities: dict[str, str] = {}
    for label, relative in (('wire_probe', 'web/core/official_wire_probe.py'), ('harness', 'web/core/official_platform_harness.py')):
        baseline_raw = _git(root, 'show', f'{expected_baseline_head}:{relative}', binary=True)
        repair_raw = _git(root, 'show', f'{expected_repair_head}:{relative}', binary=True)
        if not isinstance(baseline_raw, bytes) or not isinstance(repair_raw, bytes):
            raise ValueError(f'called-allin {label} source is unavailable')
        baseline_sha256 = _bcd._sha256_bytes(baseline_raw)
        repair_sha256 = _bcd._sha256_bytes(repair_raw)
        platform_field = f'{label}_sha256'
        if platform.get(platform_field) != baseline_sha256 or baseline_sha256 == repair_sha256:
            raise ValueError(f'called-allin {label} contract change is unproven')
        if require_live_repair_source:
            live_raw = _read_regular_exact(root / relative, max_bytes=4 * 1024 * 1024)
            if live_raw != repair_raw:
                raise ValueError(f'live {label} is not the reviewed repair')
        source_identities[f'baseline_{label}_sha256'] = baseline_sha256
        source_identities[f'repair_{label}_sha256'] = repair_sha256
    oracle_identity = _bcd._called_allin_oracle_identity(root, expected_repair_head=expected_repair_head, require_live_repair_source=require_live_repair_source)
    candidate = root / 'bots' / _bcd.bot_name(_bcd.FIRST_STRICT_POLICY_VERSION)
    authority_absence = _bcd._called_allin_authority_absence(root, candidate=candidate, control_consumption=control_consumption, require_live=require_live_repair_source)
    suite = directory / 'suite_attempt_01'
    _bcd._require_regular_directory(suite)
    status_summary = status.get('summary')
    status_summary = status_summary if isinstance(status_summary, dict) else {}
    if _bcd.Path(str(status_summary.get('suite_dir') or '')) != suite:
        raise ValueError('called-allin suite path is not job-owned')
    evidence_path = suite / 'official_evidence.json'
    if _bcd.Path(str(status.get('official_evidence_path') or '')) != evidence_path:
        raise ValueError('called-allin evidence path is not canonical')
    summary_raw, suite_report = _bcd._regular_json(suite / 'summary.json', max_bytes=4 * 1024 * 1024)
    evidence_raw, evidence = _bcd._regular_json(evidence_path, max_bytes=4 * 1024 * 1024)
    evidence_sha256 = _bcd._sha256_bytes(evidence_raw)
    deterministic = status.get('official_deterministic_status_receipt')
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    archive = status.get('official_evidence_archive')
    archive = archive if isinstance(archive, dict) else {}
    archive_validation = validate_evidence_archive(archive, expected_evidence_sha256=evidence_sha256)
    if deterministic.get('evidence_sha256') != evidence_sha256 or archive.get('evidence_sha256') != evidence_sha256 or archive_validation.get('valid') is not True or (evidence.get('schema_version') != 1) or (evidence.get('purpose') != 'official_platform_compliance') or (evidence.get('strength_evaluation') != 'not_applicable'):
        raise ValueError('called-allin evidence/archive identity changed')
    expected_summary = {'self_play_rounds': 5, 'opponent_rounds': 3, 'target_hands': 70, 'rounds_requested': 8, 'rounds_run': 8, 'passed_rounds': 5, 'failed_rounds': 3, 'resumed_rounds': 0, 'official_platform': True}
    report_summary = suite_report.get('summary')
    report_summary = report_summary if isinstance(report_summary, dict) else {}
    evidence_summary = evidence.get('summary')
    evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
    if any((status_summary.get(key) != expected or report_summary.get(key) != expected or evidence_summary.get(key) != expected for key, expected in expected_summary.items())):
        raise ValueError('called-allin suite is not exact 5-pass/3-fail')
    if _bcd.Path(str(report_summary.get('suite_dir') or '')) != suite or _bcd.Path(str(evidence_summary.get('suite_dir') or '')) != suite or report_summary.get('attribution') != status_summary.get('attribution') or (evidence_summary.get('attribution') != status_summary.get('attribution')) or (report_summary.get('formal_execution') != status_summary.get('formal_execution')) or (evidence_summary.get('formal_execution') != status_summary.get('formal_execution')) or (not isinstance(status_summary.get('formal_execution'), dict)) or (status_summary['formal_execution'].get('ok') is not True) or (status_summary['formal_execution'].get('issues') != []) or (evidence_summary.get('passed') is not False) or (evidence_summary.get('raw_passed') is not False) or (evidence_summary.get('wire_evidence_required_rounds') != 8) or (evidence_summary.get('wire_evidence_complete_rounds') != 8):
        raise ValueError('called-allin suite crossbinding changed')
    attribution = status_summary.get('attribution')
    attribution = attribution if isinstance(attribution, dict) else {}
    attribution_rounds = attribution.get('rounds')
    if status.get('status') != 'official-inconclusive' or attribution.get('schema_version') != 1 or attribution.get('policy_id') != 'official-attribution-v1' or (attribution.get('candidate_verdict') != 'inconclusive') or (attribution.get('candidate_blocking') is not False) or (attribution.get('inconclusive') is not True) or (attribution.get('countable_rounds') != 5) or (not isinstance(attribution_rounds, list)) or (len(attribution_rounds) != 8):
        raise ValueError('called-allin attribution is not harness-inconclusive')
    report_rounds = suite_report.get('rounds')
    evidence_rounds = evidence.get('rounds')
    if not isinstance(report_rounds, list) or len(report_rounds) != 8 or (not isinstance(evidence_rounds, list)) or (len(evidence_rounds) != 8):
        raise ValueError('called-allin suite round set is incomplete')
    oracle_by_slot = {item['slot']: item for item in _bcd._CALLED_ALLIN_FALSE_FAILURES}
    round_receipts: list[dict[str, _bcd.Any]] = []
    false_failures: list[dict[str, _bcd.Any]] = []
    for offset, slot in enumerate(_bcd._CALLED_ALLIN_EXPECTED_SLOTS):
        expected_passed = _bcd._CALLED_ALLIN_PASS_PATTERN[offset]
        kind = 'self_play' if slot.startswith('self_play') else 'opponent'
        index = int(slot.rsplit('_', 1)[1])
        receipt = report_rounds[offset]
        evidence_round = evidence_rounds[offset]
        attribution_round = attribution_rounds[offset]
        if not all((isinstance(item, dict) for item in (receipt, evidence_round, attribution_round))):
            raise ValueError('called-allin round evidence shape is invalid')
        round_id = receipt.get('round_id')
        if receipt.get('round_kind') != kind or receipt.get('round_index') != index or receipt.get('target_hands') != 70 or (receipt.get('passed') is not expected_passed) or (not isinstance(round_id, str)) or (not round_id.startswith(f'{slot}_')) or (evidence_round.get('round_kind') != kind) or (evidence_round.get('round_index') != index) or (evidence_round.get('round_id') != round_id) or (evidence_round.get('passed') is not expected_passed) or (attribution_round.get('candidate_blocking') is not False) or (attribution_round.get('candidate_verdict') != ('pass' if expected_passed else 'inconclusive')) or (attribution_round.get('countable') is not expected_passed):
            raise ValueError('called-allin round outcome identity changed')
        _bcd._require_exact_round_job_envelope(receipt.get('job_envelope'), status.get('official_job_envelope'), job_id=directory.name, candidate_hash=candidate_hash)
        wire_probe = receipt.get('wire_probe')
        wire_probe = wire_probe if isinstance(wire_probe, dict) else {}
        if wire_probe.get('enabled') is not True or wire_probe.get('issues') != []:
            raise ValueError('called-allin wire probe failed independently')
        artifacts = evidence_round.get('artifacts')
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        receipt_item = artifacts.get('receipt')
        archive_path = str((receipt_item or {}).get('archive_path') or '')
        pure_receipt = _bcd.PurePosixPath(archive_path)
        if len(pure_receipt.parts) != 4 or pure_receipt.parts[0] != slot or pure_receipt.parts[1] != 'executions' or (_bcd.re.fullmatch('run_[0-9]+_[0-9]+', pure_receipt.parts[2]) is None) or (pure_receipt.parts[3] != 'receipt.json'):
            raise ValueError('called-allin round execution path is invalid')
        execution_prefix = '/'.join(pure_receipt.parts[:-1])
        receipt_raw = _bcd._strict_artifact_bytes(suite, receipt_item, expected_archive_path=f'{execution_prefix}/receipt.json', max_bytes=2 * 1024 * 1024)
        if _bcd.json.loads(receipt_raw.decode('utf-8')) != receipt:
            raise ValueError('called-allin summary receipt bytes changed')
        slot_dir = suite / slot
        executions = slot_dir / 'executions'
        execution_dir = executions / pure_receipt.parts[2]
        for owned_directory in (slot_dir, executions, execution_dir):
            _bcd._require_regular_directory(owned_directory)
        if sorted((item.name for item in slot_dir.iterdir())) != ['executions', 'receipt.json'] or sorted((item.name for item in executions.iterdir())) != [pure_receipt.parts[2]] or _read_regular_exact(slot_dir / 'receipt.json', max_bytes=2 * 1024 * 1024) != receipt_raw:
            raise ValueError('called-allin round was resumed or duplicated')
        receipt_sha256 = _bcd._sha256_bytes(receipt_raw)
        round_receipts.append({'slot': slot, 'round_id': round_id, 'passed': expected_passed, 'receipt_sha256': receipt_sha256})
        stored_replay = receipt.get('wire_replay_summary')
        if not isinstance(stored_replay, dict) or evidence_round.get('wire_replay_summary') != stored_replay:
            raise ValueError('called-allin stored replay is not cross-bound')
        if expected_passed:
            if receipt.get('issues') != [] or stored_replay.get('issues') != [] or stored_replay.get('warnings') != [] or (stored_replay.get('hands_started_min') != 70) or (stored_replay.get('settlements_min') != 69):
                raise ValueError('called-allin passing round is not intact')
            continue
        expected_failure = oracle_by_slot.get(slot)
        if expected_failure is None:
            raise ValueError('called-allin false-failure slot is unsupported')
        wire_raw = _bcd._strict_artifact_bytes(suite, artifacts.get('wire_events'), expected_archive_path=f'{execution_prefix}/wire_events.jsonl', max_bytes=2 * 1024 * 1024)
        replay_raw = _bcd._strict_artifact_bytes(suite, artifacts.get('replay_summary'), expected_archive_path=f'{execution_prefix}/replay_summary.json', max_bytes=2 * 1024 * 1024)
        if _bcd._sha256_bytes(wire_raw) != expected_failure['wire_events_sha256'] or _bcd.json.loads(replay_raw.decode('utf-8')) != stored_replay:
            raise ValueError('called-allin raw/replay oracle binding changed')
        old_issues = stored_replay.get('issues')
        if not isinstance(old_issues, list) or len(old_issues) != 2 or [item.get('conn') for item in old_issues] != ['B', 'A'] or any((not isinstance(item, dict) or item.get('kind') != 'showdown_boundary_invalid' or item.get('hand') != expected_failure['hand'] or (item.get('stage') != expected_failure['stage']) or (item.get('reason') != 'oppo_hands is valid only at a five-card non-fold showdown') for item in old_issues)) or (stored_replay.get('warnings') != []) or (stored_replay.get('hands_started_min') != expected_failure['corrected_hands_started']) or (stored_replay.get('settlements_min') != expected_failure['corrected_settlements']):
            raise ValueError('called-allin old replay has another failure')
        round_issues = receipt.get('issues')
        if not isinstance(round_issues, list) or len([issue for issue in round_issues if isinstance(issue, str) and issue.startswith('wire_showdown_boundary_invalid:')]) != 2 or sorted((issue for issue in round_issues if not str(issue).startswith('wire_showdown_boundary_invalid:'))) != sorted(_bcd._LEGACY_DOWNSTREAM_FINDINGS):
            raise ValueError('called-allin old receipt contains another failure')
        events = [_bcd.json.loads(line) for line in wire_raw.decode('utf-8').splitlines() if line.strip()]
        if any((not isinstance(event, dict) for event in events)) or stored_replay.get('events_seen') != len(events) or len(events) < max(expected_failure['record_seq']):
            raise ValueError('called-allin raw event set is incomplete')
        selected = [events[index - 1] for index in expected_failure['record_seq']]
        selected_messages = [str((event.get('messages') or [''])[0]) for event in selected]
        if [event.get('observation_seq') for event in selected] != expected_failure['observation_seq'] or selected_messages[:3] != ['allin', 'allin', 'call'] or any((not message.startswith('earnChips ') for message in selected_messages[3:5])) or any((not message.startswith('oppo_hands|') for message in selected_messages[5:])):
            raise ValueError('called-allin raw semantic sequence changed')
        corrected = replay_events(events, now=max((float(event['t']) for event in events)), finalized=True)
        corrected_warnings = corrected.get('warnings')
        corrected_omissions = corrected.get('omitted_allin_runout_boundaries')
        pending = corrected.get('pending_expected_actions')
        if corrected.get('issues') != [] or not isinstance(corrected_warnings, list) or len(corrected_warnings) != 2 or any((item.get('kind') != 'showdown_runout_omitted_after_called_allin' or item.get('hand') != expected_failure['hand'] or item.get('stage') != expected_failure['stage'] or (item.get('public_cards_observed') != expected_failure['public_cards_observed']) for item in corrected_warnings)) or (not isinstance(corrected_omissions, list)) or (len(corrected_omissions) != 2) or ({item.get('conn') for item in corrected_omissions} != {'A', 'B'}) or any((item.get('kind') != 'omitted_allin_runout' or item.get('hand') != expected_failure['hand'] or item.get('stage') != expected_failure['stage'] or (item.get('public_cards_observed') != expected_failure['public_cards_observed']) or (item.get('natural_hand_70') is not False) or (item.get('player_chips') != 0) or (item.get('opponent_chips') != 0) or (item.get('player_bet') != item.get('opponent_bet')) or (item.get('pot') != 40000) or ([action.get('action_type') for action in item.get('action_suffix') or []] != ['allin', 'call']) or any((action.get('stage') != expected_failure['stage'] for action in item.get('action_suffix') or [])) for item in corrected_omissions)) or (sorted((item.get('settlement_amount') for item in corrected_omissions)) not in ([-20000, 20000], [0, 0])) or (corrected.get('events_seen') != len(events)) or (corrected.get('hands_started_min') != expected_failure['corrected_hands_started']) or (corrected.get('settlements_min') != expected_failure['corrected_settlements']) or (not isinstance(pending, list)) or (len(pending) != 1):
            raise ValueError('called-allin repaired replay is not exact')
        false_failures.append({'slot': slot, 'round_id': round_id, 'hand': expected_failure['hand'], 'stage': expected_failure['stage'], 'public_cards_observed': expected_failure['public_cards_observed'], 'receipt_sha256': receipt_sha256, 'wire_events_sha256': _bcd._sha256_bytes(wire_raw), 'replay_summary_sha256': _bcd._sha256_bytes(replay_raw), 'event_count': len(events), 'stored_summary_digest': _bcd.canonical_digest(stored_replay), 'corrected_summary_digest': _bcd.canonical_digest(corrected), 'omitted_runout_boundaries_digest': _bcd.canonical_digest(corrected_omissions), 'corrected_hands_started': corrected['hands_started_min'], 'corrected_settlements': corrected['settlements_min'], 'corrected_pending_count': len(pending)})
    payload = {'schema_version': 1, 'kind': _bcd._CALLED_ALLIN_DIAGNOSIS_KIND, 'profile_id': _bcd._CALLED_ALLIN_PROFILE_ID, 'defect_id': _bcd._CALLED_ALLIN_DEFECT_ID, 'incident_identity': incident_identity, **source_identities, 'oracle_identity': oracle_identity, 'evidence_sha256': evidence_sha256, 'evidence_archive_sha256': archive['archive_sha256'], 'evidence_archive_manifest_digest': archive['manifest_digest'], 'suite_summary_sha256': _bcd._sha256_bytes(summary_raw), 'attribution_digest': _bcd.canonical_digest(attribution), 'round_receipts': round_receipts, 'false_failures': false_failures, 'authority_absence': authority_absence, 'strength_evaluation': 'not_applicable', 'disposition': 'abandon_and_reprepare_only_without_evidence_reuse'}
    return _bcd._validate_called_allin_failure_diagnosis_envelope({**payload, 'proof_digest': _bcd.canonical_digest(payload)})
