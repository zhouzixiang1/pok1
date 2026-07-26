"""Master-plan validation and architecture-policy build for tool_planning.

Extracted as a cohesive business cluster; tool_planning.py retains thin delegate
shells so external ``from tool_planning import <name>`` and
``monkeypatch.setattr(tool_planning, "<name>", ...)`` keep resolving.

Business responsibility
-----------------------
Normalize and validate Master's emitted plan: replay-citation auditing,
master plan structural/runtime-contract errors, and building the generation
architecture policy (the source-capability digest anchor).

Cross-reference policy
----------------------
* Calls to another MOVED function -> bare global (intra-companion).
* Calls to STAYING ``tool_planning`` module-level helpers / constants that
  tests monkeypatch on ``tool_planning.<name>`` -> routed through ``_tp.``
  so the monkeypatch is honored. This covers at minimum:
  ``get_bot_dir``, ``log_system_event``.
* Things provided directly by sibling modules (``bot_namespace``,
  ``output_schema``, ``tool_helpers``, ``runtime_architecture_policy``,
  ``evidence_snapshot``, ``national_capability_contract``,
  ``system_strict_bootstrap``, ``bot_artifact``, ``evolution_infra``,
  ``workflow_profiles``, ``strategy_reference_pack``) and NOT monkeypatched
  on ``tool_planning`` are imported directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys as _sys
from pathlib import Path

from bot_namespace import bot_name, bot_relpath
from output_schema import (
    MASTER_PLAN_MAX_TASKS,
    RuntimeContract,
    WORKER_PROMPT_MAX_CHARS,
    WORKER_TASK_MAX_TARGET_FILES,
    runtime_contract_is_required,
    runtime_contract_missing_sections,
    runtime_contract_required_sections,
    runtime_contract_worker_prompt_terms,
)
from tool_helpers import (
    PROJECT_ROOT,
    _target_rel,
    normalize_worker_role,
)

# Lazy reference back to tool_planning so monkeypatches on
# ``tool_planning.<name>`` are respected at call time. Imported after the rest
# of the top-level block above so the companion's own ``import tool_planning``
# resolves to the (mid-load, but already past its A-D header) parent module.
import tool_planning as _tp  # noqa: E402


# ---------------------------------------------------------------------------
# Parent-module symbol forwarding.
#
# ``get_bot_dir`` and ``log_system_event`` are monkeypatched on ``tool_planning``
# by the test suite (see ``monkeypatch.setattr(tool_planning, ...)`` audit).
# They must resolve through tool_planning at call time, never be snapshotted at
# import, so a test patch is observed on the next call. ``_TPCallableProxy``
# mirrors the same class in tool_planning_worker.py / tool_planning_quality_
# contracts.py.
# ---------------------------------------------------------------------------


class _TPCallableProxy:
    """Callable proxy that re-reads ``tool_planning.<name>`` on every call.

    Static analysis confirms every real usage of these names in the moved body
    is a plain call (zero attribute-access, zero bare non-call loads), so
    ``__call__`` plus attribute forwarding is sufficient.
    """

    __slots__ = ("_name",)

    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def _resolve(self):
        tp = _sys.modules.get("tool_planning")
        if tp is None:
            raise RuntimeError(
                "tool_planning is not initialized; _TPCallableProxy cannot resolve "
                + object.__getattribute__(self, "_name")
            )
        return getattr(tp, object.__getattribute__(self, "_name"))

    def __call__(self, *args, **kwargs):
        return object.__getattribute__(self, "_resolve")()(*args, **kwargs)

    def __getattr__(self, attr):
        return getattr(object.__getattribute__(self, "_resolve")(), attr)

    def __repr__(self):
        try:
            return repr(object.__getattribute__(self, "_resolve")())
        except Exception:
            return f"<_TPCallableProxy name={object.__getattribute__(self, '_name')!r}>"


# Names that test suites historically monkeypatch on ``tool_planning`` and that
# the moved body calls.  These resolve live through tool_planning so
# ``monkeypatch.setattr(tool_planning, <name>, ...)`` keeps working.
_MONKEYPATCHED_TP_SYMBOLS_MPV = (
    "get_bot_dir",
    "log_system_event",
)
for _name in _MONKEYPATCHED_TP_SYMBOLS_MPV:
    globals()[_name] = _TPCallableProxy(_name)
del _name


# Immutable parent-module data constant the moved body reads. Resolved lazily
# through ``_tp.`` inside the function body so it always tracks the value
# bound on ``tool_planning``.


# ---------------------------------------------------------------------------
# Module-level constants (only used by the functions in this cluster).
# ---------------------------------------------------------------------------

_TUNER_STRUCTURAL_PATTERNS = [
    "add parameter", "add a parameter", "function signature",
    "add function", "new function", "add method",
    "add class", "new class",
    "add import", "new import",
    "before the clamp", "after the existing",
]


# A4 (evidence_gate, evolution-plan-refresh-jun21): citation patterns the agents use
# to reference spotlight hands. Anchored form (G3H25#9a3f1c02) is preferred but the
# bare form (G3H25) is what fabricated citations usually look like.
_CITATION_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:G\d+H\d+|H\d+)(?:#[0-9a-fA-F]{8})?(?![A-Za-z0-9_])"
)


# ---------------------------------------------------------------------------
# Moved functions (originally tool_planning.py lines 1201-2024).
# ---------------------------------------------------------------------------


def _normalize_master_plan_paths(plan, source_v, next_v):
    """Rewrite parent bot paths in a Master plan to the target bot path.

    Master can inspect the source bot, but worker edit and verification paths
    must point at the prepared target directory. Keep the rewrite path-scoped so
    prose such as "national_v206 is weak vs underbets" remains intact.
    """
    meta = {
        "source_v": source_v,
        "next_v": next_v,
        "replacements": 0,
        "fields": [],
    }
    if not isinstance(plan, (dict, list)) or source_v is None or next_v is None:
        return plan, meta
    try:
        source_i = int(source_v)
        next_i = int(next_v)
    except (TypeError, ValueError):
        return plan, meta
    if source_i == next_i:
        return plan, meta

    source_bot = bot_name(source_i)
    target_bot = bot_name(next_i)
    rel_source = f"bots/{source_bot}"
    rel_target = f"bots/{target_bot}"
    win_source = f"bots\\{source_bot}"
    win_target = f"bots\\{target_bot}"
    abs_source = str(PROJECT_ROOT / "bots" / source_bot)
    abs_target = str(PROJECT_ROOT / "bots" / target_bot)
    abs_win_source = abs_source.replace("/", "\\")
    abs_win_target = abs_target.replace("/", "\\")

    literal_replacements = [
        (rel_source + "/", rel_target + "/"),
        (win_source + "\\", win_target + "\\"),
        (abs_source + "/", abs_target + "/"),
        (abs_win_source + "\\", abs_win_target + "\\"),
    ]
    quoted_dirs = [
        (rel_source, rel_target),
        (win_source, win_target),
        (abs_source, abs_target),
        (abs_win_source, abs_win_target),
    ]

    def replace_text(text):
        changed = 0
        out = text
        for src, dst in literal_replacements:
            n = out.count(src)
            if n:
                out = out.replace(src, dst)
                changed += n
        for src, dst in quoted_dirs:
            pattern = re.compile(rf"(?P<q>['\"]){re.escape(src)}(?P=q)")

            def _quoted(match, replacement=dst):
                return f"{match.group('q')}{replacement}{match.group('q')}"

            out, n = pattern.subn(_quoted, out)
            changed += n

            cd_pattern = re.compile(
                rf"(?P<prefix>\bcd\s+){re.escape(src)}"
                rf"(?P<suffix>\s*(?:&&|;|\||\n|$))"
            )
            out, n = cd_pattern.subn(
                lambda m, replacement=dst: (
                    f"{m.group('prefix')}{replacement}{m.group('suffix')}"
                ),
                out,
            )
            changed += n
        return out, changed

    def walk(value, path):
        if isinstance(value, str):
            new_value, count = replace_text(value)
            if count:
                meta["replacements"] += count
                meta["fields"].append(path)
            return new_value
        if isinstance(value, list):
            return [walk(item, f"{path}[{idx}]") for idx, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: walk(item, f"{path}.{key}") for key, item in value.items()}
        return value

    if isinstance(plan, dict):
        normalized = dict(plan)
        if "tasks" in normalized:
            normalized["tasks"] = walk(normalized["tasks"], "plan.tasks")
    else:
        normalized = walk(plan, "plan")
    if meta["fields"]:
        meta["fields"] = sorted(set(meta["fields"]))
    return normalized, meta


def _normalize_and_log_master_plan_paths(plan, source_v, next_v):
    normalized, meta = _normalize_master_plan_paths(plan, source_v, next_v)
    if meta.get("replacements", 0) > 0:
        try:
            log_system_event(
                "pipeline.master_plan_paths_normalized", "warn",
                f"Normalized {meta['replacements']} parent-path reference(s) "
                f"in Master plan v{next_v}: {bot_relpath(source_v)} -> "
                f"{bot_relpath(next_v)}",
                meta,
            )
        except Exception:
            pass
    return normalized


def _load_replay_anchor_map(next_v=None):
    """Load the generation snapshot citations as ``{base_id: anchor}``.

    Returns:
        dict mapping citation base ID (e.g. "G3H25") to anchor string, or
        ``None`` only for context-free utility calls with no generation id.
        A missing/corrupt generation snapshot returns an empty map so any
        citation fails closed.
    """
    if next_v is None:
        return None
    try:
        from evidence_snapshot import load_generation_evaluation_snapshot

        frozen = load_generation_evaluation_snapshot(int(next_v))
        if not frozen.get("available"):
            return {}
        spotlight = frozen.get("replay_spotlight")
        if not isinstance(spotlight, dict):
            return {}
    except Exception:
        return {}

    anchor_map = {}
    for citation in spotlight.get("citations", []):
        if not isinstance(citation, dict):
            continue
        base = str(citation.get("id") or "")
        anchor = str(citation.get("anchor") or "")
        if base and anchor:
            anchor_map[base] = anchor
    return anchor_map


def _check_citations(text_list, anchor_map):
    """Check text list for fabricated GxHx#anchor citations.

    Args:
        text_list: List of strings to check for citation patterns.
        anchor_map: Manifest of valid anchors.
                   None  = no manifest loaded (skip check, return []).
                   {}    = manifest loaded but empty = ALL citations fabricated.
                   {id: anchor, ...} = normal validation.

    Returns:
        List of error messages for fabricated citations.
    """
    if anchor_map is None:
        return []  # No manifest loaded, skip
    errors = []
    for text in text_list:
        for match in _CITATION_RE.finditer(text):
            ref = match.group(0)
            base = ref.split("#", 1)[0] if "#" in ref else ref
            if base not in anchor_map:
                errors.append(
                    f"FABRICATED_EVIDENCE: '{ref}' is NOT in the spotlight manifest "
                    f"(no such hand exists in recent replays). Only cite hands "
                    f"verbatim from the injected Replay Spotlight section "
                    f"(format: G<game>H<hand>#<anchor>)."
                )
            elif "#" in ref:
                cited_anchor = ref.split("#", 1)[1]
                expected = anchor_map.get(base, "")
                if expected and cited_anchor.lower() != expected.lower():
                    errors.append(
                        f"FABRICATED_EVIDENCE: '{ref}' anchor mismatch "
                        f"(expected #{expected}). Possible hallucination or "
                        f"tampering with a real hand id."
                    )
    return errors


def _sanitize_unverified_replay_citations(text, anchor_map):
    """Remove stale replay hand IDs from Master side context.

    The current replay spotlight is the only authoritative citation source for
    a generation. Direction-audit, match-analysis, research, or other advisory text
    can mention historical GxHy IDs from prior generations; if injected as-is,
    Master tends to repeat them and the evidence gate correctly rejects the
    plan. Keep valid current IDs, fix stale anchors, and redact invalid IDs
    before the text reaches Master.
    """
    if anchor_map is None or not isinstance(text, str) or not text:
        return text, 0

    count = 0

    def repl(match):
        nonlocal count
        ref = match.group(0)
        base = ref.split("#", 1)[0] if "#" in ref else ref
        if base not in anchor_map:
            count += 1
            return "unverified-replay-ref"
        if "#" in ref:
            cited_anchor = ref.split("#", 1)[1]
            expected = anchor_map.get(base, "")
            if expected and cited_anchor.lower() != expected.lower():
                count += 1
                return f"{base}#{expected}"
        return ref

    return _CITATION_RE.sub(repl, text), count


def _verify_cited_replays(plan, *, next_v=None):
    """A4 (evidence_gate): reject Master/Worker replay citations that don't
    correspond to any real replay hand in the spotlight manifest.

    Historical agents invented GxHx IDs that did not exist in the bound replay
    set. The pure spotlight builder now stores every emitted citation directly
    inside the immutable generation evidence snapshot; this function
    cross-checks the plan only against that snapshot.

    Returns a list of BLOCKING error strings. Fabricated evidence must not
    reach Workers.
    """
    anchor_map = _load_replay_anchor_map(next_v)
    tasks = plan if isinstance(plan, list) else (
        plan.get("tasks", []) if isinstance(plan, dict) else []
    )
    texts = []
    for i, task in enumerate(tasks or []):
        if not isinstance(task, dict):
            continue
        texts.append(" ".join([
            str(task.get("worker_prompt", "")),
            str(task.get("instruction", "")),
            str(task.get("targeted_failure", "")),
        ]))
    return _check_citations(texts, anchor_map)


def _validate_master_plan(
    plan,
    next_v=None,
):
    """Validate master plan constraints before dispatching workers.

    Returns (errors, warnings) — only errors block plan storage.
    Boundary warnings are logged but non-blocking; the reviewer/critic
    enforce actual role boundaries during code review.

    """
    errors = []
    warnings = []
    tasks = plan.get("tasks", [])
    if len(tasks) > MASTER_PLAN_MAX_TASKS:
        errors.append(
            f"Too many tasks: {len(tasks)} > {MASTER_PLAN_MAX_TASKS}"
        )
    for i, task in enumerate(tasks):
        targets = task.get("target_files", [])
        files_allowed = task.get("files_allowed", []) or []
        if len(targets) > WORKER_TASK_MAX_TARGET_FILES:
            errors.append(
                f"Task {i}: too many target_files "
                f"({len(targets)} > {WORKER_TASK_MAX_TARGET_FILES})"
            )
        prompt = task.get("worker_prompt", "")
        if len(prompt) > WORKER_PROMPT_MAX_CHARS:
            errors.append(
                f"Task {i}: worker_prompt too long "
                f"({len(prompt)} > {WORKER_PROMPT_MAX_CHARS} chars)"
            )
        layer = str(task.get("skill_layer", "") or "").strip()
        errors.extend(_runtime_contract_errors(task, i, layer))
        declared_rels = {
            (
                _target_rel(item, next_v)
                if next_v is not None
                else Path(str(item)).name
            )
            for item in [*targets, *files_allowed]
            if str(item).strip()
        }
        if declared_rels != _tp._ACTIVE_CANDIDATE_WRITABLE_FILES:
            errors.append(
                f"Task {i}: national_tcp_policy_v1 writable scope must be exactly "
                f"['policy.py']; got {sorted(declared_rels)}. System files, helper "
                "modules, candidate-owned assets, and unbound external assets are not "
                "Worker targets."
            )
        role = str(task.get("role", ""))
        if normalize_worker_role(role) == "tuner":
            # All roles share the sole candidate artifact; Tuner scope is
            # semantic (existing numeric values only), not a separate module.
            tuner_only_files = _tp._ACTIVE_CANDIDATE_WRITABLE_FILES
            declared_files = list(targets) + list(files_allowed)
            non_tuner_files = [t for t in declared_files if Path(str(t)).name not in tuner_only_files]
            if non_tuner_files:
                errors.append(
                    f"Task {i}: Hyperparameter Tuner declares non-policy file(s) {non_tuner_files}; "
                    "all candidate edits must remain in policy.py."
                )
            prompt_lower = prompt.lower()
            # Skip structural keywords that appear in constraint/negative contexts
            _skip_contexts = ("do not", "don't", "must not", "never", "preserve",
                              "keep", "unchanged", "maintain", "no new", "forbidden",
                              "avoid", "except", "aside from", "other than",
                              "should not", "cannot", "do not change", "do not add")
            for kw in _TUNER_STRUCTURAL_PATTERNS:
                # Find the keyword in context — skip if it's in a constraint sentence
                idx = prompt_lower.find(kw)
                if idx >= 0:
                    # Check surrounding context (200 chars before) for negative cues
                    context_before = prompt_lower[max(0, idx - 200):idx]
                    if any(cue in context_before for cue in _skip_contexts):
                        continue
                    # Keyword found in an affirmative (structural) context — warn only
                    warnings.append(
                        f"Task {i} boundary warning: Hyperparameter Tuner prompt contains structural instruction "
                        f"'{kw}' — Tuner should only change numeric constants. "
                        f"The reviewer/critic will enforce this boundary."
                    )
                    break

    # tasks 校验之后：禁止 Master 自行指定 source override 字段。
    # Source ancestor 由系统在 prepare_generation (generation_scheduler._decide_strategy)
    # 决定，Master 不得设置；否则为永不生效的死字段（写 checkpoint 后从不读取）。
    # 注意：本检查必须在 Pydantic (MasterPlan.model_validate, extra='ignore')
    # 剥离 branch_from 之前对原始 dict 调用，否则该键已被丢弃、检查永不命中。
    # 见 _run_master_analysis (agent_master.py) 中 validate_agent_output 之前的
    # 原始 dict 预检。
    source_override_fields = ("branch_from", "source_override", "source_v_override")
    offending = [f for f in source_override_fields if plan.get(f)]
    if offending:
        errors.append(
            f"Master plan must not set source-override field(s) {offending}. "
            f"Source ancestor selection is decided automatically in "
            f"prepare_generation (generation_scheduler._decide_strategy); "
            f"Master must not set branch_from."
        )

    # Check target_files overlap between workers.
    # Architect-Tuner overlap on any file is a hard error (causes boundary false positives).
    # Other overlaps are informational — workers execute sequentially so overlap is safe,
    # but different files make each worker's scope clearer.
    architect_targets = {}
    tuner_targets = {}
    all_targets = {}
    for i, task in enumerate(tasks):
        role = str(task.get("role", ""))
        _role_kind = normalize_worker_role(role)
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v) if next_v else target.strip()
            if _role_kind == "architect":
                architect_targets.setdefault(rel, []).append(i)
            elif _role_kind == "tuner":
                tuner_targets.setdefault(rel, []).append(i)
            if rel in all_targets:
                warnings.append(
                    f"Tasks {all_targets[rel]} and {i} share target_file '{target}'. "
                    f"This is safe (sequential execution) but consider splitting for clarity."
                )
            else:
                all_targets[rel] = i

    # All active tasks necessarily share policy.py. Per-worker snapshots isolate
    # boundary review, and the executor serializes overlapping tasks.
    overlap = set(architect_targets.keys()) & set(tuner_targets.keys())
    if overlap:
        warnings.append(
            f"Architect and Tuner share the sole policy target {sorted(overlap)}; "
            "execute sequentially and audit each worker against its own snapshot."
        )

    # BLOCKING: reject replay hands that do not exist in this generation's
    # digest-bound spotlight payload. A plan built on invented evidence must
    # not reach Workers.
    try:
        errors.extend(_verify_cited_replays(plan, next_v=next_v))
    except Exception:
        pass  # never let the gate itself crash the pipeline

    try:
        from runtime_architecture_policy import validate_plan_architecture_focus
        errors.extend(validate_plan_architecture_focus(plan))
    except Exception as exc:
        if isinstance(plan, dict) and isinstance(plan.get("architecture_policy"), dict):
            errors.append(
                f"Architecture focus validation failed closed: {type(exc).__name__}: {str(exc)[:200]}"
            )

    return errors, warnings


def _runtime_contract_errors(task: dict, index: int, layer: str) -> list[str]:
    """Return hard Master-plan errors for runtime-architecture task contracts."""
    focus_id = str(task.get("architecture_focus_id") or "").strip()
    if not runtime_contract_is_required(layer, focus_id):
        return []

    contract = task.get("runtime_contract")
    if not isinstance(contract, dict):
        return [
            f"Task {index}: runtime_contract is required for skill_layer={layer!r}. "
            "Declare decision, precompute_artifacts, match_memory, and "
            "official_feedback_refs as applicable, and mirror "
            "the concrete work into worker_prompt."
        ]

    try:
        validated = RuntimeContract.model_validate(contract)
    except Exception as exc:
        details: list[str] = []
        if hasattr(exc, "errors"):
            for item in exc.errors()[:8]:
                location = ".".join(str(part) for part in item.get("loc") or [])
                details.append(f"{location}: {item.get('msg')}")
        else:
            details.append(str(exc))
        return [
            f"Task {index}: runtime_contract schema invalid: {'; '.join(details)}"
        ]

    required_sections = runtime_contract_required_sections(layer, focus_id)
    missing = runtime_contract_missing_sections(validated, required_sections)
    if missing:
        return [
            f"Task {index}: runtime_contract for skill_layer={layer!r} is missing "
            f"{', '.join(missing)}"
        ]

    writable_scope = {
        Path(str(item)).name
        for item in [
            *(task.get("target_files") or []),
            *(task.get("files_allowed") or []),
        ]
        if str(item).strip()
    }
    read_only_scope = {
        Path(str(item)).name
        for item in task.get("read_only_dependencies") or []
        if str(item).strip()
    }
    overlap = sorted(writable_scope.intersection(read_only_scope))
    if overlap:
        return [
            f"Task {index}: read_only_dependencies overlap writable "
            f"target_files/files_allowed: {overlap}"
        ]
    owners = []
    if validated.match_memory is not None:
        owners.append(validated.match_memory.owner_file)
    owners.extend(item.owner_file for item in validated.precompute_artifacts)
    invalid_precompute_owners = sorted({
        item.owner_file
        for item in validated.precompute_artifacts
        if item.owner_file != "precompute.py"
    })
    if invalid_precompute_owners:
        return [
            f"Task {index}: precompute artifacts must be existing read-only "
            f"precompute.py objects, got owners {invalid_precompute_owners}."
        ]
    if (
        validated.match_memory is not None
        and validated.match_memory.owner_file != "national_bot.py"
    ):
        return [
            f"Task {index}: match memory is owned by read-only national_bot.py, "
            f"got {validated.match_memory.owner_file!r}."
        ]
    missing_owners = sorted({
        owner
        for owner in owners
        if owner not in writable_scope and owner not in read_only_scope
    })
    if missing_owners:
        return [
            f"Task {index}: runtime_contract owner file(s) {missing_owners} are outside "
            "the declared writable/read-only scope: "
            f"writable={sorted(writable_scope)}, read_only={sorted(read_only_scope)}."
        ]
    # national_bot.py and precompute.py can only be declared read-only; their
    # content failures are system/infrastructure failures, never Worker repairs.

    state_learning = validated.state_learning
    if state_learning is not None:
        missing_checks = sorted(
            set(state_learning.primary_checks()).difference(
                str(item) for item in task.get("checks_required") or []
            )
        )
        if missing_checks:
            return [
                f"Task {index}: state_learning primary innovation "
                f"{state_learning.primary_innovation()!r} requires checks_required "
                f"{missing_checks}."
            ]
        if (
            state_learning.work_primitive == "sample_counted_candidate_batch"
            and validated.decision is None
        ):
            return [
                f"Task {index}: sample_counted_candidate_batch requires a decision contract."
            ]
        if state_learning.work_primitive is not None:
            from strategy_reference_pack import validate_reference_task

            reference_errors = validate_reference_task(
                validated.reference_pack_id,
                state_learning.primary_innovation(),
                target_files=[
                    *(task.get("target_files") or []),
                    *(task.get("files_allowed") or []),
                ],
                worker_prompt=str(task.get("worker_prompt", task.get("instruction", ""))),
            )
            if reference_errors:
                return [f"Task {index}: {error}" for error in reference_errors]

    prompt = str(task.get("worker_prompt", task.get("instruction", ""))).lower()
    contract_terms = runtime_contract_worker_prompt_terms(validated)
    missing_terms = [term for term in contract_terms if term not in prompt]
    if missing_terms:
        return [
            f"Task {index}: runtime_contract is declared but worker_prompt does not "
            f"mention required execution term(s) {missing_terms}. Mirror every contract "
            "boundary into the worker instructions so it reaches the implementation."
        ]
    return []


def _build_generation_architecture_policy(
    source_v: int,
    *,
    prepared_capability_snapshot: dict | None = None,
    prepared_dir: Path | None = None,
    allow_lineage_only_source: bool = False,
) -> dict:
    """Assess and build the system-owned policy for a native source artifact."""

    from workflow_profiles import get_workflow_profile

    profile = get_workflow_profile()
    if getattr(profile, "national_execution_mode", "") != "native_tcp":
        return {"outcome": "skipped", "policy": None, "capabilities": None}
    if allow_lineage_only_source:
        # The only path-free lineage exception is the one-time v142 -> v143
        # empty-pool bootstrap.  Do not establish it by checking whether a
        # historical directory happens to exist: stale local debris must have
        # exactly zero influence.
        from bot_namespace import (
            ARCHIVED_VERSION_HIGH_WATER,
            FIRST_STRICT_POLICY_VERSION,
        )
        from runtime_architecture_policy import (
            build_lineage_only_architecture_policy,
            lineage_only_capabilities,
            validate_prepared_capability_snapshot,
        )

        target_dir = Path(prepared_dir) if prepared_dir is not None else None
        source_identity = bot_name(source_v)
        snapshot_errors = []
        if int(source_v) != int(ARCHIVED_VERSION_HIGH_WATER):
            snapshot_errors.append("lineage_only_source_not_archived_high_water")
        if target_dir is None or target_dir.name != bot_name(
            FIRST_STRICT_POLICY_VERSION
        ):
            snapshot_errors.append("lineage_only_target_not_first_strict")
        if not isinstance(prepared_capability_snapshot, dict):
            snapshot_errors.append("lineage_only_prepared_snapshot_missing")
        else:
            snapshot_errors.extend(validate_prepared_capability_snapshot(
                prepared_capability_snapshot,
                lineage_parent_bot=source_identity,
                prepared_bot_dir=target_dir,
            ))
        if snapshot_errors:
            return {
                "outcome": "infrastructure_failure",
                "policy": None,
                "capabilities": None,
                "infrastructure_failures": [{
                    "component": "fresh_bootstrap_architecture_policy",
                    "failure_class": "internal_infrastructure",
                    "issues": list(dict.fromkeys(snapshot_errors))[:20],
                }],
            }
        try:
            policy = build_lineage_only_architecture_policy(
                source_identity,
                prepared_capability_snapshot=prepared_capability_snapshot,
            )
        except Exception as exc:
            return {
                "outcome": "infrastructure_failure",
                "policy": None,
                "capabilities": None,
                "infrastructure_failures": [{
                    "component": "fresh_bootstrap_architecture_policy",
                    "failure_class": "internal_infrastructure",
                    "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
                }],
            }
        return {
            "outcome": "passed",
            "policy": policy,
            "capabilities": lineage_only_capabilities(),
        }

    source_dir = get_bot_dir(source_v)
    if not (source_dir / "national_bot.py").exists():
        return {
            "outcome": "source_invalid",
            "policy": None,
            "capabilities": None,
            "issues": [f"{source_dir.name}/national_bot.py is missing"],
        }
    from national_capability_contract import evaluate_national_capabilities
    from runtime_architecture_policy import build_architecture_policy

    try:
        capabilities = evaluate_national_capabilities(source_dir)
    except Exception as exc:
        return {
            "outcome": "infrastructure_failure",
            "policy": None,
            "capabilities": None,
            "infrastructure_failures": [{
                "component": "national_runtime_probe",
                "failure_class": "internal_infrastructure",
                "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
            }],
        }
    infrastructure_failures = capabilities.get("infrastructure_failures") or []
    if capabilities.get("outcome") == "infrastructure_failure" or infrastructure_failures:
        return {
            "outcome": "infrastructure_failure",
            "policy": None,
            "capabilities": capabilities,
            "infrastructure_failures": infrastructure_failures or [{
                "component": "national_runtime_probe",
                "failure_class": "internal_infrastructure",
                "issues": ["source capability probe was inconclusive"],
            }],
        }
    try:
        policy = build_architecture_policy(
            source_dir,
            source_capabilities=capabilities,
            prepared_capability_snapshot=prepared_capability_snapshot,
        )
    except Exception as exc:
        return {
            "outcome": "infrastructure_failure",
            "policy": None,
            "capabilities": capabilities,
            "infrastructure_failures": [{
                "component": "runtime_architecture_policy",
                "failure_class": "internal_infrastructure",
                "issues": [f"{type(exc).__name__}: {str(exc)[:300]}"],
            }],
        }
    return {"outcome": "passed", "policy": policy, "capabilities": capabilities}


def _master_snapshot_binding_errors(checkpoint, next_v):
    """Verify every post-selection Master read uses the selected cutoff."""
    if not isinstance(checkpoint, dict):
        return ["master_checkpoint_missing"]
    audit_context = checkpoint.get("audit_context") or {}
    selection = audit_context.get("selection") or {}
    if selection.get("bootstrap_without_strength_evidence") is True:
        errors = []
        receipt = audit_context.get("protocol_bootstrap")
        try:
            from evolution_infra import get_active_bots
            active_bots = list(get_active_bots())
            if (
                isinstance(receipt, dict)
                and receipt.get("mode") == "fresh_national_policy_bootstrap"
            ):
                from system_strict_bootstrap import validate_fresh_bootstrap_receipt

                errors.extend(validate_fresh_bootstrap_receipt(
                    receipt, active_bots=active_bots
                ))
            else:
                from bot_artifact import canonical_digest

                unsigned = {
                    key: value for key, value in (receipt or {}).items()
                    if key != "receipt_digest"
                }
                if not isinstance(receipt, dict) or receipt.get(
                    "receipt_digest"
                ) != canonical_digest(unsigned):
                    errors.append("policy_bootstrap_receipt_digest_mismatch")
                if sorted((receipt or {}).get("active_bots") or []) != sorted(active_bots):
                    errors.append("policy_bootstrap_active_pool_mismatch")
        except Exception as exc:
            errors.append(
                f"protocol_bootstrap_validation_error:{type(exc).__name__}:"
                f"{str(exc)[:160]}"
            )
        prepare = audit_context.get("protocol_bootstrap_prepare")
        if not isinstance(prepare, dict):
            errors.append("protocol_bootstrap_prepare_receipt_missing")
            return errors
        if not isinstance(receipt, dict) or prepare.get("receipt_digest") != receipt.get(
            "receipt_digest"
        ):
            errors.append("protocol_bootstrap_prepare_receipt_digest_mismatch")
        candidate_dir = get_bot_dir(next_v)
        entry = candidate_dir / "national_bot.py"
        try:
            actual_entry_hash = hashlib.sha256(entry.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(f"protocol_bootstrap_runtime_unreadable:{type(exc).__name__}")
        else:
            if prepare.get("national_bot_sha256") != actual_entry_hash:
                errors.append("protocol_bootstrap_runtime_hash_mismatch")
        if (
            isinstance(receipt, dict)
            and receipt.get("mode") == "fresh_national_policy_bootstrap"
            and prepare.get("system_runtime_replaced") is not True
        ):
            errors.append("protocol_bootstrap_system_runtime_not_replaced")
        try:
            from bot_namespace import (
                NATIONAL_RUNTIME_MANIFEST,
                POLICY_EPOCH_RECEIPT,
                epoch_receipt_errors,
                runtime_manifest_errors,
            )
            runtime_manifest = json.loads(
                (candidate_dir / NATIONAL_RUNTIME_MANIFEST).read_text(encoding="utf-8")
            )
            epoch_receipt = json.loads(
                (candidate_dir / POLICY_EPOCH_RECEIPT).read_text(encoding="utf-8")
            )
            errors.extend(
                "protocol_bootstrap_candidate_contract:" + item
                for item in [
                    *runtime_manifest_errors(candidate_dir, runtime_manifest),
                    *epoch_receipt_errors(
                        candidate_dir, int(next_v), runtime_manifest, epoch_receipt
                    ),
                ]
            )
        except Exception as exc:
            errors.append(
                f"protocol_bootstrap_candidate_contract_error:{type(exc).__name__}"
            )
        return errors
    formal_binding = bool(
        audit_context.get("master_context") is not None
        or selection.get("evaluation_evidence") is not None
        or selection.get("h2h_snapshot_manifest_digest")
    )
    if not formal_binding:
        # Legacy fixtures/checkpoints have no selected-evidence contract. The
        # strict downstream loader still cannot create a replacement cutoff.
        return []
    try:
        from evidence_snapshot import load_generation_snapshot_identity

        snapshot = load_generation_snapshot_identity(next_v)
    except Exception as exc:
        return [f"generation_snapshot_read_failed:{type(exc).__name__}"]
    if not snapshot.get("available"):
        return [
            "generation_snapshot_unavailable:"
            f"{snapshot.get('reason', 'unknown')}"
        ]
    errors = []
    expected_manifest = str(selection.get("h2h_snapshot_manifest_digest") or "")
    expected_sha = str(selection.get("h2h_snapshot_sha256") or "")
    evidence_cutoffs = (selection.get("evaluation_evidence") or {}).get("cutoffs") or {}
    expected_cycle = str(evidence_cutoffs.get("cycle_manifest_digest") or "")
    if not expected_manifest:
        errors.append("checkpoint_snapshot_manifest_digest_missing")
    elif expected_manifest != str(snapshot.get("manifest_digest") or ""):
        errors.append("checkpoint_snapshot_manifest_digest_mismatch")
    if not expected_sha:
        errors.append("checkpoint_snapshot_h2h_sha256_missing")
    elif expected_sha != str(snapshot.get("sha256") or ""):
        errors.append("checkpoint_snapshot_h2h_sha256_mismatch")
    actual_cycle = str((snapshot.get("cycle") or {}).get("manifest_digest") or "")
    if not expected_cycle:
        errors.append("checkpoint_cycle_manifest_digest_missing")
    elif expected_cycle != actual_cycle:
        errors.append("checkpoint_cycle_manifest_digest_mismatch")
    return errors
