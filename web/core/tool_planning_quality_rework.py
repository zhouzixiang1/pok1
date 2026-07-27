"""Companion for tool_planning_quality_contracts: mechanical file-size trims & precommit rework synthesis.

Every intra-companion call routes through the main module as ``_qc`` so moved
symbols stay single-dispatch even when invoked from the main module.
"""

from __future__ import annotations

import tool_planning_quality_contracts as _qc


def _text_line_count(text):
    if not text:
        return 0
    return text.count('\n') + (0 if text.endswith('\n') else 1)


def _docstring_line_ranges(text):
    ranges = set()
    try:
        tree = _qc.ast.parse(text)
    except SyntaxError:
        return ranges
    node_types = (_qc.ast.Module, _qc.ast.FunctionDef, _qc.ast.AsyncFunctionDef, _qc.ast.ClassDef)
    for node in _qc.ast.walk(tree):
        if not isinstance(node, node_types) or not getattr(node, 'body', None):
            continue
        first = node.body[0]
        if not (isinstance(first, _qc.ast.Expr) and isinstance(getattr(first, 'value', None), _qc.ast.Constant) and isinstance(first.value.value, str)):
            continue
        if not isinstance(node, _qc.ast.Module) and len(node.body) == 1:
            continue
        end_lineno = getattr(first, 'end_lineno', first.lineno)
        ranges.update(range(first.lineno, end_lineno + 1))
    return ranges


def _tokenized_comment_and_string_lines(text):
    comment_lines = set()
    string_lines = set()
    try:
        tokens = _qc.tokenize.generate_tokens(_qc.io.StringIO(text).readline)
        for tok in tokens:
            if tok.type == _qc.tokenize.COMMENT:
                line = tok.line or ''
                if not line[:tok.start[1]].strip():
                    comment_lines.add(tok.start[0])
            elif tok.type == _qc.tokenize.STRING:
                string_lines.update(range(tok.start[0], tok.end[0] + 1))
    except (_qc.tokenize.TokenError, IndentationError):
        pass
    return (comment_lines, string_lines)


def _mechanically_trim_python_text(text):
    """Remove non-behavioral Python text and return ``(new_text, stats)``."""
    lines = text.splitlines(keepends=True)
    before = len(lines)
    if not lines:
        return (text, {'before': 0, 'after': 0, 'removed': 0})
    docstring_lines = _qc._docstring_line_ranges(text)
    comment_lines, string_lines = _qc._tokenized_comment_and_string_lines(text)
    protected_string_lines = string_lines - docstring_lines
    remove_lines = set(docstring_lines)
    remove_lines.update(comment_lines - protected_string_lines)
    for idx, line in enumerate(lines, start=1):
        if idx not in protected_string_lines and (not line.strip()):
            remove_lines.add(idx)
    trimmed_lines = [line for idx, line in enumerate(lines, start=1) if idx not in remove_lines]
    new_text = ''.join(trimmed_lines)
    if new_text and (not new_text.endswith('\n')):
        new_text += '\n'
    after = _qc._text_line_count(new_text)
    return (new_text, {'before': before, 'after': after, 'removed': before - after, 'docstring_lines': len(docstring_lines), 'comment_lines': len(comment_lines), 'blank_lines': sum((1 for idx, line in enumerate(lines, start=1) if idx in remove_lines and (not line.strip())))})


def _mechanical_trim_python_file(path, limit):
    path = _qc.Path(path)
    try:
        old_text = path.read_text(encoding='utf-8')
    except OSError as exc:
        return {'changed': False, 'error': str(exc), 'file': str(path)}
    before = _qc._text_line_count(old_text)
    if limit is not None and before <= int(limit):
        return {'changed': False, 'file': str(path), 'before': before, 'after': before, 'limit': limit}
    new_text, stats = _qc._mechanically_trim_python_text(old_text)
    after = _qc._text_line_count(new_text)
    if after >= before:
        return {'changed': False, 'file': str(path), 'before': before, 'after': after, 'limit': limit}
    try:
        path.write_text(new_text, encoding='utf-8')
        _qc.py_compile.compile(str(path), doraise=True)
    except Exception as exc:
        try:
            path.write_text(old_text, encoding='utf-8')
        except OSError:
            pass
        return {'changed': False, 'rolled_back': True, 'error': str(exc), 'file': str(path), 'before': before, 'after': before, 'attempted_after': after, 'limit': limit}
    return {'changed': True, 'file': str(path), 'limit': limit, **stats}


def _apply_mechanical_file_size_trims(tasks, next_dir, source_dir, next_v, source_v):
    """Apply behavior-preserving text trims before expensive file_size workers."""
    try:
        _total, oversized = _qc.check_code_size(next_dir, source_dir=source_dir)
    except Exception as exc:
        log_system_event('pipeline.file_size_mechanical_trim_check_failed', 'warn', f'Could not compute file_size mechanical trim inputs for v{next_v}: {exc}', {'next_v': next_v, 'source_v': source_v})
        return []
    oversized_by_name = {_qc.Path(name).name: (lines, limit) for name, lines, limit in oversized}
    results = []
    for task in tasks or []:
        if not _qc._is_file_size_repair_task(task):
            continue
        for target in task.get('target_files', []) or []:
            rel = _qc._target_rel(target, next_v)
            if not rel:
                continue
            filename = _qc.Path(rel).name
            current = oversized_by_name.get(filename)
            if not current:
                continue
            lines, limit = current
            if int(lines) - int(limit) < 200:
                continue
            path = next_dir / rel
            result = _qc._mechanical_trim_python_file(path, limit)
            result.update({'next_v': next_v, 'source_v': source_v, 'target': rel, 'initial_lines': lines})
            results.append(result)
            if result.get('changed'):
                log_system_event('pipeline.file_size_mechanical_trim_applied', 'warn', f'Applied mechanical file_size trim to v{next_v}/{rel}: {result.get('before')}L -> {result.get('after')}L (limit {limit})', result)
            elif result.get('error'):
                log_system_event('pipeline.file_size_mechanical_trim_failed', 'warn', f'Mechanical file_size trim failed for v{next_v}/{rel}: {result.get('error')}', result)
    return results


def _precommit_repair_task(filename, ckpt, feedback):
    if _qc.Path(str(filename)).name not in _ACTIVE_CANDIDATE_WRITABLE_FILES:
        raise ValueError(f'precommit repair cannot write system/extra artifact {filename!r}')
    next_v = ckpt.get('next_v')
    source_v = ckpt.get('source_v')
    suffix = _qc._task_id_suffix(filename)
    line_note = ''
    try:
        path = get_bot_dir(next_v) / filename
        if path.exists():
            line_count = sum((1 for _ in path.open('r', encoding='utf-8', errors='ignore')))
            if line_count >= 2300:
                line_note = f'\n- `{filename}` is near the hard size cap ({line_count} lines). Prefer deleting or tightening an existing risky branch over adding a new subsystem.'
    except Exception:
        line_note = ''
    prompt = f"This is one file-scoped precommit regression repair from a failed native national TCP final gate for bots/{_qc.bot_name(next_v)}.\n\nTarget file: `{filename}`\nSource lineage identity: national_v{source_v} (not readable by this Worker)\nFailed candidate: bots/{_qc.bot_name(next_v)}/\n\nExact precommit feedback:\n{feedback}\n\nNon-negotiable national position invariant:\n- This invariant is protocol correctness, not an EV/matchup lever. Do not change, relax, or roll it back to chase a precommit result.\n- Read `decision_context.hand.position`/`acts_first_postflop` and `decision_context.line.position`/`hero_in_position_postflop` directly.\n- `big_blind` acts first postflop; `small_blind` is in position postflop.\n- Never reconstruct seat identity, action order, donk, delayed-probe, or responding-to-check state inside candidate policy.\n- Preserve the candidate's national TCP position semantics and the official oracle boundaries.\n\nRequired method:\n- Only edit `{filename}`. Other files are intentionally out of scope for this worker.\n- This is a policy/matchup repair. `policy.py` is the sole writable file; national_bot.py and precompute.py remain byte-identical system artifacts.\n- Use the system-injected precommit feedback and current candidate region to identify which changed behavior could explain the losing complete 70-hand native TCP samples.\n- Make one bounded EV/matchup correction in this file. Prefer tightening, gating, or partially rolling back a risky new branch over adding broad new logic.\n- Do not wholesale replace the candidate with the source parent; the final candidate must remain a real code change after repair.\n- Preserve native TCP protocol/card mapping, national action legality, and previously passed quality gates.\n- Run `python -m py_compile bots/{_qc.bot_name(next_v)}/{filename}` before finishing; system gates own imports and dynamic execution.{line_note}"
    return {'worker_id': f'auto_precommit_repair_{suffix}', 'role': 'Strategic Regression Repair Architect', 'target_files': [filename], 'must_change_files': [filename], 'worker_prompt': prompt, 'task_kind': 'precommit_repair', 'repair_blocker': 'precommit_regression', 'repair_contract': {'blocker': 'precommit_regression', 'file': filename, 'evidence': feedback[:2000], 'protected_invariants': ['national_position_semantics']}}


def _precommit_repair_tasks(ckpt, feedback):
    return [_qc._precommit_repair_task(filename, ckpt, feedback) for filename in _qc._precommit_repair_target_files(ckpt, feedback)]


def _precommit_repair_task_refresh_reason(tasks, ckpt, feedback=''):
    if not _qc._is_precommit_rework_checkpoint(ckpt):
        return ''
    if not tasks:
        return 'missing precommit repair task(s)'
    expected = set(_qc._precommit_repair_target_files(ckpt, feedback))
    task_targets = []
    for task in tasks or []:
        if not isinstance(task, dict):
            return 'invalid precommit repair task'
        task_kind = str(task.get('task_kind') or '').lower()
        task_text = ' '.join([str(task.get('worker_id', '')), str(task.get('role', '')), str(task.get('worker_prompt', task.get('instruction', '')))[:500]]).lower()
        if 'precommit_repair' not in task_kind and 'precommit' not in task_text:
            return 'checkpoint task is not a precommit repair'
        prompt_text = str(task.get('worker_prompt', task.get('instruction', ''))).lower()
        if 'national position invariant' not in prompt_text or 'decision_context.hand.position' not in prompt_text or 'decision_context.line.position' not in prompt_text or ('not an ev/matchup lever' not in prompt_text):
            return 'precommit repair task is missing national position invariant'
        targets = [rel for rel in (_qc._target_rel(target, ckpt.get('next_v')) for target in task.get('target_files', []) or []) if rel]
        must_change = [rel for rel in (_qc._target_rel(target, ckpt.get('next_v')) for target in task.get('must_change_files', []) or []) if rel]
        if len(targets) != 1:
            return 'precommit repair task is not file-scoped'
        if must_change and must_change != targets:
            return 'precommit repair must_change_files do not match its single target'
        task_targets.extend(targets)
    task_set = set(task_targets)
    if expected and task_set != expected:
        return 'precommit repair targets are stale'
    if len(task_targets) != len(task_set):
        return 'duplicate precommit repair targets'
    return ''


def _synthesize_rework_tasks_from_checkpoint(ckpt, reviewer_feedback=''):
    """Build bounded repair tasks when a checkpoint has gate feedback but no plan.

    Legacy crossover checkpoints and the defensive hard-position repair route
    may store a synthetic plan with no worker tasks. New crossover generations
    stop at ``prepared`` and pass through direction audit, Master, and Workers
    before quality; deterministic task synthesis remains necessary for older or
    explicit repair checkpoints.
    """
    if not isinstance(ckpt, dict):
        return []
    stage = ckpt.get('stage')
    if stage not in {'quality_failed', 'repair_planned', 'rework_running', 'precommit_failed', 'official_failed'}:
        return []
    if _qc._has_legacy_critic_repair_contract(ckpt, _qc._checkpoint_master_plan(ckpt).get('tasks', [])):
        return []
    feedback = str(reviewer_feedback or _qc._checkpoint_rework_feedback(ckpt) or '').strip()
    if not feedback:
        return []
    master_plan = _qc._checkpoint_master_plan(ckpt)
    is_precommit_rework = _qc._is_precommit_rework_checkpoint(ckpt)
    is_official_rework = _qc._is_official_rework_checkpoint(ckpt)
    is_review_rework = _qc._is_review_rework_checkpoint(ckpt)
    quality_contracts = [] if is_precommit_rework or is_official_rework or is_review_rework else _qc._quality_repair_contracts(ckpt, feedback)
    if is_precommit_rework:
        return _qc._precommit_repair_tasks(ckpt, feedback)
    elif is_official_rework:
        return _qc._official_repair_tasks(ckpt, feedback)
    elif is_review_rework:
        target_files = _qc._review_repair_target_files(ckpt, feedback)
    elif quality_contracts:
        target_files = [contract['file'] for contract in quality_contracts]
    elif reviewer_feedback:
        return []
    else:
        failures = _qc._quality_failure_items(ckpt)
        target_files = _qc._extract_quality_failure_files(failures)
        if not target_files:
            target_files = _qc._extract_quality_failure_files([feedback])
    if not target_files:
        return []
    targets = target_files
    is_crossover = bool(ckpt.get('parent2_v')) or master_plan.get('strategy') == 'crossover'
    if is_review_rework:
        preservation = 'This is a Lead Code Reviewer hard-gate repair. Preserve the current candidate in bots/{bot_name(next_v)}; fix the exact code-quality blocker named by the reviewer. Do not chase secondary notes unless they are required to resolve the primary blocker.'
        method = "- Read all listed target files and the quoted reviewer feedback before editing.\n- Resolve the primary rejected state coherently. If the feedback offers mutually exclusive paths, choose ONE complete path.\n- Do not leave defined-but-unwired helpers, misleading comments/docstrings, unused imports, or half-restored systems.\n- Keep the candidate's already-passing national protocol/card mapping behavior intact.\n- Run `python -m py_compile` on the exact edited file before finishing; system gates own imports and self-tests."
        worker_id = 'auto_review_repair'
        role = 'Algorithmic Logic Architect'
        task_kind = 'crossover_review_repair' if is_crossover else 'review_repair'
    elif is_crossover and stage in {'quality_failed', 'repair_planned', 'rework_running'}:
        preservation = "This is a crossover quality repair. Preserve the current candidate's crossover behavior in bots/{bot_name(next_v)}; fix only the blocking quality-gate issues unless a tiny local cleanup is required."
        method = '- Read the listed target files before editing.\n- For file_size blockers, remove dead/duplicated code or consolidate helper logic; do not weaken strategy by deleting active decisions blindly.\n- For position_semantics blockers, remove local seat derivation and read `decision_context.hand.position`/`acts_first_postflop` plus `decision_context.line.position` directly.\n- Do not change protocol/card mapping behavior outside the named blockers.\n- Leave stderr telemetry honest if touched.'
        worker_id = 'auto_quality_repair'
        role = 'Algorithmic Logic Architect'
        task_kind = 'quality_repair'
    else:
        preservation = 'This is a gate repair. Make the smallest structural correction that clears the listed blockers while preserving the intended strategy.'
        method = '- Read the listed target files before editing.\n- For file_size blockers, remove dead/duplicated code or consolidate helper logic; do not weaken strategy by deleting active decisions blindly.\n- For position_semantics blockers, remove local seat derivation and read `decision_context.hand.position`/`acts_first_postflop` plus `decision_context.line.position` directly.\n- Do not change protocol/card mapping behavior outside the named blockers.\n- Leave stderr telemetry honest if touched.'
        worker_id = 'auto_quality_repair'
        role = 'Algorithmic Logic Architect'
        task_kind = 'quality_repair'
    if quality_contracts:
        return _qc._order_quality_repair_tasks([_qc._quality_contract_task(contract, ckpt, preservation, task_kind) for contract in quality_contracts])
    prompt = f'{preservation.format(next_v=ckpt.get('next_v'))}\n\nExact gate feedback:\n{feedback}\n\nRequired method:\n{method}'
    repair_blocker = 'review_rejection' if is_review_rework else 'quality_gate'
    return [{'worker_id': worker_id, 'role': role, 'target_files': targets, 'must_change_files': targets, 'worker_prompt': prompt, 'task_kind': task_kind, 'repair_blocker': repair_blocker, 'repair_contract': {'blocker': repair_blocker, 'files': targets, 'evidence': feedback[:2000], 'source_stage': str(stage or '')}}]
