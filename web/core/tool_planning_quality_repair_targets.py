"""Repair-target extraction + quality contract emission for tool_planning_quality_contracts.

Extracted as a cohesive business cluster from
tool_planning_quality_contracts.py. The main module retains the quality-task
classification, contract-task assembly, mechanical source trimming, and the
authoritative rework synthesis; this companion owns the three parallel
"failure-source -> repair-target" families (precommit/official/review) and the
contract-emission cluster (file_size / position / national_native /
architecture / feedback / generic).

CRITICAL (wave-3 lesson): every intra-companion call to a moved symbol AND
every reference to a stay-behind main-module helper is routed through
``_qc.<name>(...)`` so that ``monkeypatch.setattr(tool_planning_quality_contracts,
"<name>", ...)`` and ``monkeypatch.setattr(tool_planning, "<name>", ...)``
fired by tests keep propagating after the move. A function calling ITSELF
recursively may stay bare; everything else goes through ``_qc.``.

The main module re-exports every symbol below via a bottom-of-file
``from tool_planning_quality_repair_targets import (...)`` block, so existing
``from tool_planning_quality_contracts import <name>`` /
``from tool_planning import <name>`` / ``tool_planning.<name>`` callers (and
test monkeypatches applied to ``tool_planning``) keep resolving unchanged.
"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path

from bot_namespace import bot_name
from output_schema import (
    NATIONAL_POLICY_FOCUS_ID,
    POLICY_CONTEXT_SCHEMA_VERSION,
    POLICY_CONTEXT_TOP_LEVEL_FIELDS,
    POLICY_ENTRYPOINTS,
    POLICY_INTENT_KINDS,
    PRECOMPUTE_KEY_SHAPE_PATTERN,
    PRECOMPUTE_MAX_BUILD_MS,
    PRECOMPUTE_MAX_BYTES,
    PRECOMPUTE_MAX_ENTRIES,
    RuntimeContract,
)
from tool_helpers import _target_rel

import tool_planning_quality_contracts as _qc  # for intra-companion + stay-behind refs


def _is_precommit_rework_checkpoint(ckpt):
    if not isinstance(ckpt, dict):
        return False
    if ckpt.get("stage") == "precommit_failed":
        return True
    gate_results = ckpt.get("gate_results") or {}
    precommit_gate = (
        gate_results.get("precommit_eval")
        if isinstance(gate_results, dict)
        else None
    )
    if isinstance(precommit_gate, dict) and precommit_gate.get("passed") is False:
        # Older checkpoints recorded the precommit receipt while leaving the
        # stage at critic_checked.  Preserve that compatibility only for a
        # measured regression; infrastructure-only failures stay on the
        # precommit owner and Critic advice can never enter this branch.
        from failure_classification import classify_precommit_gate

        if classify_precommit_gate(precommit_gate) in {
            "regression",
            "failed_unknown",
        }:
            return True
    work_item = _qc._checkpoint_work_item(ckpt)
    route = work_item.get("route") if isinstance(work_item.get("route"), dict) else {}
    return (
        work_item.get("kind") == "precommit_repair"
        or work_item.get("source_stage") == "precommit_failed"
        or route.get("intent") == "precommit_rework"
    )



def _precommit_failure_items(ckpt):
    if not isinstance(ckpt, dict):
        return []
    precommit = (ckpt.get("gate_results") or {}).get("precommit_eval") or {}
    items = []

    def add(value):
        if isinstance(value, dict):
            reason = value.get("reason")
            details = value.get("details")
            if reason or details:
                items.append(": ".join(str(x) for x in (reason, details) if x))
            evidence = value.get("evidence")
            if isinstance(evidence, (list, tuple)):
                for item in evidence[:5]:
                    add(item)
            elif evidence:
                add(evidence)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    add(precommit.get("directive"))
    add(ckpt.get("reviewer_feedback"))
    add(precommit.get("blockers"))
    add(precommit.get("failures"))

    for matchup in (precommit.get("matchups") or [])[:6]:
        if not isinstance(matchup, dict):
            continue
        opponent = matchup.get("opponent") or matchup.get("bot_b") or matchup.get("label") or "unknown"
        wins = matchup.get("wins", matchup.get("wins_a"))
        losses = matchup.get("losses", matchup.get("wins_b"))
        draws = matchup.get("draws", 0)
        reason = matchup.get("reason")
        net = matchup.get("net_chips")
        if isinstance(net, list):
            net = sum(x for x in net if isinstance(x, (int, float)))
        parts = [f"vs {opponent}"]
        if reason:
            parts.append(f"reason={reason}")
        if wins is not None and losses is not None:
            parts.append(f"result={wins}W-{losses}L-{draws}D")
        if net is not None:
            parts.append(f"net_chips={net}")
        items.append("; ".join(parts))

    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped



def _precommit_changed_python_files(ckpt):
    """Return candidate .py files that actually differ from the source parent."""
    if not isinstance(ckpt, dict):
        return []
    source_v = ckpt.get("source_v")
    next_v = ckpt.get("next_v")
    if source_v is None or next_v is None:
        return []
    if _qc._is_fresh_empty_pool_bootstrap(ckpt):
        # Fresh v143 has no source-side diff.  Its prepared/Worker receipts own
        # the exact policy delta; precommit must not infer one from stale v142.
        return []
    try:
        source_dir = _qc.get_bot_dir(source_v)
        next_dir = _qc.get_bot_dir(next_v)
        changed = _qc._py_files_changed_between(source_dir, next_dir)
    except Exception:
        return []

    preferred_order = {
        "policy.py": 0,
    }
    normalized = []
    seen = set()
    for item in changed:
        rel = _target_rel(item, next_v)
        if not rel or "backup" in rel:
            continue
        if rel in _qc._ACTIVE_CANDIDATE_WRITABLE_FILES and rel not in seen:
            seen.add(rel)
            normalized.append(rel)
    return sorted(normalized, key=lambda rel: (preferred_order.get(rel, 100), rel))



_PRECOMMIT_STRATEGY_REPAIR_FILES = [
    "policy.py",
]


_PRECOMMIT_PROTOCOL_REPAIR_FILES = frozenset()


_PRECOMMIT_PROTOCOL_EVIDENCE_MARKERS = (
    "official_smoke",
    "official smoke",
    "official-platform",
    "official platform",
    "illegal action",
    "illegal wire",
    "invalid action",
    "malformed action",
    "protocol violation",
    "wire output",
    "action serialization",
    "action format",
    "bet keyword",
    "extra spaces",
    "leading/trailing",
)



def _precommit_protocol_compliance_failure(failures, feedback=""):
    """Whether a precommit failure contains exact illegal/protocol evidence.

    National/official harnesses are compliance oracles in this pipeline. A plain
    W-L regression is a strategy repair and should not ask workers to tune the
    TCP entrypoint. Protocol files are only repair targets when the failure text
    names an illegal wire/action-format problem.
    """

    parts = [str(item) for item in failures or [] if item is not None]
    if feedback:
        parts.append(str(feedback))
    text = "\n".join(parts).lower()
    return any(marker in text for marker in _qc._PRECOMMIT_PROTOCOL_EVIDENCE_MARKERS)



def _precommit_filter_repair_targets(files, *, allow_protocol_files=False):
    """Return only candidate-owned policy targets.

    ``allow_protocol_files`` is retained for caller compatibility; system
    runtime files are never made writable by failure prose.
    """
    allowed = []
    for item in files or []:
        rel = Path(str(item)).name
        if rel not in _qc._ACTIVE_CANDIDATE_WRITABLE_FILES:
            continue
        allowed.append(rel)
    return allowed



def _limit_precommit_repair_targets(files):
    try:
        limit = int(os.environ.get("POK_PRECOMMIT_REPAIR_MAX_TARGETS", "3"))
    except ValueError:
        limit = 3
    limit = max(1, limit)
    targets = []
    seen = set()
    for item in files or []:
        rel = Path(str(item)).name
        if rel in _qc._ACTIVE_CANDIDATE_WRITABLE_FILES and rel not in seen:
            seen.add(rel)
            targets.append(rel)
        if len(targets) >= limit:
            break
    return targets



def _precommit_repair_target_files(ckpt, feedback):
    failures = _qc._precommit_failure_items(ckpt)
    if _qc._precommit_protocol_compliance_failure(failures, feedback):
        # Protocol/runtime bytes are system-owned.  Do not reinterpret a
        # compliance failure as permission for an LLM to mutate them.
        return []
    evidence_files = _qc._extract_quality_failure_files(failures)
    if not evidence_files and feedback:
        evidence_files = _qc._extract_quality_failure_files([feedback])

    changed_files = _qc._precommit_changed_python_files(ckpt)
    changed_repair_files = _qc._precommit_filter_repair_targets(
        changed_files,
        allow_protocol_files=False,
    )
    evidence_repair_files = _qc._precommit_filter_repair_targets(
        evidence_files,
        allow_protocol_files=False,
    )
    if changed_files and evidence_files:
        evidence_set = set(evidence_repair_files)
        intersected = [name for name in changed_repair_files if name in evidence_set]
        if intersected:
            return _qc._limit_precommit_repair_targets(intersected)
    if changed_repair_files:
        return _qc._limit_precommit_repair_targets(changed_repair_files)
    if evidence_repair_files:
        return _qc._limit_precommit_repair_targets(evidence_repair_files)

    try:
        next_v = ckpt.get("next_v") if isinstance(ckpt, dict) else None
        bot_dir = _qc.get_bot_dir(next_v) if next_v is not None else None
        if bot_dir:
            existing = [
                name for name in _qc._PRECOMMIT_STRATEGY_REPAIR_FILES
                if (bot_dir / name).exists()
            ]
            if existing:
                return _qc._limit_precommit_repair_targets(existing[:1])
    except Exception:
        pass
    return ["policy.py"]



def _official_failure_items(ckpt, feedback=""):
    items = []

    def add(value):
        if isinstance(value, dict):
            for key, val in value.items():
                add(f"{key}: {val}")
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    if isinstance(ckpt, dict):
        official = (ckpt.get("gate_results") or {}).get("official_full") or {}
        if isinstance(official, dict):
            add(official.get("issues"))
            add(official.get("official_evidence_summary"))
            add(official.get("verdict"))
            status = official.get("status") if isinstance(official.get("status"), dict) else {}
            add(status.get("official_llm_repair_guidance"))
            add(status.get("official_llm_prompt_feedback"))
            add(status.get("official_llm_analysis_summary"))
    add(feedback)
    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped



def _official_deterministic_failure_items(ckpt):
    """Return only machine-owned official verdict evidence used for repair scope.

    Reviewer feedback and the official LLM analysis are useful context for a
    worker, but they are not authority for making the system-owned TCP entrypoint
    writable.  In particular, an advisory sentence containing ``wire`` or
    ``protocol`` must never redirect an otherwise strategic repair.
    """
    items = []

    def add(value):
        if isinstance(value, dict):
            for key, val in value.items():
                add(f"{key}: {val}")
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    if isinstance(ckpt, dict):
        official = (ckpt.get("gate_results") or {}).get("official_full") or {}
        if isinstance(official, dict):
            add(official.get("issues"))
            add(official.get("official_evidence_summary"))
            add(official.get("verdict"))
    return list(dict.fromkeys(items))



def _official_failure_is_protocol(items):
    text = "\n".join(str(item) for item in items or []).lower()
    return any(marker in text for marker in (
        "protocol",
        "illegal",
        "invalid action",
        "unknown action",
        "wire",
        "raise format",
        "sticky",
        "connectionrefused",
        "brokenpipe",
    ))



def _official_repair_target_files(ckpt, feedback):
    deterministic_items = _qc._official_deterministic_failure_items(ckpt)
    evidence_files = _qc._extract_quality_failure_files(deterministic_items)
    if _qc._official_failure_is_protocol(deterministic_items):
        return []

    changed_files = _qc._precommit_changed_python_files(ckpt)
    strategy_candidates = [
        rel for rel in _qc._precommit_filter_repair_targets(changed_files, allow_protocol_files=False)
        if rel in _qc._PRECOMMIT_STRATEGY_REPAIR_FILES
    ]
    evidence_strategy = [
        rel for rel in _qc._precommit_filter_repair_targets(evidence_files, allow_protocol_files=False)
        if rel in _qc._PRECOMMIT_STRATEGY_REPAIR_FILES
    ]
    if strategy_candidates and evidence_strategy:
        evidence_set = set(evidence_strategy)
        intersected = [name for name in strategy_candidates if name in evidence_set]
        if intersected:
            return _qc._limit_precommit_repair_targets(intersected)
    if strategy_candidates:
        return _qc._limit_precommit_repair_targets(strategy_candidates)
    if evidence_strategy:
        return _qc._limit_precommit_repair_targets(evidence_strategy)
    try:
        next_v = ckpt.get("next_v") if isinstance(ckpt, dict) else None
        bot_dir = _qc.get_bot_dir(next_v) if next_v is not None else None
        if bot_dir:
            existing = [
                name for name in _qc._PRECOMMIT_STRATEGY_REPAIR_FILES
                if (bot_dir / name).exists()
            ]
            if existing:
                return _qc._limit_precommit_repair_targets(existing[:2])
    except Exception:
        pass
    return ["policy.py"]



def _official_repair_tasks(ckpt, feedback):
    items = _qc._official_failure_items(ckpt, feedback)
    targets = _qc._official_repair_target_files(ckpt, feedback)
    if not targets:
        return []
    evidence = "\n".join(str(item) for item in items[:30]) or str(feedback or "official full certification failed")
    next_v = ckpt.get("next_v") if isinstance(ckpt, dict) else "?"
    source_v = ckpt.get("source_v") if isinstance(ckpt, dict) else "?"
    method = (
        "- This is an official EXE full-certification repair, not a strength-rating tweak.\n"
        "- Use only the checkpoint-injected deterministic round issues below; the raw official evidence path is system-owned.\n"
        "- Fix only the bot-side reason the official 70-hand full gate could not complete.\n"
        "- Do not loosen local validators, suppress official evidence, or mark certification passed manually.\n"
        "- Keep the five-file strict artifact (the five executable/identity files) intact and the system-owned TCP entrypoint byte-identical. "
        "A model/table is only future system-owned brokered infrastructure, never a candidate file."
    )
    method += (
        "\n- Candidate scope is policy.py only. Repair only a policy exception, "
        "invalid typed intent, or bounded-deadline behavior proven by the evidence."
        "\n- A wire/parser/reducer/entrypoint failure is system-owned and must remain "
        "fail-closed; never edit national_bot.py or precompute.py."
    )
    role = "Algorithmic Logic Architect"
    prompt = (
        f"Repair official EXE full-certification blocker for bots/{bot_name(next_v)} from source v{source_v}.\n\n"
        f"Official evidence:\n{evidence[:5000]}\n\n"
        f"Required method:\n{method}\n\n"
        "Verification expectation:\n"
        "- Run `python -m py_compile` on the exact edited file; imports and dynamic checks remain system-owned.\n"
        "- Confirm only policy.py changed; system artifacts must remain byte-identical.\n"
        "- End with the concrete official failure class you addressed."
    )
    return [{
        "worker_id": "auto_official_full_repair",
        "role": role,
        "target_files": targets,
        "must_change_files": targets,
        "worker_prompt": prompt,
        "task_kind": "official_repair",
        "repair_blocker": "official_full",
        "repair_contract": {
            "blocker": "official_full",
            "files": targets,
            "evidence": evidence[:2000],
            "source_stage": str(ckpt.get("stage") or "")
            if isinstance(ckpt, dict) else "",
        },
    }]



def _review_feedback_items(ckpt, feedback=""):
    items = []

    def add(value):
        if isinstance(value, dict):
            for key, val in value.items():
                add(f"{key}: {val}")
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    add(feedback)
    if isinstance(ckpt, dict):
        review = (ckpt.get("gate_results") or {}).get("review") or {}
        if isinstance(review, dict):
            for key in (
                "feedback",
                "reasoning",
                "directive",
                "blockers",
                "failures",
                "issues",
                "code_quality_issues",
            ):
                add(review.get(key))

    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped



def _review_primary_feedback_text(feedback):
    """Trim reviewer feedback down to the blocking issue, excluding side notes."""
    text = str(feedback or "").strip()
    if not text:
        return ""
    text = re.split(r"(?i)\n\s*NOTE:\s+This is\b", text, maxsplit=1)[0].strip()
    text = re.split(r"(?i)\bNote on\s+[A-Za-z0-9_./-]+\.py\s*:", text, maxsplit=1)[0].strip()
    text = re.split(r"(?i)\bAlso notes?\b", text, maxsplit=1)[0].strip()
    text = re.split(r"(?i)\bOther checks\s*:", text, maxsplit=1)[0].strip()
    return text



def _review_repair_target_files(ckpt, feedback):
    primary = _qc._review_primary_feedback_text(feedback)
    evidence_files = _qc._extract_quality_failure_files([primary]) if primary else []
    allow_protocol_files = _qc._precommit_protocol_compliance_failure([primary], feedback)
    evidence_repair_files = _qc._precommit_filter_repair_targets(
        evidence_files,
        allow_protocol_files=allow_protocol_files,
    )
    if evidence_repair_files:
        return _qc._limit_precommit_repair_targets(evidence_repair_files)

    changed_files = _qc._precommit_changed_python_files(ckpt)
    changed_repair_files = _qc._precommit_filter_repair_targets(
        changed_files,
        allow_protocol_files=allow_protocol_files,
    )
    if changed_repair_files:
        return _qc._limit_precommit_repair_targets(changed_repair_files)
    return ["policy.py"]



def _flatten_text_items(value):
    items = []

    def add(item):
        if isinstance(item, dict):
            for key, val in item.items():
                add(f"{key}: {val}")
        elif isinstance(item, (list, tuple, set)):
            for sub in item:
                add(sub)
        elif item is not None:
            text = str(item).strip()
            if text:
                items.append(text)

    add(value)
    return items



def _is_position_semantics_failure_text(item):
    text = str(item or "").lower()
    return (
        "position_semantics" in text
        or "retired position identifier" in text
        or "retired decision_context key" in text
        or "decision_context.hand.position" in text
        or "decision_context.line.position" in text
        or "acts_first_postflop" in text
        or "hero_in_position_postflop" in text
        or "bb acts first postflop" in text
        or "candidate-side seat reconstruction" in text
    )



def _is_official_smoke_protocol_failure_text(item):
    text = str(item or "").lower()
    if any(marker in text for marker in (
        "protocol_",
        "protocol error",
        "illegal_bet_action",
        "protocol_raise_format",
        "protocol_action_format",
        "protocol_action_whitespace",
        "invalid action",
        "unknown action",
    )):
        return True
    return "illegal" in text and "official" in text



def _is_runtime_architecture_failure_text(item):
    text = str(item or "").lower()
    return any(marker in text for marker in (
        "runtime_architecture",
        "architecture_focus:",
        "architecture_regression:",
        "architecture_policy_",
        "national_capability_contract",
    ))



def _declared_scope_violation_files(ckpt, reviewer_feedback=""):
    """Extract undeclared artifact paths for fail-closed integrity handling."""
    if not isinstance(ckpt, dict):
        return set()
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    if not isinstance(quality, dict) or quality.get("declared_scope_ok") is True:
        return set()

    next_v = ckpt.get("next_v")
    evidence = []
    evidence.extend(_qc._flatten_text_items(quality.get("declared_scope_errors")))
    evidence.extend(
        item for item in _qc._quality_failure_items(ckpt)
        if _qc._is_declared_scope_failure_text(item)
    )
    # Machine-owned declared_scope_errors/metrics are the primary authority.
    # If an older checkpoint lacks them, consume only feedback lines that
    # themselves describe a scope violation; never append the aggregate quality
    # receipt because it also names legitimate file_size/position targets.
    if not evidence and reviewer_feedback:
        evidence.extend(
            line.strip()
            for line in str(reviewer_feedback).splitlines()
            if _qc._is_declared_scope_failure_text(line)
        )

    files = set()
    for filename in _qc._extract_quality_failure_files(evidence):
        rel = _target_rel(filename, next_v)
        if rel:
            files.add(rel)

    scope_metrics = quality.get("declared_scope") or {}
    if not files and isinstance(scope_metrics, dict):
        changed = {
            rel for rel in (
                _target_rel(item, next_v)
                for item in scope_metrics.get("changed_files", []) or []
            )
            if rel
        }
        allowed = {
            rel for rel in (
                _target_rel(item, next_v)
                for item in scope_metrics.get("allowed_files", []) or []
            )
            if rel
        }
        files.update(changed - allowed)
    return files



def _line_count_contracts(quality, failures):
    """Return structured file_size blocker contracts from quality gate output."""
    by_file = {}

    def add(filename, current=None, limit=None, evidence=""):
        rel = Path(str(filename)).name
        if not rel:
            return
        existing = by_file.get(rel, {})
        evidences = []
        if existing.get("evidence"):
            evidences.append(str(existing["evidence"]))
        if evidence and evidence not in evidences:
            evidences.append(evidence)
        by_file[rel] = {
            "blocker": "file_size",
            "file": rel,
            "current_lines": current if current is not None else existing.get("current_lines"),
            "line_limit": limit if limit is not None else existing.get("line_limit"),
            "evidence": "; ".join(evidences),
        }

    oversized = quality.get("oversized_files")
    if isinstance(oversized, dict):
        for filename, lines in oversized.items():
            try:
                current = int(lines)
            except (TypeError, ValueError):
                current = None
            add(filename, current=current, evidence=f"oversized_files[{filename}]={lines}")

    text = "\n".join(str(item) for item in failures or [])
    for group in re.finditer(r"file_size\(([^)]*)\)", text):
        body = group.group(1)
        for match in re.finditer(
            r"([A-Za-z0-9_./-]+\.py):(\d+)L(?:/(\d+)L)?",
            body,
        ):
            current = int(match.group(2))
            limit = int(match.group(3)) if match.group(3) else None
            add(match.group(1), current=current, limit=limit, evidence=f"file_size({body})")
    return [by_file[name] for name in sorted(by_file)]



def _position_contracts(quality):
    """Return structured position_semantics contracts grouped by file."""
    source_items = []
    source_items.extend(_qc._flatten_text_items(quality.get("position_semantics_errors")))
    for item in _qc._flatten_text_items(quality.get("failed_gates")):
        if "position_semantics(" in item:
            source_items.append(item)

    by_file = {}
    for item in source_items:
        text = str(item)
        for match in re.finditer(
            r"([A-Za-z0-9_./-]+\.py):(\d+):?\s*([^;\n)]*)",
            text,
        ):
            rel = Path(match.group(1)).name
            if not rel:
                continue
            detail = {
                "line": int(match.group(2)),
                "message": match.group(3).strip() or text.strip(),
                "evidence": text.strip(),
            }
            by_file.setdefault(rel, []).append(detail)

    contracts = []
    for rel, details in by_file.items():
        deduped = []
        seen = set()
        for detail in details:
            key = (detail["line"], detail["message"])
            if key not in seen:
                seen.add(key)
                deduped.append(detail)
        contracts.append({
            "blocker": "position_semantics",
            "file": rel,
            "details": deduped,
            "evidence": "; ".join(d["evidence"] for d in deduped[:4]),
        })
    return sorted(contracts, key=lambda c: c["file"])



def _national_native_contracts(quality, failures):
    """System runtime contract failures are never candidate repair tasks."""
    return []



def _official_smoke_contracts(quality, failures):
    """Official wire failures remain fail-closed system/infrastructure debt."""
    return []



_ARCHITECTURE_FOCUS_LAYERS = {
    NATIONAL_POLICY_FOCUS_ID: "runtime_architecture",
    "incremental_match_model": "opponent_model",
    "deadline_refinement": "runtime_architecture",
    "bounded_runtime_enumeration": "precompute",
    "decision_path_purity": "runtime_architecture",
}


_STATE_LEARNING_ORACLE_REFS = [
    "docs/official-raise-boundary-oracle-2026-07-11.md",
    "docs/official-terminal-settlement-oracle-2026-07-11.md",
    "docs/official-allin-runout-wire-oracle-2026-07-19.md",
]



def _detected_artifact_consumer(artifact):
    """Return a schema consumer bound to an actual detector call-chain node."""
    candidates = []
    for location in artifact.get("consumer_locations") or []:
        for segment in str(location).split("->"):
            match = re.fullmatch(
                r"([A-Za-z_][A-Za-z0-9_]*)\.py:([A-Za-z_][A-Za-z0-9_]*)",
                segment,
            )
            if match:
                candidates.append(f"{match.group(1)}.{match.group(2)}")
    for preferred in POLICY_ENTRYPOINTS:
        for candidate in candidates:
            if candidate.endswith(f".{preferred}"):
                return candidate
    return candidates[0] if candidates else "policy.get_baseline_decision"



def _candidate_consumed_precompute_contracts(
    candidate_capabilities,
    *,
    require_action_influence: bool = False,
):
    """Translate proven candidate artifacts into repair declarations.

    Static evidence owns identity, build phase, bound, and consumer. Dynamic
    evidence supplies measured key shape, bytes, and import latency. This keeps a
    repair attached to the candidate's real artifact instead of inventing a
    generic lookup whenever an unrelated architecture check fails.  When a
    lookup is the selected primary, require the runtime counterfactual proof as
    well: a read-only/discarded foundation table is still useful acceleration,
    but is not a strategy innovation.
    """
    if not isinstance(candidate_capabilities, dict):
        return []
    precompute = candidate_capabilities.get("precompute_evidence") or {}
    dynamic_rows = {
        (str(row.get("owner_file") or ""), str(row.get("name") or "")): row
        for row in (
            (candidate_capabilities.get("dynamic_runtime_probe") or {}).get("artifacts")
            or []
        )
        if isinstance(row, dict)
    }
    contracts = []
    static_artifacts = [
        artifact
        for artifact in precompute.get("consumed_artifacts") or []
        if isinstance(artifact, dict)
    ]
    static_artifacts.sort(key=lambda artifact: (
        not bool(dynamic_rows.get((
            str(artifact.get("location") or "").split(":", 1)[0],
            str(artifact.get("name") or ""),
        ), {}).get("ok")),
        str(artifact.get("location") or ""),
        str(artifact.get("name") or ""),
    ))
    for artifact in static_artifacts:
        owner_file = str(artifact.get("location") or "").split(":", 1)[0]
        name = str(artifact.get("name") or "").strip()
        if owner_file != "precompute.py" or len(name) < 2:
            continue
        dynamic = dynamic_rows.get((owner_file, name)) or {}
        if require_action_influence and not dynamic.get("value_affects_final_wire"):
            continue
        raw_shape = str(dynamic.get("observed_key_shape") or "int")
        key_shape = (
            raw_shape
            if re.fullmatch(PRECOMPUTE_KEY_SHAPE_PATTERN, raw_shape)
            else "int"
        )
        entries = max(1, int(artifact.get("bound_entries") or 1))
        measured_bytes = max(262_144, int(dynamic.get("deep_bytes") or 0))
        measured_ms = max(
            500,
            int(float(dynamic.get("import_elapsed_ms") or 0) + 0.999),
        )
        contracts.append({
            "name": name,
            "owner_file": owner_file,
            "build_phase": str(artifact.get("build_phase") or "module_import"),
            "max_build_ms": min(PRECOMPUTE_MAX_BUILD_MS, measured_ms),
            "max_entries": min(PRECOMPUTE_MAX_ENTRIES, entries),
            "max_bytes": min(PRECOMPUTE_MAX_BYTES, measured_bytes),
            "key_shape": key_shape,
            "consumer": _qc._detected_artifact_consumer(artifact),
            "fallback": "legal_baseline",
        })
        break
    return contracts



def _default_state_learning_contract(
    focus_id,
    skill_layer,
    required_checks,
    candidate_capabilities=None,
):
    if focus_id != NATIONAL_POLICY_FOCUS_ID:
        return None
    required = {str(item) for item in required_checks or []}
    work_primitive = None
    profile_dimensions = []
    line_controls = []
    wants_precompute = "precompute_lookup_path" in required or skill_layer == "precompute"
    if wants_precompute:
        # Compact system facts remain valid acceleration inputs, but table use
        # cannot be selected as the generation's primary innovation until a
        # digest-bound value-variant probe exists.  Use the independently
        # measurable bounded-candidate primary for deterministic repair plans.
        work_primitive = "sample_counted_candidate_batch"
    elif "terminal_response_adaptation" in required:
        profile_dimensions = ["terminal_response"]
    elif "showdown_range_adaptation" in required:
        profile_dimensions = ["showdown_range"]
    elif "incremental_opponent_model" in required or skill_layer in {
        "match_memory",
        "opponent_model",
    }:
        profile_dimensions = ["action_profile"]
    elif "donk_line_reachability" in required:
        line_controls = ["donk"]
    elif "delayed_probe_line_reachability" in required:
        line_controls = ["delayed_probe"]
    elif skill_layer == "line_template":
        line_controls = ["donk"]
    else:
        work_primitive = "sample_counted_candidate_batch"
    return {
        "work_primitive": work_primitive,
        "profile_dimensions": profile_dimensions,
        "line_controls": line_controls,
        "oracle_refs": list(_qc._STATE_LEARNING_ORACLE_REFS),
    }



def _architecture_default_runtime_contract(
    focus_id,
    skill_layer,
    owner_file=None,
    required_checks=(),
    candidate_capabilities=None,
):
    """Return a strict fallback contract for deterministic/crossover repair plans."""
    required_checks = {str(item) for item in required_checks or []}
    contract = {
        "policy_abi": {
            "module": "policy.py",
            "context_schema_version": POLICY_CONTEXT_SCHEMA_VERSION,
            "context_fields": list(POLICY_CONTEXT_TOP_LEVEL_FIELDS),
            "entrypoints": list(POLICY_ENTRYPOINTS),
            "intent_kinds": list(POLICY_INTENT_KINDS),
            "raise_field": "raise_to",
            "pass_mapping": "socket_owner_call_or_check",
        },
        "decision": None,
        "precompute_artifacts": [],
        "match_memory": None,
        "state_learning": _qc._default_state_learning_contract(
            focus_id,
            skill_layer,
            required_checks,
            candidate_capabilities,
        ),
        "reference_pack_id": "",
        "official_feedback_refs": [],
        "forbidden_runtime_work": [
            "reconstructing match state outside decision_context",
            "file, network, or subprocess I/O inside the decision path",
            "unbounded combinatorial construction per decision",
        ],
    }
    state_learning = contract.get("state_learning") or {}
    primary_work = state_learning.get("work_primitive")
    if primary_work:
        from strategy_reference_pack import default_reference_pack_id

        contract["reference_pack_id"] = default_reference_pack_id(primary_work)
    primary_profiles = set(state_learning.get("profile_dimensions") or [])
    if (
        skill_layer in {"match_memory", "opponent_model"}
        or focus_id in {
            "incremental_match_model",
        }
        or primary_profiles
        or required_checks.intersection({
            "persistent_match_memory",
            "terminal_response_memory",
            "showdown_range_posterior",
            "authoritative_hand_context",
            "incremental_opponent_model",
            "terminal_response_adaptation",
            "showdown_range_adaptation",
            "donk_line_reachability",
            "delayed_probe_line_reachability",
            "semantic_line_reachability",
            "decision_path_no_full_history_scan",
        })
    ):
        contract["match_memory"] = {
            "tracker_class": "OpponentTracker",
            "owner_file": "national_bot.py",
            "reset_boundary": "tcp_connection",
            "update_events": [
                "hand_start",
                "street_start",
                "opponent_action",
                "settlement",
                "showdown",
            ],
            "snapshot_field": "opponent",
            "max_recent_hands": 8,
            "prior_rule": "beta_prior_weight_8",
            "confidence_rule": (
                "global_actions_over_actions_plus_24_and_context_samples_over_samples_plus_8"
            ),
            "adaptation_cap": 0.65,
            "consumer": "policy.get_baseline_decision",
        }
    if (
        skill_layer == "precompute"
        or focus_id in {
            "bounded_runtime_enumeration",
        }
        or required_checks.intersection({
            "precompute_lookup_path",
            "decision_path_no_large_runtime_tables",
        })
    ):
        contract["precompute_artifacts"] = _qc._candidate_consumed_precompute_contracts(
            candidate_capabilities,
            require_action_influence=False,
        )
    if (
        skill_layer in {"runtime_architecture", "native_tcp"}
        or focus_id in {
            "deadline_refinement",
            "decision_path_purity",
        }
        or primary_work == "sample_counted_candidate_batch"
        or required_checks.intersection({
            "decision_time_budget_visible",
            "killable_decision_runtime",
            "fast_policy_baseline",
            "incremental_refinement_protocol",
            "budget_scaled_refinement",
            "decision_path_no_external_io",
        })
    ):
        contract["decision"] = {
            "clock": "time.monotonic",
            "hard_deadline_ms": 55_000,
            "baseline_target_ms": 250,
            "refinement_budget_ms": 54_000,
            "baseline_path": "compute a legal deterministic action before optional refinement",
            "fallback_action": "return typed pass when no wager is faced, otherwise fold",
            "refinement_bound": "stop on the monotonic deadline and an explicit finite sample cap",
            "max_samples": 4_096,
        }
    return contract



def _merge_runtime_contract_floor(inherited, floor_contract):
    """Preserve the accepted contract while adding newly proven floor debt."""
    result = deepcopy(floor_contract)
    if not isinstance(inherited, dict):
        return result
    if inherited.get("policy_abi") is not None:
        result["policy_abi"] = deepcopy(inherited["policy_abi"])
    if inherited.get("decision") is not None:
        result["decision"] = deepcopy(inherited["decision"])
    if inherited.get("match_memory") is not None:
        result["match_memory"] = deepcopy(inherited["match_memory"])
    if inherited.get("state_learning") is not None:
        result["state_learning"] = deepcopy(inherited["state_learning"])
    if inherited.get("reference_pack_id"):
        result["reference_pack_id"] = str(inherited["reference_pack_id"])
    state_learning = result.get("state_learning") or {}
    primary_work = state_learning.get("work_primitive") if isinstance(state_learning, dict) else None
    if primary_work and not result.get("reference_pack_id"):
        from strategy_reference_pack import default_reference_pack_id

        result["reference_pack_id"] = default_reference_pack_id(primary_work)
    inherited_artifacts = [
        deepcopy(item)
        for item in inherited.get("precompute_artifacts") or []
        if isinstance(item, dict)
    ]
    if inherited_artifacts:
        by_identity = {
            (str(item.get("owner_file")), str(item.get("name"))): item
            for item in result.get("precompute_artifacts") or []
            if isinstance(item, dict)
        }
        for item in inherited_artifacts:
            by_identity[(str(item.get("owner_file")), str(item.get("name")))] = item
        result["precompute_artifacts"] = list(by_identity.values())
    for key in ("official_feedback_refs", "forbidden_runtime_work"):
        result[key] = list(dict.fromkeys([
            *(result.get(key) or []),
            *(inherited.get(key) or []),
        ]))[:8]
    return result



def _architecture_repair_context(ckpt, focus_id):
    plan = _qc._checkpoint_master_plan(ckpt)
    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if focus_id and str(task.get("architecture_focus_id") or "") != focus_id:
            continue
        contract = task.get("runtime_contract")
        if isinstance(contract, dict):
            return str(task.get("skill_layer") or ""), contract
    return "", None



def _architecture_contracts(quality, ckpt):
    """Build one evidence-scoped repair contract for the transition hard gate.

    Runtime architecture is deliberately repaired as one coherent task. Splitting
    provider, consumer, and decision-path cleanup across generic workers can make
    each edit look plausible while the end-to-end AST capability still fails.
    """
    transition = quality.get("national_architecture_transition") or {}
    if not isinstance(transition, dict) or transition.get("ok", True):
        # Defect-C hardening: when the transition evaluator is absent/skipped/ok
        # but the flat national_capability_contract already proves a policy.py-
        # repairable failure, synthesize a policy.py repair contract from the
        # flat contract alone.  Without this the rework synthesizer dead-ends on
        # ``system_repair_task_synthesis_empty`` and the generation terminal-
        # abandons (observed for v12/v13 under a different gate shape; the same
        # dead-end recurs whenever the transition object is missing but the flat
        # contract carries the evidence).  Fail-closed: only check_ids that the
        # _ARCHITECTURE_CHECK_FILES map AND the flat contract's own
        # evidence.locations both corroborate as policy.py are repaired; a non-
        # policy.py failure still returns [] (terminal abandon, unchanged).
        return _architecture_contracts_from_capability_only(quality, ckpt)
    if transition.get("runtime_probe_infra"):
        return []
    if transition.get("policy_identity_errors"):
        return []

    candidate = transition.get("candidate_capabilities") or {}
    checks_by_id = candidate.get("checks_by_id") or {}
    policy = transition.get("policy") or {}
    focus = transition.get("selected_focus") or policy.get("selected_focus") or {}
    focus_id = str(focus.get("focus_id") or "")

    failing_ids = _qc._architecture_transition_failure_ids(transition)

    # A policy identity mismatch is repository/checkpoint drift, not bot code
    # debt. Do not waste a worker edit trying to change a digest.
    if not failing_ids:
        return []
    worker_repairable = (
        "runtime_contract_implementation" in failing_ids
        or any(
            "policy.py" in _qc._ARCHITECTURE_CHECK_FILES.get(check_id, ())
            for check_id in failing_ids
        )
    )
    if not worker_repairable:
        # Reducer, socket, tracker, or system-precompute failures cannot be
        # redirected into policy.py merely to obtain a repair task.
        return []

    inherited_layer, inherited_contract = _qc._architecture_repair_context(ckpt, focus_id)
    skill_layer = inherited_layer or _qc._ARCHITECTURE_FOCUS_LAYERS.get(focus_id, "")
    if not skill_layer:
        for check_id in failing_ids:
            candidate_layer = str((checks_by_id.get(check_id) or {}).get("skill_layer") or "")
            if candidate_layer:
                skill_layer = candidate_layer
                break
    skill_layer = skill_layer or "runtime_architecture"

    candidate_dir = _qc.get_bot_dir(ckpt.get("next_v")) if ckpt.get("next_v") is not None else None
    target_files = _qc._architecture_transition_repair_files(transition, candidate_dir)

    evidence_lines = []
    for check_id in failing_ids:
        check = checks_by_id.get(check_id) or {}
        evidence = check.get("evidence") or {}
        guidance = check.get("guidance") or "Satisfy this capability with code consumed by the decision path."
        locations = [str(item) for item in evidence.get("locations") or []]
        summary = str(evidence.get("summary") or "no detector summary")
        location_text = f"; locations={locations[:3]}" if locations else ""
        evidence_lines.append(f"{check_id}: {summary}; required={guidance}{location_text}")
    for error in transition.get("runtime_contract_implementation_errors") or []:
        evidence_lines.append(f"runtime_contract_implementation: {error}")

    target_files = ["policy.py"]
    primary = target_files[0]
    precompute_owner = "precompute.py"
    floor_contract = _qc._architecture_default_runtime_contract(
        focus_id,
        skill_layer,
        precompute_owner,
        required_checks=failing_ids,
        candidate_capabilities=candidate,
    )
    runtime_contract = _qc._merge_runtime_contract_floor(inherited_contract, floor_contract)
    validated_runtime_contract = RuntimeContract.model_validate(runtime_contract)
    primary_checks = (
        list(validated_runtime_contract.state_learning.primary_checks())
        if validated_runtime_contract.state_learning is not None
        else []
    )
    task_required_checks = list(dict.fromkeys([
        *failing_ids,
        *primary_checks,
    ]))
    return [{
        "blocker": "runtime_architecture",
        "file": primary,
        "files": target_files,
        "must_change_files": [primary],
        "focus_id": focus_id,
        "required_checks": task_required_checks,
        "preserve_checks": list(policy.get("baseline_passed_checks") or []),
        "skill_layer": skill_layer,
        "evidence": "\n".join(evidence_lines),
        "architecture_policy": policy,
        "runtime_contract": runtime_contract,
    }]


def _architecture_contracts_from_capability_only(quality, ckpt):
    """Synthesize a policy.py repair contract from the flat capability contract.

    Used only when ``national_architecture_transition`` is absent/skipped/ok but
    the flat ``national_capability_contract`` carries policy.py-repairable
    failures (defect-C hardening).  Fail-closed: a failing check is repaired only
    if BOTH (a) ``_ARCHITECTURE_CHECK_FILES`` maps it to ``policy.py`` AND (b)
    the flat contract's own ``evidence.locations`` corroborate ``policy.py``.
    Returns ``[]`` (terminal abandon) for genuinely non-policy.py failures,
    matching the pre-existing behavior for those cases.
    """
    capability = quality.get("national_capability_contract") or {}
    if not isinstance(capability, dict) or capability.get("ok", True):
        return []
    checks_by_id = capability.get("checks_by_id") or {}
    if not isinstance(checks_by_id, dict) or not checks_by_id:
        return []
    required_failures = capability.get("required_failures") or []
    candidate_failing = []
    for entry in required_failures:
        check_id = str((entry or {}).get("check_id") or "") if isinstance(entry, dict) else str(entry or "")
        if check_id:
            candidate_failing.append(check_id)
    if not candidate_failing:
        return []
    # Fail-closed guard: require BOTH the static map AND the flat contract's own
    # evidence.locations to corroborate policy.py for each repaired check_id.
    repairable = []
    for check_id in candidate_failing:
        mapped_files = _qc._ARCHITECTURE_CHECK_FILES.get(check_id, ())
        if "policy.py" not in mapped_files:
            continue
        check = checks_by_id.get(check_id) or {}
        locations = [str(item) for item in (check.get("evidence") or {}).get("locations") or []]
        if "policy.py" in locations:
            repairable.append(check_id)
    if not repairable:
        return []
    failing_ids = repairable
    policy = capability.get("policy") or {}
    focus_id = ""
    skill_layer = ""
    for check_id in failing_ids:
        candidate_layer = str((checks_by_id.get(check_id) or {}).get("skill_layer") or "")
        if candidate_layer:
            skill_layer = candidate_layer
            break
    skill_layer = skill_layer or "runtime_architecture"
    inherited_layer, inherited_contract = _qc._architecture_repair_context(ckpt, focus_id)
    if inherited_layer:
        skill_layer = inherited_layer
    evidence_lines = []
    for check_id in failing_ids:
        check = checks_by_id.get(check_id) or {}
        evidence = check.get("evidence") or {}
        guidance = check.get("guidance") or "Satisfy this capability with code consumed by the decision path."
        locations = [str(item) for item in evidence.get("locations") or []]
        summary = str(evidence.get("summary") or "no detector summary")
        location_text = f"; locations={locations[:3]}" if locations else ""
        evidence_lines.append(f"{check_id}: {summary}; required={guidance}{location_text}")
    target_files = ["policy.py"]
    primary = target_files[0]
    precompute_owner = "precompute.py"
    floor_contract = _qc._architecture_default_runtime_contract(
        focus_id,
        skill_layer,
        precompute_owner,
        required_checks=failing_ids,
        candidate_capabilities=capability,
    )
    runtime_contract = _qc._merge_runtime_contract_floor(inherited_contract, floor_contract)
    validated_runtime_contract = RuntimeContract.model_validate(runtime_contract)
    primary_checks = (
        list(validated_runtime_contract.state_learning.primary_checks())
        if validated_runtime_contract.state_learning is not None
        else []
    )
    task_required_checks = list(dict.fromkeys([
        *failing_ids,
        *primary_checks,
    ]))
    return [{
        "blocker": "runtime_architecture",
        "file": primary,
        "files": target_files,
        "must_change_files": [primary],
        "focus_id": focus_id,
        "required_checks": task_required_checks,
        "preserve_checks": list(policy.get("baseline_passed_checks") or []),
        "skill_layer": skill_layer,
        "evidence": "\n".join(evidence_lines),
        "architecture_policy": policy,
        "runtime_contract": runtime_contract,
    }]



def _split_reviewer_quality_feedback(feedback):
    """Return actionable reviewer issue snippets, excluding positive check text."""
    text = str(feedback or "").strip()
    if not text:
        return []
    if text.lower().startswith("quality gates failed:"):
        return []

    chunks = []
    for part in re.split(r"(?m)(?:^|\n)\s*(?=\d+[\.)]\s+)", text):
        cleaned = re.sub(r"^\s*\d+[\.)]\s+", "", part.strip())
        if cleaned:
            chunks.append(cleaned)
    if not chunks:
        chunks = [text]

    actionable = []
    problem_markers = (
        "block",
        "issue",
        "violation",
        "dead code",
        "unused",
        "unconsumed",
        "must be",
        "must not",
        "rejected",
        "reject",
        "flag",
        "risk",
        "failed",
        "failure",
        "scope",
    )
    positive_markers = (
        "other checks",
        "compile cleanly",
        "compiles",
        "imports succeed",
        "valid raw tcp client",
        "unchanged and remains",
    )
    for chunk in chunks:
        chunk = re.split(r"(?i)\bOther checks\s*:", chunk, maxsplit=1)[0].strip()
        if not chunk:
            continue
        lower = chunk.lower()
        if not re.search(r"[A-Za-z0-9_./-]+\.py", chunk):
            continue
        if any(marker in lower for marker in positive_markers) and not any(
            marker in lower for marker in ("but", "however", "block", "issue", "violation", "dead code", "unused")
        ):
            continue
        if any(marker in lower for marker in problem_markers):
            actionable.append(chunk.strip())
    return actionable



def _primary_feedback_file(item):
    text = str(item or "")
    scope_files = _qc._scope_drift_feedback_files(text)
    if scope_files:
        return scope_files[0]
    patterns = (
        r"(?:in|on|file)\s+([A-Za-z0-9_./-]+\.py)\s*:",
        r"([A-Za-z0-9_./-]+\.py)\s*:",
        r"([A-Za-z0-9_./-]+\.py)\s+(?:edits|changes|changed|computes|defines|returns|stores)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            rel = Path(match.group(1)).name
            if rel:
                return rel
    files = _qc._extract_quality_failure_files([text])
    return files[0] if files else ""



_SCOPE_DRIFT_FEEDBACK_MARKERS = (
    "unauthorized scope",
    "scope drift",
    "role-boundary violation",
    "role boundary violation",
    "prohibited_files",
    "prohibited files",
    "do_not_touch",
    "do not touch",
    "outside declared target_files",
    "outside master plan target_files",
)


_REVERT_FEEDBACK_MARKERS = ("revert", "restore", "rollback", "roll back")



def _has_scope_drift_marker(item):
    text = str(item or "").lower()
    return any(marker in text for marker in _qc._SCOPE_DRIFT_FEEDBACK_MARKERS)



def _scope_drift_feedback_files(item):
    """Return the actual files that a reviewer asks to revert/restore.

    Reviewer feedback can begin with positive context like "policy.py changes
    are compliant" and only later say "However,
    national_bot.py was in do_not_touch; revert it". The first file mention is
    then explicitly not the repair target. Parse scope-drift/revert cues before
    falling back to generic primary-file extraction.
    """

    text = str(item or "")
    lower = text.lower()
    if not any(marker in lower for marker in _qc._SCOPE_DRIFT_FEEDBACK_MARKERS + _qc._REVERT_FEEDBACK_MARKERS):
        return []

    candidates = []

    def add(value):
        rel = Path(str(value)).name
        if rel and rel.endswith(".py") and rel not in candidates:
            candidates.append(rel)

    for pattern in (
        r"\b(?:revert|restore|rollback|roll\s+back)\s+(?:bots/[A-Za-z0-9_./-]+/)?([A-Za-z0-9_./-]+\.py)\b",
        r"\b([A-Za-z0-9_./-]+\.py)\b[^.\n;]{0,220}\b(?:do_not_touch|do\s+not\s+touch|prohibited_files|prohibited\s+files)\b",
        r"\b([A-Za-z0-9_./-]+\.py)\b[^.\n;]{0,220}\b(?:unauthorized\s+scope|scope\s+drift|role-boundary\s+violation|role\s+boundary\s+violation)\b",
    ):
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            add(match.group(1))

    for part in re.split(r"(?i)\b(?:however|but|nevertheless)\b[:,]?\s*", text)[1:]:
        part_lower = part.lower()
        if any(marker in part_lower for marker in _qc._SCOPE_DRIFT_FEEDBACK_MARKERS + _qc._REVERT_FEEDBACK_MARKERS):
            for filename in _qc._extract_quality_failure_files([part]):
                add(filename)

    return candidates



def _feedback_quality_contracts(feedback):
    """Return file-scoped contracts from reviewer feedback.

    Reviewer prose often names helper files while describing a policy-consumer
    problem, for example "ranges.py returns fields never read by policy.py".
    Use the first/primary file in the issue snippet as the repair target instead
    of expanding to every mentioned file.
    """
    by_file = {}
    for item in _qc._split_reviewer_quality_feedback(feedback):
        scope_files = _qc._scope_drift_feedback_files(item)
        targets = scope_files or [_qc._primary_feedback_file(item)]
        for rel in targets:
            if not rel:
                continue
            by_file.setdefault(rel, []).append(item)

    contracts = []
    for rel in sorted(by_file):
        evidence = "\n".join(dict.fromkeys(by_file[rel]))
        contract = {
            "blocker": "quality_gate",
            "file": rel,
            "evidence": evidence,
        }
        lower = evidence.lower()
        if (
            rel == "policy.py"
            and (
                "hyperparameter tuner" in lower
                or "role boundary" in lower
                or "existing numeric" in lower
                or "existing constant" in lower
                or "threshold" in lower
            )
        ):
            contract["role_hint"] = "tuner"
        if _qc._scope_drift_feedback_files(evidence) and _qc._has_scope_drift_marker(evidence):
            contract["role_hint"] = "scope_revert"
        contracts.append(contract)
    return contracts



def _generic_quality_contracts(
    quality,
    failures,
    claimed_files,
    architecture_contracts=None,
):
    """Build file-scoped fallback contracts for non-mechanical quality blockers."""
    evidence_items = []
    for key in (
        "compile_errors",
        "import_errors",
        "protected_contract_errors",
        "national_native_contract_errors",
        "smoke_errors",
        "national_protocol_errors",
        "national_acceptance_errors",
        "critical_failures",
        "reachability_warnings",
    ):
        evidence_items.extend(_qc._flatten_text_items(quality.get(key)))
    evidence_items = [
        item for item in evidence_items
        if not _qc._is_declared_scope_failure_text(item)
        and not _qc._is_national_native_contract_failure_text(item)
        and not _qc._is_official_smoke_protocol_failure_text(item)
        and not _qc._is_runtime_architecture_failure_text(item)
    ]
    if not evidence_items:
        evidence_items = [
            item for item in failures
            if not str(item).startswith("file_size(")
            and not _qc._is_position_semantics_failure_text(item)
            and not _qc._is_declared_scope_failure_text(item)
            and not _qc._is_national_native_contract_failure_text(item)
            and not _qc._is_official_smoke_protocol_failure_text(item)
            and not _qc._is_runtime_architecture_failure_text(item)
        ]
    evidence_files = _qc._extract_quality_failure_files(evidence_items)
    mechanical_files = {c["file"] for c in _qc._line_count_contracts(quality, failures)}
    mechanical_files.update(c["file"] for c in _qc._position_contracts(quality))
    mechanical_files.update(c["file"] for c in _qc._national_native_contracts(quality, failures))
    mechanical_files.update(c["file"] for c in _qc._official_smoke_contracts(quality, failures))
    mechanical_files.update(c["file"] for c in architecture_contracts or [])
    if not evidence_items:
        return []
    generic_files = evidence_files or [f for f in claimed_files if f not in mechanical_files]
    if not generic_files:
        return []

    contracts = []
    for rel in generic_files:
        matching = [item for item in evidence_items if rel in str(item)]
        contracts.append({
            "blocker": "quality_gate",
            "file": rel,
            "evidence": "\n".join(str(item) for item in (matching or evidence_items)[:8]),
        })
    return contracts



def _quality_repair_contracts(ckpt, feedback=""):
    if not isinstance(ckpt, dict):
        return []
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    failures = _qc._quality_failure_items(ckpt)
    claimed_files = _qc._extract_quality_failure_files(failures)
    if not claimed_files and feedback:
        claimed_files = _qc._extract_quality_failure_files([feedback])
    violation_files = _qc._declared_scope_violation_files(ckpt, feedback)
    if violation_files:
        claimed_files = [
            filename for filename in claimed_files if filename not in violation_files
        ]
    architecture_contracts = _qc._architecture_contracts(quality, ckpt)
    contracts = []
    contracts.extend(_qc._line_count_contracts(quality, failures))
    contracts.extend(_qc._position_contracts(quality))
    contracts.extend(_qc._national_native_contracts(quality, failures))
    contracts.extend(_qc._official_smoke_contracts(quality, failures))
    contracts.extend(architecture_contracts)
    contracts.extend(_qc._feedback_quality_contracts(feedback))
    contracts.extend(
        _qc._generic_quality_contracts(
            quality,
            failures,
            claimed_files,
            architecture_contracts=architecture_contracts,
        )
    )

    ordered = []
    seen = set()
    for contract in contracts:
        if Path(str(contract.get("file") or "")).name not in _qc._ACTIVE_CANDIDATE_WRITABLE_FILES:
            # System artifacts, extra modules, and undeclared files are not
            # repaired by candidate Workers. Their owning gate remains failed.
            continue
        key = (contract.get("blocker"), contract.get("file"))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(contract)
    return ordered


