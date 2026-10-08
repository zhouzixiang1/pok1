"""Deterministic worker-role policy: lead review, role rules, repair roles.

Root fix (2026-10-08) for the v525-v530 ``quality_rework_circuit_breaker``
chain. Three defects are removed at the architecture level:

* D1 — the LLM Worker-CoT audit no longer owns ANY direct block/rollback
  authority. Its verdicts are *leads*; :func:`review_worker_cot_lead` is
  the deterministic reviewer that confirms or downgrades each lead against
  the task role and the per-worker diff facts. Only a confirmed lead may
  enter the existing ``_reset_target_files_to_source`` rollback path.
* D2 — the role → allowed-change-shape mapping lives in ONE authoritative
  code table (:data:`ROLE_CHANGE_CATEGORIES`). The CoT prompt's role rules
  are rendered from the table per role
  (:func:`render_role_boundary_rules`); unenumerated roles get an explicit
  default rule, and the prompt carries no verbatim verdict examples for the
  audit LLM to parrot.
* D3 — repair-contract role assignment is evidence-driven
  (:func:`classify_repair_role_hint`), and an unsatisfiable
  constants-only-tuner × structural-blocker contract is flipped to an
  Architect-family role BEFORE dispatch
  (:func:`repair_contract_satisfiable` /
  :func:`enforce_repair_contract_satisfiability`).

True positives are preserved with a STRONGER evidence standard: a real
Tuner boundary violation blocks exactly when the deterministic gate's own
judgment (``normalize_worker_role(role) == 'tuner'`` and a non-numbers-only
per-worker diff) holds; a runtime side effect blocks only when the diff's
added lines themselves carry a side-effect token; a task mismatch blocks
only when every declared target is provably unchanged. Every existing
deterministic gate (``_validate_worker_boundaries``, must-change,
AST/capability/native contracts) keeps its semantics unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re

from tool_helpers import _numbers_only_changed, normalize_worker_role

__all__ = [
    "ROLE_CHANGE_CATEGORIES",
    "DEFAULT_CHANGE_CATEGORIES",
    "CotLeadVerdict",
    "RepairContractDecision",
    "allowed_change_categories",
    "render_role_boundary_rules",
    "diff_added_lines",
    "classify_diff_change_categories",
    "review_worker_cot_lead",
    "classify_repair_role_hint",
    "repair_contract_satisfiable",
    "repair_contract_flip_decision",
    "enforce_repair_contract_satisfiability",
    "role_policy_digest",
    "normalize_worker_role",
]


# ─────────────────────────────────────────────────────────────────────────────
# R2 — the single authoritative role → allowed-change-category table
# ─────────────────────────────────────────────────────────────────────────────

#: Change categories:
#:   numeric_constants — only numeric literal values differ
#:   structure         — control flow / statements / identifiers changed
#:   imports           — import lines changed
#:   new_code          — new functions / classes / statements added
ROLE_CHANGE_CATEGORIES = {
    "tuner": frozenset({"numeric_constants"}),
    "architect": frozenset(
        {"numeric_constants", "structure", "imports", "new_code"}
    ),
}

#: Every role that is not a Tuner variant (including all unenumerated and
#: future roles — Opponent Modeler, the Repair Architect family, anything
#: the planning layer coins next) shares the architect bucket by default:
#: non-Tuner roles carry no prompt-level change-shape restriction, their
#: boundaries are enforced by the deterministic gates.
DEFAULT_CHANGE_CATEGORIES = ROLE_CHANGE_CATEGORIES["architect"]

_ROLE_POLICY_SCHEMA = 1


def allowed_change_categories(role):
    """Return the authoritative allowed-change-category set for ``role``."""
    if not isinstance(role, str):
        role = str(role or "")
    if normalize_worker_role(role) == "tuner":
        return ROLE_CHANGE_CATEGORIES["tuner"]
    if normalize_worker_role(role) == "architect":
        return ROLE_CHANGE_CATEGORIES["architect"]
    return DEFAULT_CHANGE_CATEGORIES


_ADJUDICATION_NOTE = (
    "You only REPORT suspicions as leads; the system's deterministic "
    "reviewer adjudicates enforcement (block/rollback) against the task "
    "role and the actual diff."
)


def _tuner_rule_text(role):
    return (
        f"- {role}: the only allowed change category is `numeric_constants` "
        "— edits are limited to the values of existing numeric constants "
        "(thresholds, multipliers, caps). New identifiers, control-flow "
        "edits, imports, functions, classes, or comment-code rewrites are "
        "outside this role's shape.\n"
    )


def _architect_rule_text(role):
    return (
        f"- {role}: allowed change categories are `numeric_constants`, "
        "`structure`, `imports`, and `new_code`. Structural edits and "
        "numeric constant adjustments are BOTH in scope for this role; "
        "changing a constant does not by itself violate this role's "
        "boundary.\n"
    )


def _default_rule_text(role):
    return (
        f"- {role}: no prompt-level change-shape restriction is enumerated "
        "for this role. The default (non-Tuner) category set applies "
        "(`numeric_constants`, `structure`, `imports`, `new_code`); role "
        "boundaries are adjudicated by the system's deterministic "
        "reviewer, not by this audit.\n"
    )


def render_role_boundary_rules(role):
    """Render the CoT prompt's role-boundary rules section for ``role``.

    The rendering is a pure function of the authoritative table: the Tuner
    bucket gets the constants-only rule, every other role (enumerated
    architect or any unenumerated/future role) gets an explicit rule naming
    its category set. No verbatim verdict examples are ever rendered.
    """
    role = str(role or "").strip() or "Worker"
    category = normalize_worker_role(role)
    if category == "tuner":
        body = _tuner_rule_text(role)
    elif category == "architect":
        body = _architect_rule_text(role)
    else:
        body = _default_rule_text(role)
    return body + f"- {_ADJUDICATION_NOTE}\n"


def role_policy_digest():
    """Content-bound sha256 over the authoritative role table.

    Audit prompt provenance carries this digest so any change to the table
    changes the rendered-prompt identity.
    """
    payload = {
        "schema": _ROLE_POLICY_SCHEMA,
        "roles": {
            name: sorted(categories)
            for name, categories in ROLE_CHANGE_CATEGORIES.items()
        },
        "default": sorted(DEFAULT_CHANGE_CATEGORIES),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Diff shape analysis
# ─────────────────────────────────────────────────────────────────────────────

#: Side-effect CODE matcher for ADDED DIFF LINES (F1 fix, adversarial audit
#: 2026-10-09). The historic ``_COT_RUNTIME_SIDE_EFFECT_RE`` word list
#: (which includes the bare words ``telemetry``/``debug``) stays in use for
#: LEAD detection on the audit text, but the DIFF CONFIRMATION layer must
#: prove a real side-effect call/reference in code: runtime-architecture
#: repair contracts explicitly REQUIRE telemetry prose ("prove a typed-intent
#: counterfactual plus telemetry"), so a comment/identifier/string mention
#: must never confirm a lead (v526: comment "# Reducer-owned action_profile
#: telemetry: ..." × verdict focus prose "not only refinement telemetry"
#: rolled back a correct repair under the substring matcher). Bare
#: ``telemetry``/``debug`` words are therefore NOT diff-confirming tokens;
#: the deterministic native telemetry/protected-contract gates own that
#: dimension. Comment parts of a line are stripped before matching.
_DIFF_SIDE_EFFECT_CODE_RE = re.compile(
    r"(?:\bprint\s*\("
    r"|\blogging\s*\.\s*\w+\s*\("
    r"|\bsys\s*\.\s*std(?:err|out)\b"
    r"|_sys\s*\.\s*std(?:err|out)\b)"
)


def _code_part_of_line(line):
    """Return the code part of one added diff line (comment stripped).

    A pure comment line yields ``''``. A trailing comment is cut at the
    first ``#`` that follows whitespace; a ``#`` inside a string literal
    without preceding whitespace stays (conservative in both directions
    and matches how this codebase writes comments).
    """
    text = str(line or "")
    stripped = text.lstrip()
    if stripped.startswith("#"):
        return ""
    code = re.split(r"\s#", text, maxsplit=1)[0]
    return code.rstrip()

_ADDED_LINE_BUDGET = 500


def diff_added_lines(before, after):
    """Return the diff's added lines (without the leading ``+``), bounded."""
    import difflib

    added = []
    for line in difflib.unified_diff(
        str(before or "").splitlines(),
        str(after or "").splitlines(),
        lineterm="",
    ):
        if line.startswith("+++") or not line.startswith("+"):
            continue
        added.append(line[1:])
        if len(added) >= _ADDED_LINE_BUDGET:
            break
    return added


def classify_diff_change_categories(before, after):
    """Classify the shape of one file's before→after text change.

    Returns a frozenset drawn from ``numeric_constants`` / ``structure`` /
    ``runtime_side_effect`` (empty when nothing changed). ``numeric_constants``
    reuses the deterministic comparator the Tuner boundary gate uses
    (``tool_helpers._numbers_only_changed``); any non-numeric change implies
    ``structure``; an added line carrying a side-effect token implies
    ``runtime_side_effect``.
    """
    before_text = str(before or "")
    after_text = str(after or "")
    if before_text == after_text:
        return frozenset()
    categories = set()
    if _numbers_only_changed(before_text, after_text):
        categories.add("numeric_constants")
    else:
        categories.add("structure")
    for line in diff_added_lines(before_text, after_text):
        if _DIFF_SIDE_EFFECT_CODE_RE.search(_code_part_of_line(line)):
            categories.add("runtime_side_effect")
            break
    return frozenset(categories)


# ─────────────────────────────────────────────────────────────────────────────
# R1 — deterministic CoT lead reviewer
# ─────────────────────────────────────────────────────────────────────────────

#: Lead-text matcher for undisclosed runtime side effects (historic word
#: list, moved from agent_workers; used ONLY to detect the lead — blocking
#: additionally requires diff confirmation).
_COT_RUNTIME_SIDE_EFFECT_RE = re.compile(
    r"(stderr|stdout|sys\.stderr|_sys\.stderr|telemetry|debug|logging|"
    r"print\(|runtime\s+side[- ]effect|side[- ]effect|unconditional\s+log)",
    re.IGNORECASE,
)

#: Lead-text matcher for severe task mismatches (historic word list, moved
#: from agent_workers; blocking additionally requires all-unchanged proof).
_COT_TASK_MISMATCH_RE = re.compile(
    r"(assigned\s+task|task\s+was|task\s+steps?|diff|changed_functions|"
    r"worker'?s\s+changed|actual\s+surface\s+area).{0,240}"
    r"(performs?\s+none|none\s+of\s+these|does\s+not\s+implement|"
    r"not\s+implemented|revers(?:e|es|ed|ing)|inverted|opposite|"
    r"omits?|omitting|undisclosed|larger\s+and\s+more\s+invasive)",
    re.IGNORECASE | re.DOTALL,
)

_LEAD_NOTE_BUDGET = 8
_LEAD_NOTE_TEXT_BUDGET = 240


class CotLeadVerdict:
    """Deterministic review outcome for one LLM CoT inconsistency verdict."""

    __slots__ = ("block", "reason", "advisory_notes", "downgrade_reasons",
                 "lead_kinds")

    def __init__(self, block, reason, advisory_notes, downgrade_reasons,
                 lead_kinds):
        self.block = bool(block)
        self.reason = str(reason or "")
        self.advisory_notes = [str(item) for item in advisory_notes or ()]
        self.downgrade_reasons = [
            str(item) for item in downgrade_reasons or ()
        ]
        self.lead_kinds = [str(item) for item in lead_kinds or ()]

    def to_dict(self):
        return {
            "block": self.block,
            "reason": self.reason,
            "advisory_notes": list(self.advisory_notes),
            "downgrade_reasons": list(self.downgrade_reasons),
            "lead_kinds": list(self.lead_kinds),
        }

    def __repr__(self):  # pragma: no cover - diagnostic helper
        return (
            f"CotLeadVerdict(block={self.block!r}, reason={self.reason!r}, "
            f"downgrade_reasons={self.downgrade_reasons!r})"
        )


def _cot_lead_text(cot):
    if not isinstance(cot, dict):
        return "", []
    parts = []
    items = []
    for key in (
        "discrepancies",
        "logical_contradictions",
        "boundary_violations",
        "focus_areas",
    ):
        value = cot.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
            items.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
            items.append(str(value))
    return "\n".join(parts), items


def _normalized_target_names(task):
    names = []
    for target in task.get("target_files", []) or []:
        name = str(target).rstrip("/").rsplit("/", 1)[-1]
        if name and name not in names:
            names.append(name)
    return names


def _fact_entries_for_targets(diff_facts, names):
    entries = []
    for name in names:
        for key, entry in (diff_facts or {}).items():
            if str(key).rsplit("/", 1)[-1] == name:
                entries.append(entry)
                break
    return entries


def _diff_changed_non_numeric(diff_facts):
    for entry in (diff_facts or {}).values():
        if not isinstance(entry, dict):
            continue
        if entry.get("changed") and entry.get("numbers_only") is False:
            return True
    return False


def _diff_added_side_effect_lines(diff_facts):
    for entry in (diff_facts or {}).values():
        if not isinstance(entry, dict):
            continue
        for line in entry.get("added_lines") or []:
            if _DIFF_SIDE_EFFECT_CODE_RE.search(_code_part_of_line(line)):
                return True
    return False


def _all_targets_provably_unchanged(task, diff_facts):
    names = _normalized_target_names(task)
    if not names:
        return False
    entries = _fact_entries_for_targets(diff_facts, names)
    if len(entries) != len(names):
        # Facts do not cover every declared target — "nothing changed" is
        # not provable, and the must-change/boundary gates stay the
        # authority for the uncovered files.
        return False
    return all(not entry.get("changed") for entry in entries)


def review_worker_cot_lead(task, cot, diff_facts):
    """Deterministically re-review an inconsistent Worker CoT verdict.

    Decision matrix (R1):

    * **boundary lead** (typed ``boundary_violations`` non-empty) blocks
      only when the task role normalizes to ``tuner`` AND the worker's diff
      is not numbers-only — the exact judgment the deterministic
      ``_validate_worker_boundaries`` gate makes for real Tuner escapes.
    * **runtime-side-effect lead** blocks only when the diff's added lines
      themselves carry a side-effect token.
    * **task-mismatch lead** blocks only when every declared target file is
      provably unchanged (same direction as the must-change contract).
    * plain repair discrepancies / logical contradictions are ALWAYS
      advisory: they are recorded as notes for the Reviewer and repair
      feedback, never rolled back, never counted as enforcement.

    Unknown/missing diff facts never confirm a lead (the LLM verdict alone
    has no enforcement power).
    """
    if not isinstance(task, dict):
        task = {}
    text, items = _cot_lead_text(cot)

    boundary_lead = [
        str(item)
        for item in (
            cot.get("boundary_violations") or [] if isinstance(cot, dict) else []
        )
        if str(item).strip()
    ]
    side_effect_lead = bool(text and _COT_RUNTIME_SIDE_EFFECT_RE.search(text))
    mismatch_lead = bool(text and _COT_TASK_MISMATCH_RE.search(text))

    confirmed = []
    downgraded = []
    notes = []
    leads = []
    role = str(task.get("role", "") or "")
    category = normalize_worker_role(role)

    def _note(kind, confirmed_flag, first_item):
        if len(notes) >= _LEAD_NOTE_BUDGET:
            return
        excerpt = str(first_item or "")[:_LEAD_NOTE_TEXT_BUDGET]
        state = "confirmed" if confirmed_flag else "rejected"
        notes.append(
            f"Worker CoT {kind} lead (deterministic review: {state}): "
            f"{excerpt}"
        )

    if boundary_lead:
        leads.append("boundary")
        if category == "tuner":
            if _diff_changed_non_numeric(diff_facts):
                confirmed.append(
                    "boundary_lead_confirmed_tuner_non_numeric_diff"
                )
                _note("boundary", True, boundary_lead[0])
            else:
                downgraded.append("boundary_lead_rejected_numbers_only_diff")
                _note("boundary", False, boundary_lead[0])
        else:
            downgraded.append(
                f"boundary_lead_rejected_role_not_tuner:{category or 'other'}"
            )
            _note("boundary", False, boundary_lead[0])

    if side_effect_lead:
        leads.append("runtime_side_effect")
        if _diff_added_side_effect_lines(diff_facts):
            confirmed.append("runtime_side_effect_lead_confirmed_in_diff")
            _note("runtime-side-effect", True, text.splitlines()[0])
        else:
            downgraded.append(
                "runtime_side_effect_lead_rejected_no_diff_evidence"
            )
            _note(
                "runtime-side-effect",
                False,
                next(
                    (
                        item
                        for item in items
                        if _COT_RUNTIME_SIDE_EFFECT_RE.search(item)
                    ),
                    "",
                ),
            )

    if mismatch_lead:
        leads.append("task_mismatch")
        if _all_targets_provably_unchanged(task, diff_facts):
            confirmed.append("task_mismatch_lead_confirmed_targets_unchanged")
            _note("task-mismatch", True, text.splitlines()[0])
        else:
            downgraded.append("task_mismatch_lead_rejected_targets_changed")
            _note(
                "task-mismatch",
                False,
                next(
                    (
                        item
                        for item in items
                        if _COT_TASK_MISMATCH_RE.search(item)
                    ),
                    "",
                ),
            )

    # Plain repair/claim discrepancies with no lead above stay advisory —
    # recorded for the Reviewer and the repair feedback loop, never a
    # rollback. (The old repair-marker one-vote veto died here six
    # generations in a row: v525-v530.)
    leftover = [
        item
        for item in items
        if item not in boundary_lead
        and not _COT_RUNTIME_SIDE_EFFECT_RE.search(item)
        and not _COT_TASK_MISMATCH_RE.search(item)
    ]
    if isinstance(cot, dict) and not cot.get("cot_consistent", True):
        for item in leftover[: max(0, _LEAD_NOTE_BUDGET - len(notes))]:
            notes.append(
                "Worker CoT discrepancy (advisory; not enforcement): "
                f"{str(item)[:_LEAD_NOTE_TEXT_BUDGET]}"
            )

    block = bool(confirmed)
    return CotLeadVerdict(
        block=block,
        reason=";".join(confirmed),
        advisory_notes=notes,
        downgrade_reasons=downgraded,
        lead_kinds=leads,
    )


# ─────────────────────────────────────────────────────────────────────────────
# R3 — evidence-driven repair role hint + satisfiability precheck
# ─────────────────────────────────────────────────────────────────────────────

#: Deterministic constant-class markers: only these prove the repair
#: evidence is about a numeric-constant rollback/retune (the fixed
#: ``hyperparameter_boundary_violation`` gate error, or an explicit
#: revert-the-numeric-constant instruction).
_CONSTANT_CLASS_MARKERS = ("hyperparameter_boundary_violation",)

_CONSTANT_REVERT_INSTRUCTION_RE = re.compile(
    r"\b(?:revert|restore|roll\s*back|retune)\b[^.\n]{0,64}?\b"
    r"(?:numeric\s+constant|constant\s+value)\b",
    re.IGNORECASE,
)

#: Structural instruction shapes in repair evidence: phrases that demand
#: code-structure work no constants-only edit can express.
_STRUCTURAL_EVIDENCE_RE = re.compile(
    r"(?:\bcontrol[ -]flow\b|"
    r"\bnew\s+(?:functions?|branches?|imports?|classes?|helpers?)\b|"
    r"\bhelper\s+functions?\b|"
    r"\badd(?:s|ing|ed)?\s+(?:an?\s+)?(?:new\s+)?"
    r"(?:functions?|branches?|imports?|classes?|helpers?)\b|"
    r"\brefactor\w*\b|\brestructur\w+\b|\brewir\w+\b|"
    r"\bwire\s+it\s+into\b|"
    r"\bif[/ -]else\b|"
    r"\bstructural\s+correction\b)",
    re.IGNORECASE,
)

#: Blocker families whose fix is structural by construction; a Tuner
#: contract over any of them is unsatisfiable from birth.
_STRUCTURAL_REPAIR_BLOCKERS = frozenset({
    "runtime_architecture",
    "review_rejection",
    "precommit_regression",
    "official_rejection",
    "file_size",
    "position_semantics",
})

_FLIP_ROLE = "Algorithmic Logic Architect"

_REPAIR_TASK_KIND_MARKERS = (
    "repair",
    "rework",
)


def classify_repair_role_hint(evidence):
    """Return ``'tuner'`` only for deterministic constant-class evidence.

    Replaces the historic five-substring pin (``hyperparameter tuner`` /
    ``role boundary`` / ``existing numeric`` / ``existing constant`` /
    ``threshold``) that converted any threshold-mentioning structural
    blocker (v525: 'threshold' on 33 lines) into a constants-only Tuner
    contract. Everything else returns ``''`` and falls through to the
    Algorithmic Logic Architect default.
    """
    text = str(evidence or "")
    if not text.strip():
        return ""
    lowered = text.lower()
    if any(marker in lowered for marker in _CONSTANT_CLASS_MARKERS):
        return "tuner"
    if _CONSTANT_REVERT_INSTRUCTION_RE.search(text):
        return "tuner"
    return ""


class RepairContractDecision:
    """Satisfiability decision for one repair task's role contract."""

    __slots__ = ("satisfiable", "flip_role", "reason")

    def __init__(self, satisfiable, flip_role, reason):
        self.satisfiable = bool(satisfiable)
        self.flip_role = str(flip_role or "")
        self.reason = str(reason or "")

    def __repr__(self):  # pragma: no cover - diagnostic helper
        return (
            f"RepairContractDecision(satisfiable={self.satisfiable!r}, "
            f"flip_role={self.flip_role!r}, reason={self.reason!r})"
        )


def repair_contract_flip_decision(role, blocker, evidence, must_change_files):
    """Decide whether a Tuner repair contract is satisfiable as-is.

    A constants-only Tuner contract is unsatisfiable (flip to an Architect
    role before dispatch) when any of:

    * the blocker family is structural by construction;
    * more than one file must change;
    * the evidence demands structural work (instruction-shaped keywords)
      and carries NO deterministic constant-class marker — the marker, when
      present, always wins, so a genuine numeric-rollback contract keeps
      its Tuner.
    """
    if normalize_worker_role(role) != "tuner":
        return RepairContractDecision(True, "", "")
    blocker_text = str(blocker or "").strip().lower()
    if blocker_text in _STRUCTURAL_REPAIR_BLOCKERS:
        return RepairContractDecision(
            False, _FLIP_ROLE, f"structural_blocker_family:{blocker_text}"
        )
    must_change = [
        str(item) for item in (must_change_files or []) if str(item).strip()
    ]
    if len(set(must_change)) > 1:
        return RepairContractDecision(
            False, _FLIP_ROLE, "multi_file_must_change"
        )
    evidence_text = str(evidence or "")
    if evidence_text and not any(
        marker in evidence_text.lower()
        for marker in _CONSTANT_CLASS_MARKERS
    ) and not _CONSTANT_REVERT_INSTRUCTION_RE.search(evidence_text):
        if _STRUCTURAL_EVIDENCE_RE.search(evidence_text):
            return RepairContractDecision(
                False, _FLIP_ROLE, "structural_evidence_keywords"
            )
    return RepairContractDecision(True, "", "")


def repair_contract_satisfiable(task):
    """Satisfiability precheck for a whole repair task dict."""
    if not isinstance(task, dict):
        return RepairContractDecision(True, "", "")
    contract = (
        task.get("repair_contract")
        if isinstance(task.get("repair_contract"), dict)
        else {}
    )
    return repair_contract_flip_decision(
        role=task.get("role", ""),
        blocker=contract.get("blocker") or task.get("repair_blocker"),
        evidence=contract.get("evidence") or "",
        must_change_files=(
            task.get("must_change_files") or task.get("target_files")
        ),
    )


def _is_repair_task(task):
    if not isinstance(task, dict):
        return False
    if isinstance(task.get("repair_contract"), dict) and task.get(
        "repair_contract"
    ):
        return True
    if str(task.get("repair_blocker") or "").strip():
        return True
    task_kind = str(task.get("task_kind") or "").lower()
    return any(marker in task_kind for marker in _REPAIR_TASK_KIND_MARKERS)


def emit_repair_role_reassigned(task, decision, next_v):
    """Emit the durable role-reassignment event for one flipped contract."""
    try:
        from system_log import log_system_event

        log_system_event(
            "pipeline.repair_contract_role_reassigned",
            "warn",
            (
                f"Repair contract for worker "
                f"{task.get('worker_id', '?')} was unsatisfiable as a "
                f"constants-only Tuner contract ({decision.reason}); role "
                f"flipped to {decision.flip_role} before dispatch"
            ),
            {
                "next_v": next_v,
                "worker_id": task.get("worker_id"),
                "old_role": task.get("role"),
                "new_role": decision.flip_role,
                "reason": decision.reason,
                "repair_blocker": (
                    task.get("repair_contract", {}).get("blocker")
                    if isinstance(task.get("repair_contract"), dict)
                    else task.get("repair_blocker")
                ),
            },
        )
    except Exception:
        pass


def enforce_repair_contract_satisfiability(task, next_v=None):
    """Return ``task`` with an unsatisfiable Tuner contract flipped.

    The flip happens BEFORE dispatch, so the contract the Worker sees is
    satisfiable by construction (no constants-only guidance fighting a
    structural blocker and a must-change list). Non-repair innovation tasks
    are returned untouched: their Tuner roles are planning-layer decisions
    outside this authority.
    """
    if not _is_repair_task(task):
        return task
    decision = repair_contract_satisfiable(task)
    if decision.satisfiable or not decision.flip_role:
        return task
    flipped = dict(task)
    flipped["role"] = decision.flip_role
    contract = flipped.get("repair_contract")
    if isinstance(contract, dict):
        flipped["repair_contract"] = dict(contract)
        flipped["repair_contract"]["role_reassigned_from"] = str(
            task.get("role") or ""
        )
        flipped["repair_contract"]["role_reassignment_reason"] = (
            decision.reason
        )
    emit_repair_role_reassigned(task, decision, next_v)
    return flipped
