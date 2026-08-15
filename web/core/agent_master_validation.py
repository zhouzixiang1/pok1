"""Master proposal schema validation and source symbol analysis.

Extracted from agent_master.py for maintainability.

All symbols are re-exported by agent_master.py for backward compatibility.
"""

"""Master Architect agent: plans worker tasks from frozen generation evidence."""

import ast
import hashlib
import json
import re
import time
from pathlib import Path

from bot_namespace import ACTIVE_BOT_PREFIX, bot_name, bot_relpath
from evolution_infra import (
    run_claude_query, substitute_template,
    get_logs_dir, _trim_to_budget, PROMPTS_DIR,
    MAX_MASTER_RETRIES,
    get_bot_dir, MAX_LINES_HARD_CAP,
)

from output_schema import (
    MASTER_PROPOSAL_FALSIFIER_PRIMARY,
    MASTER_PROPOSAL_FALSIFIER_TESTS,
    STATE_LEARNING_INTERVENTION_TARGET_ALIASES,
    STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS,
    STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS,
    STATE_LEARNING_PRIMARY_CHECKS,
    STATE_LEARNING_PRIMARY_PROMPT_TERMS,
    WORKER_PROMPT_MAX_CHARS,
    WORKER_PROMPT_MIN_CHARS,
    master_plan_executable_contract_text,
)
from llm_availability import LLMAvailabilityBlocked, gather_llm_fail_fast

import agent_master_symbol_graph as _symbol_graph
import agent_master_proposal_packet as _packet
import agent_master_proposal_primaries as _pp


# Keep the rendered strength hypothesis on a validator-known literal.  The
# former ``<W/L/D interval method>`` placeholder prompted natural-language
# values such as ``W/L/D bootstrap 95% CI``; all three Scouts and their one
# schema retry then failed the machine-readable uncertainty contract.
_PROPOSAL_STRENGTH_SAMPLE_FLOOR = ">=30_complete_matches"
_PROPOSAL_UNCERTAINTY_PROMPT_VALUE = "wilson_wld_interval"

# Closed aliases whose natural-language spellings ("fold rate", "fold-rate",
# "fold.rate") routinely appear in legitimate poker prose.  Bind these only at
# an underscore separator (the Python identifier form, e.g. ``fold_rate``) or
# as a compact token (``foldrate``); a space, hyphen, or dot is prose, not an
# alias reference.  Axis-name aliases such as ``terminal_response`` deliberately
# stay permissive so "terminal response" still binds.
_PROSE_PRONE_ALIASES = frozenset({
    "fold_rate",
    "call_rate",
    "raise_rate",
    "allin_rate",
})



def _proposal_schema_repair_guidance(
    projection_hints: tuple[str, ...],
    *,
    require_snapshot_evidence: bool,
    allowed_primaries: tuple[str, ...] | None = None,
) -> str:
    """Render bounded, error-directed Scout repair instructions.

    The final output contract already contains the complete schema.  Repeating
    a generic twelve-item tutorial on every retry increased the prompt while
    hiding the deterministic reason that actually failed.  Keep at most four
    canonical corrections, ordered by semantic risk.
    """

    hints = tuple(str(item) for item in projection_hints)
    guidance: list[str] = []

    def add(text: str) -> None:
        if text not in guidance:
            guidance.append(text)

    if any("proposal_json_object_required" in item for item in hints):
        add(
            "Emit the entire proposal as one JSON object only; no prose, "
            "fences, or trailing text outside that object."
        )
    if any("proposal_schema_mismatch" in item for item in hints):
        add(
            f"Set schema_version to exactly \"{_PROPOSAL_SCHEMA_VERSION}\" at the "
            "top of each proposal object."
        )
    if any("proposal_execution_mode_mismatch" in item for item in hints):
        add(
            "Set execution_mode to the exact value required by this generation's "
            "evidence_mode (strategy_implementation or "
            "fixed_blueprint_capability_audit); copy it verbatim."
        )
    if any("proposal_mechanism_foreign_targets" in item for item in hints):
        add(
            "Do not name, deny, preserve, or qualify any foreign closed target. "
            "State instead that all other decision_context fields are "
            "byte-identical."
        )
    if any("proposal_mechanism_shared_leaf" in item for item in hints):
        roots = tuple(
            sorted({
                STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
                for primary in allowed_primaries or ()
                if primary in STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS
            })
        )
        root_clause = (
            f" The only executable root for this frozen proposal is {roots[0]}."
            if len(roots) == 1
            else " Use only the root selected by the mapping row."
        )
        add(
            "Rewrite the complete object from scratch; do not preserve prior "
            "prose. In structural_change, expected_diff, and "
            "falsifier.intervention, use the selected root literal only, never "
            "a bare or qualified leaf, confidence field, or another opponent "
            "path."
            + root_clause
            + " State that all other decision_context fields are byte-identical."
        )
    if any("proposal_mechanism_root_scoped_unknown_leaf" in item for item in hints):
        add(
            "Under the selected root, keep only that root's known child fields; "
            "remove any unrecognized leaf and express the same fact through the "
            "root's existing fields only."
        )
    if any(
        "proposal_mechanism_target_missing" in item
        or "proposal_mechanism_target_mismatch" in item
        or "proposal_falsifier_intervention_target_mismatch" in item
        for item in hints
    ):
        add(
            "Copy the selected row's exact dot target into structural_change, "
            "expected_diff, and falsifier.intervention; bracket notation is "
            "only supplementary and never replaces that literal."
        )
    if any("proposal_mechanism_target_invalid" in item for item in hints):
        add(
            "Set mechanism_target to the exact dot target from one mapping row "
            "of the selected state_learning primary; no other path is valid."
        )
    if any("proposal_target_files_invalid" in item for item in hints):
        add(
            "Set target_files to exactly [\"policy.py\"]; no helper, asset, or "
            "system file may appear there."
        )
    if any(
        "proposal_mechanism_qualified_target_identifier_continuation" in item
        for item in hints
    ):
        add(
            "Do not append identifier characters to any owner-qualified target "
            "or field literal."
        )
    if any("proposal_reachable_chain" in item for item in hints):
        add(
            "Use current direct caller-to-callee edges ending exactly at the one "
            "existing change_symbol; put every chain symbol in source_symbols with "
            "matching source: evidence_refs. An unchanged entry anchor is invalid."
        )
    if any("proposal_change_symbol" in item for item in hints):
        add(
            "Set change_symbol to the exact existing file.py:symbol whose AST body "
            "the Worker will modify, name it in expected_diff, and make it the final "
            "reachable_chain item."
        )
    if any(
        "proposal_evidence" in item or "proposal_source_symbol" in item
        for item in hints
    ):
        add(
            "Use only exact file.py:symbol entries from the verified index and "
            "one matching bare source:file.py:symbol evidence_ref per symbol."
        )
    if any("proposal_snapshot_evidence_too_many" in item for item in hints):
        add(
            "You used more than 2 snapshot references; the maximum is 2. "
            "Keep only the strongest 1–2 exact validated snapshot JSON pointers."
        )
    elif any("proposal_snapshot" in item for item in hints):
        add(
            "Copy one exact validated snapshot JSON pointer (maximum 2)."
            if require_snapshot_evidence
            else "This mode has no strength snapshot; emit no snapshot reference."
        )
    if any("proposal_measurement" in item for item in hints):
        add(
            "Copy the mode-specific six-field measurement contract exactly as a "
            "single semicolon-separated string. For frozen_strength_snapshot/"
            "singleton_parent_no_strength use this template (substitute target "
            "and expected_delta): "
            "\"target=<bot_name>; primary=complete_70_hand_wld; "
            "expected_delta=<0.0<d<=1.0>; samples="
            + _PROPOSAL_STRENGTH_SAMPLE_FLOOR
            + "; uncertainty=" + _PROPOSAL_UNCERTAINTY_PROMPT_VALUE
            + "; secondary=net_chip_ci\". Never replace literals with "
            "natural-language W/L/D prose."
        )
    if any("proposal_falsifier" in item for item in hints):
        add(
            "falsifier is a closed six-key object: test_name, "
            "state_learning_primary, intervention_target, control, intervention, "
            "expected_observation. Delete every extra key; mechanism_target "
            "appears exactly once at the top level. Copy test_name, "
            "state_learning_primary, mechanism_target, and intervention_target "
            "from one mapping row without relabelling it."
        )
    if not guidance:
        add(
            "Re-emit one complete object and repair only the canonical projection "
            "errors below; preserve the assigned lens and evidence scope."
        )
    return "\n".join(f"- {item}" for item in guidance[:4])


# Master prompt rendering (the three _render_*_provider_prompt functions, the
# analysis-section renderer, and PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER)
# now lives in agent_master_prompts.py.  Re-importing it here would create a
# load-time cycle (agent_master_prompts reaches validator helpers via a
# live-attribute handle to this module), so callers should import the
# renderers from agent_master_prompts or agent_master directly.


# Error types and advisory-analysis sentinels live in agent_master_errors so
# callers can import them without pulling in the full schema/parser graph.
# Re-imported here for back-compat with code that historically read these names
# from the validation module; new callers should import agent_master_errors
# (or, preferably, agent_master) directly.
from agent_master_errors import (  # noqa: F401
    LLM_INFRA_SENTINEL,
    LLM_INFRA_SENTINEL_MSG,
    MasterAuthorityError,
    MasterEnsembleInfrastructureParked,
    MasterInfrastructureError,
)


_MASTER_PROPOSAL_DIRECTIONS = (
    (
        "mechanism",
        "Propose one structural mechanism that replaces a reachable parent behavior; "
        "threshold-only tuning is invalid.",
    ),
    (
        "counterfactual",
        "Start from one falsifiable counterfactual/control and design the smallest "
        "reachable mechanism that could make it pass.",
    ),
    (
        "compute_memory",
        "Explore bounded precomputation, anytime decision work, or persistent match "
        "memory only when the injected policy/evidence makes that axis eligible.",
    ),
)


_PROPOSAL_SCHEMA_VERSION = "master-proposal-v4"
_PROPOSAL_PACKET_SCHEMA_VERSION = "master-proposal-packet-v6"
_PROPOSAL_REPAIR_EOF_OBJECT_PARSE_MODE = (
    "master-proposal-repair-eof-json-object-v1"
)
_POLICY_ABI_ENTRYPOINT_SYMBOLS = _symbol_graph._POLICY_ABI_ENTRYPOINT_SYMBOLS
_DECISION_RELEVANT_SYMBOL_TERMS = _symbol_graph._DECISION_RELEVANT_SYMBOL_TERMS
_UTILITY_SYMBOL_TERMS = _symbol_graph._UTILITY_SYMBOL_TERMS
_PROPOSAL_FALSIFIER_TESTS = MASTER_PROPOSAL_FALSIFIER_TESTS


def _proposal_falsifier_primary(test_name: object) -> str | None:
    """Delegate to agent_master_proposal_primaries companion."""
    return _pp._proposal_falsifier_primary(test_name)


def _canonical_proposal_primaries(
    values: object,
) -> tuple[str, ...] | None:
    """Delegate to agent_master_proposal_primaries companion."""
    return _pp._canonical_proposal_primaries(values)


def _architecture_proposal_primaries(
    architecture_policy: dict | None,
) -> tuple[str, ...] | None:
    """Delegate to agent_master_proposal_primaries companion."""
    return _pp._architecture_proposal_primaries(architecture_policy)


def _proposal_falsifier_mapping_text(
    *,
    allowed_primaries: tuple[str, ...] | None = None,
) -> str:
    """Delegate to agent_master_proposal_primaries companion."""
    return _pp._proposal_falsifier_mapping_text(allowed_primaries=allowed_primaries)


def _proposal_closed_json_shape() -> str:
    """Delegate to agent_master_proposal_primaries companion."""
    return _pp._proposal_closed_json_shape()


def _proposal_mechanism_target_errors(
    proposal: dict,
    falsifier: dict,
) -> tuple[str, ...]:
    """Delegate to agent_master_proposal_primaries companion."""
    return _pp._proposal_mechanism_target_errors(proposal, falsifier)



_PROPOSAL_CRITIC_CRITERIA = {
    "evidence_traceability": (
        "Every claimed source fact is bound to a verified source symbol or a frozen "
        "snapshot node with a digest-bound resolved projection."
    ),
    "runtime_reachability": (
        "The verified parent call chain reaches a file that the proposal will edit."
    ),
    "falsifiability": (
        "The control/intervention/expected observation can disprove the mechanism."
    ),
    "causal_attribution": (
        "The structured mechanism_target equals the falsifier intervention_target, "
        "and the executable claim varies only that target rather than unrelated "
        "threshold or profile axes."
    ),
    "frozen_strength_relevance": (
        "When a frozen strength snapshot exists, bind one concrete weakness and "
        "affected decision frequency. In a declared zero-strength generation, "
        "require a poker-decision mechanism with a measurable parent/control "
        "counterfactual and explicitly make no measured-strength claim. Protocol "
        "compliance, observability, and code novelty alone never suffice."
    ),
    "bounded_regression_risk": (
        "The implementation scope and fallback make regressions observable and bounded."
    ),
}

_STRENGTH_SNAPSHOT_FILENAMES = frozenset({
    "head_to_head.json",
    "selection_snapshot.json",
    "glicko_ratings.json",
    "bot_stats.json",
    "bot_action_stats.json",
    "bot_action_stats_per_opp.json",
    "replay_spotlight.json",
})
_SNAPSHOT_METADATA_ONLY_TERMINALS = frozenset({
    "active_bots",
    "schema_version",
    "manifest_digest",
    "evaluation_identity_digest",
    "sha256",
    "bytes",
    "entries",
    "save_num",
    "daemon_run_id",
    "created_at",
    "generated_at",
    "epoch",
    "version",
    "source_v",
    "next_v",
})
_SNAPSHOT_STRENGTH_SIGNAL_KEYS = frozenset({
    "games",
    "a_wins",
    "b_wins",
    "wins",
    "losses",
    "draws",
    "win_rate",
    "selection_score",
    "leaderboard_score",
    "h2h_avg_wr",
    "h2h_games",
    "h2h_coverage",
    "strength_confidence",
    "r",
    "rd",
    "sigma",
    "total_actions",
    "actions",
    "folds",
    "calls",
    "checks",
    "raises",
    "allins",
    "net_chips",
    "secondary_net_chips_mean",
})

_PROPOSAL_SUBSTANTIVE_FIELDS = (
    "schema_version",
    "targeted_failure",
    "structural_change",
    "counterfactual",
    "measurement",
    "why_not_threshold_tuning",
    "mechanism_target",
    "expected_diff",
    "target_files",
    "source_symbols",
    "change_symbol",
    "reachable_chain",
    "falsifier",
    "evidence_refs",
    "snapshot_evidence",
    "execution_mode",
)

_PROPOSAL_MEASUREMENT_FIELDS = (
    "target",
    "primary",
    "expected_delta",
    "samples",
    "uncertainty",
    "secondary",
)

_FRESH_STRICT_CONTROL_MEASUREMENT = (
    "target=fixed_blueprint_control; "
    "primary=typed_falsifier_and_official_5_plus_3; "
    "expected_delta=not_applicable; samples=official_5_plus_3; "
    "uncertainty=no_strength_claim; secondary=none"
)


def _system_bound_proposal_measurement(
    raw_value: object,
    evidence_mode: str | None,
) -> str | None:
    """Return a system-owned bootstrap measurement after field-presence proof.

    A fresh strict control has no opponent, strength sample, or measurement
    choice.  Its six-field measurement is therefore fixed by the bootstrap
    contract, just like final-plan metadata is fixed by the selected proposal.
    The provider must still emit the closed six-field shape.  Once that shape
    is present, punctuation or a paraphrase in its values cannot alter the
    immutable no-strength claim: the system substitutes the canonical control
    contract.  Missing, non-string, and malformed fields remain invalid so
    this is not a schema bypass.
    """

    if evidence_mode != "fresh_strict_control_no_strength":
        return None
    if (
        not isinstance(raw_value, str)
        or _parsed_proposal_measurement(raw_value) is None
    ):
        return None
    return _FRESH_STRICT_CONTROL_MEASUREMENT


def _parsed_proposal_measurement(value: str) -> dict[str, str] | None:
    """Parse the six-field strength hypothesis without substring loopholes."""

    parts = [part.strip() for part in str(value or "").split(";") if part.strip()]
    if len(parts) != len(_PROPOSAL_MEASUREMENT_FIELDS):
        return None
    parsed: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            return None
        key, item = part.split("=", 1)
        # The measurement string is an unproven post-publication strength
        # hypothesis (see the Master prompt): the downstream native precommit
        # and elo_daemon recompute strength from real 70-hand matches and do
        # not read this field.  Normalize case after stripping whitespace so a
        # model's natural capitalization of the canonical machine literals is
        # not rejected; all contract constants are lowercase, so case-folding
        # cannot weaken the evidence_mode split in
        # _proposal_measurement_contract_valid.
        key = key.strip().lower()
        item = item.strip().lower()
        if not key or not item or key in parsed:
            return None
        parsed[key] = item
    if tuple(parsed) != _PROPOSAL_MEASUREMENT_FIELDS:
        return None
    return parsed


def _proposal_measurement_contract_valid(value: str, evidence_mode: str) -> bool:
    """Require one machine-readable generation hypothesis, not vague test prose."""

    parsed = _parsed_proposal_measurement(value)
    if parsed is None:
        return False
    if evidence_mode == "fresh_strict_control_no_strength":
        return parsed == _parsed_proposal_measurement(
            _FRESH_STRICT_CONTROL_MEASUREMENT
        )
    elif evidence_mode in {
        "frozen_strength_snapshot",
        "singleton_parent_no_strength",
    }:
        if not re.fullmatch(rf"{re.escape(ACTIVE_BOT_PREFIX)}[1-9][0-9]*", parsed["target"]):
            return False
        try:
            expected_delta = float(parsed["expected_delta"])
        except (TypeError, ValueError):
            return False
        uncertainty = parsed["uncertainty"]
        return bool(
            0.0 < expected_delta <= 1.0
            and parsed["primary"] == "complete_70_hand_wld"
            and parsed["samples"] == _PROPOSAL_STRENGTH_SAMPLE_FLOOR
            and uncertainty == _PROPOSAL_UNCERTAINTY_PROMPT_VALUE
            and parsed["secondary"] == "net_chip_ci"
        )
    else:
        return False


def _measurement_target_bound_to_snapshot(
    measurement: str,
    snapshot_evidence: list[dict],
) -> bool:
    parsed = _parsed_proposal_measurement(measurement)
    if parsed is None:
        return False
    target = parsed["target"]
    bound_bots: set[str] = set()
    for binding in snapshot_evidence:
        if not isinstance(binding, dict):
            continue
        bound_bots.update(re.findall(
            rf"{re.escape(ACTIVE_BOT_PREFIX)}[1-9][0-9]*",
            (
                str(binding.get("reference") or "")
                + "\n"
                + str(binding.get("resolved_projection") or "")
            ).lower(),
        ))
    return target in bound_bots


def _snapshot_node_has_strength_signal(node: object, filename: str) -> bool:
    """Reject pool/manifest containers that do not contain a strength fact."""

    if isinstance(node, str):
        return filename == "replay_spotlight.json" and len(node.strip()) >= 40
    if isinstance(node, dict):
        keys = {str(key).lower() for key in node}
        if keys.intersection(_SNAPSHOT_STRENGTH_SIGNAL_KEYS):
            return True
        return any(
            _snapshot_node_has_strength_signal(value, filename)
            for value in list(node.values())[:64]
            if isinstance(value, (dict, list))
        )
    if isinstance(node, list):
        return any(
            _snapshot_node_has_strength_signal(item, filename)
            for item in node[:64]
            if isinstance(item, (dict, list))
        )
    return False


def _safe_relative_python_path(value: object) -> str | None:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._safe_relative_python_path(value)


def _source_symbol_graph(source_dir: Path) -> tuple[dict[str, set[str]], str]:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._source_symbol_graph(source_dir)


def _source_symbol_ast_digest(source_dir: Path, symbol: str) -> str | None:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._source_symbol_ast_digest(source_dir, symbol)


def _proposal_source_symbol_digests(
    proposals: list[dict],
    source_dir: Path,
) -> dict[str, dict[str, str]]:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._proposal_source_symbol_digests(proposals, source_dir)


def _verified_source_edges(
    graph: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._verified_source_edges(graph)


def _policy_abi_reachable_depths(
    graph: dict[str, set[str]],
) -> dict[str, int]:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._policy_abi_reachable_depths(graph)


def _source_symbol_prompt_index(
    graph: dict[str, set[str]],
    *,
    maximum_chars: int = 18_000,
) -> str:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._source_symbol_prompt_index(graph, maximum_chars=maximum_chars)


def _snapshot_reference_prompt_index(snapshot_dir: Path) -> str:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._snapshot_reference_prompt_index(snapshot_dir)


def _normalize_source_symbol(value: object) -> str | None:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._normalize_source_symbol(value)


def _fuzzy_resolve_symbol(
    symbol: str,
    source_graph: dict,
    *,
    emit_event: bool = True,
) -> str | None:
    """Delegate to agent_master_symbol_graph (symbol-graph subsystem)."""
    return _symbol_graph._fuzzy_resolve_symbol(
        symbol, source_graph, emit_event=emit_event
    )


def _validated_snapshot_reference(value: object, snapshot_dir: Path | None) -> str | None:
    text = str(value or "").strip()
    if not text.startswith("snapshot:") or "#" not in text:
        return None
    path_text, locator = text[len("snapshot:"):].split("#", 1)
    relative = Path(path_text.strip().replace("\\", "/"))
    if (
        snapshot_dir is None
        or not path_text.strip()
        or relative.is_absolute()
        or ".." in relative.parts
        or not locator.strip()
    ):
        return None
    root = Path(snapshot_dir).resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    locator = locator.strip()
    if candidate.suffix.lower() != ".json" or not locator.startswith("/"):
        return None
    try:
        node = json.loads(candidate.read_text(encoding="utf-8"))
        for raw_part in locator[1:].split("/") if locator != "/" else []:
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict):
                if part not in node:
                    return None
                node = node[part]
            elif isinstance(node, list):
                if not part.isdigit() or int(part) >= len(node):
                    return None
                node = node[int(part)]
            else:
                return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return f"snapshot:{relative.as_posix()}#{locator[:240]}"


def _snapshot_reference_evidence_binding(
    value: object,
    snapshot_dir: Path | None,
) -> dict | None:
    """Bind one strength-bearing snapshot node, not a metadata-only locator."""

    reference = _validated_snapshot_reference(value, snapshot_dir)
    if reference is None or snapshot_dir is None:
        return None
    path_text, locator = reference[len("snapshot:"):].split("#", 1)
    relative = Path(path_text)
    if (
        relative.as_posix() not in _STRENGTH_SNAPSHOT_FILENAMES
        or locator == "/"
    ):
        return None
    parts = [
        raw.replace("~1", "/").replace("~0", "~")
        for raw in locator[1:].split("/")
        if raw
    ]
    if not parts or parts[-1].lower() in _SNAPSHOT_METADATA_ONLY_TERMINALS:
        return None
    try:
        node = json.loads(
            (Path(snapshot_dir) / relative).read_text(encoding="utf-8")
        )
        for part in parts:
            if isinstance(node, dict):
                node = node[part]
            elif isinstance(node, list):
                node = node[int(part)]
            else:
                return None
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ):
        return None
    if not _snapshot_node_has_strength_signal(node, relative.as_posix()):
        # Pool membership, manifest identity, and naked scalar values cannot
        # explain a weakness.  Admit only a bounded row/container containing a
        # concrete W/L/D, rating, action, chip, or replay-strength signal.
        return None
    canonical = json.dumps(
        node,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(canonical) < 20:
        return None
    projection = canonical[:1600]
    return {
        "reference": reference,
        "node_sha256": hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
        "resolved_projection": projection,
        "projection_sha256": hashlib.sha256(
            projection.encode("utf-8")
        ).hexdigest(),
        "projection_truncated": len(canonical) > 1600,
    }


def _proposal_substantive_contract(proposal: dict) -> dict:
    """Return only decision-bearing mechanism claims used for diversity.

    ``direction`` is scout routing and ``risks`` is advisory prose.  Neither
    may make an otherwise identical mechanism count as an independent option.
    """

    return {
        key: proposal.get(key)
        for key in _PROPOSAL_SUBSTANTIVE_FIELDS
    }


def _proposal_identity(proposal: dict) -> str:
    identity_payload = _proposal_substantive_contract(proposal)
    return hashlib.sha256(
        json.dumps(
            identity_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]


def _master_proposal_repair_kind(
    direction: str,
    actual_role: str | None,
) -> str | None:
    """Return the sealed repair kind for one exact Scout provider role.

    A provider's prose cannot opt itself into recovery.  The caller must pass
    the system-owned role used for that invocation; the first attempt and
    every unrelated role remain on the repository-wide strict parser.
    """

    base_role = f"MASTER PROPOSAL {str(direction)}"
    role = str(actual_role or "")
    if role == base_role + " SCHEMA RETRY":
        return "schema"
    if role == base_role + " DISTINCTNESS RETRY":
        return "distinctness"
    return None


def _parse_master_proposal_output_with_mode(
    output: str,
    direction: str,
    *,
    actual_role: str | None = None,
) -> tuple[object | None, str]:
    """Parse a Scout result without widening the global JSON contract.

    Every output first uses the repository-wide parser, preserving all existing
    successful raw/fenced shapes.  Only when that parser fails may the one
    schema/distinctness repair recover the exact v54 failure shape: non-JSON
    prose followed by one complete JSON object with no trailing non-whitespace
    bytes.  Braces/brackets in the prefix make the object boundary ambiguous;
    arrays, multiple objects, malformed JSON, and trailing prose fail closed.
    """

    from llm_query import parse_json_output_with_mode

    raw = str(output or "")
    parsed, global_mode = parse_json_output_with_mode(raw)
    if parsed is not None:
        return parsed, global_mode
    if _master_proposal_repair_kind(direction, actual_role) is None:
        return None, global_mode
    if not raw.strip():
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_REJECTED"
    object_start = raw.find("{")
    if object_start < 0:
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_REJECTED"
    prefix = raw[:object_start]
    if prefix.strip() and set(prefix).intersection("{}[]"):
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_AMBIGUOUS_PREFIX"
    candidate = raw[object_start:]
    try:
        parsed, end = json.JSONDecoder().raw_decode(candidate)
    except (TypeError, ValueError):
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_REJECTED"
    if candidate[end:].strip() or not isinstance(parsed, dict):
        return None, "PROPOSAL_REPAIR_EOF_OBJECT_REJECTED"
    return parsed, _PROPOSAL_REPAIR_EOF_OBJECT_PARSE_MODE


def _validated_master_proposal(
    output: str,
    direction: str,
    *,
    source_graph: dict[str, set[str]] | None = None,
    snapshot_dir: Path | None = None,
    national_policy_only: bool = False,
    require_snapshot_evidence: bool = False,
    execution_mode: str = "strategy_implementation",
    evidence_mode: str | None = None,
    expected_measurement_target: str | None = None,
    forbidden_measurement_target: str | None = None,
    enforce_bindability: bool = True,
    allowed_primaries: tuple[str, ...] | None = None,
    actual_role: str | None = None,
) -> dict | None:
    """Normalize one evidence-bound proposal before critics or Master see it."""
    allowed_primaries = _canonical_proposal_primaries(allowed_primaries)
    data, _mode = _parse_master_proposal_output_with_mode(
        output or "",
        direction,
        actual_role=actual_role,
    )
    if not isinstance(data, dict):
        return None
    if any(data.get(key) for key in ("branch_from", "source_override", "source_v_override")):
        return None
    required = (
        "targeted_failure",
        "structural_change",
        "counterfactual",
        "measurement",
        "why_not_threshold_tuning",
        "expected_diff",
    )
    normalized = {
        "schema_version": _PROPOSAL_SCHEMA_VERSION,
        "direction": direction,
    }
    if execution_mode not in {
        "strategy_implementation",
        "fixed_blueprint_capability_audit",
    }:
        return None
    normalized["execution_mode"] = execution_mode
    raw_mechanism_target = data.get("mechanism_target")
    if not isinstance(raw_mechanism_target, str):
        return None
    mechanism_target = raw_mechanism_target.strip()
    if mechanism_target not in set(
        STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS.values()
    ):
        return None
    normalized["mechanism_target"] = mechanism_target
    for key in required:
        value = (
            _system_bound_proposal_measurement(data.get(key), evidence_mode)
            if key == "measurement"
            else None
        ) or str(data.get(key) or "").strip()
        if len(value) < 20:
            return None
        normalized[key] = value[:1600]
    if evidence_mode is not None and not _proposal_measurement_contract_valid(
        normalized["measurement"], evidence_mode
    ):
        return None
    raw_files = data.get("target_files") or []
    if not isinstance(raw_files, list):
        return None
    target_files = []
    for value in raw_files[:3]:
        name = _safe_relative_python_path(value)
        if name is None or name in target_files:
            continue
        target_files.append(name)
    if not target_files:
        return None
    if national_policy_only:
        if target_files != ["policy.py"]:
            return None
    normalized["target_files"] = target_files

    raw_symbols = data.get("source_symbols")
    if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= 8:
        return None
    source_symbols: list[str] = []
    for raw_symbol in raw_symbols:
        symbol = _normalize_source_symbol(raw_symbol)
        if symbol is None or symbol in source_symbols:
            return None
        if source_graph is not None and symbol not in source_graph:
            resolved = _fuzzy_resolve_symbol(symbol, source_graph)
            if resolved is None or resolved in source_symbols:
                return None
            symbol = resolved
        source_symbols.append(symbol)
    normalized["source_symbols"] = source_symbols

    change_symbol = _normalize_source_symbol(data.get("change_symbol"))
    if change_symbol is not None and source_graph is not None and change_symbol not in source_graph:
        change_symbol = _fuzzy_resolve_symbol(change_symbol, source_graph)
    if change_symbol is None or change_symbol not in source_symbols:
        return None
    if change_symbol.rsplit(":", 1)[0] not in target_files:
        return None
    normalized["change_symbol"] = change_symbol

    raw_chain = data.get("reachable_chain")
    if not isinstance(raw_chain, list) or not 2 <= len(raw_chain) <= 8:
        return None
    chain: list[str] = []
    for raw_symbol in raw_chain:
        symbol = _normalize_source_symbol(raw_symbol)
        if symbol is None:
            return None
        if symbol not in source_symbols and source_graph is not None:
            resolved = _fuzzy_resolve_symbol(symbol, source_graph)
            if resolved is not None and resolved in source_symbols:
                symbol = resolved
        if symbol not in source_symbols:
            return None
        chain.append(symbol)
    if len(set(chain)) != len(chain):
        return None
    if not chain or chain[-1] != change_symbol:
        return None
    if source_graph is not None:
        verified_edges = _verified_source_edges(source_graph)
        for caller, callee in zip(chain, chain[1:]):
            if callee not in verified_edges.get(caller, ()):
                return None
        if (
            national_policy_only
            and chain[0] not in _policy_abi_reachable_depths(source_graph)
        ):
            return None
    chain_files = {item.rsplit(":", 1)[0] for item in chain}
    if not chain_files.intersection(target_files):
        return None
    normalized["reachable_chain"] = chain

    falsifier = data.get("falsifier")
    falsifier_fields = {
        "test_name",
        "state_learning_primary",
        "intervention_target",
        "control",
        "intervention",
        "expected_observation",
    }
    if not isinstance(falsifier, dict) or set(falsifier) != falsifier_fields:
        return None
    normalized_falsifier = {}
    for key in falsifier_fields:
        raw_value = falsifier.get(key)
        if not isinstance(raw_value, str):
            return None
        value = raw_value.strip()
        minimum = 3 if key in {
            "test_name",
            "state_learning_primary",
            "intervention_target",
        } else 20
        if len(value) < minimum:
            return None
        if key == "test_name" and not value.replace("_", "").isalnum():
            return None
        if key == "test_name" and value not in _PROPOSAL_FALSIFIER_TESTS:
            return None
        normalized_falsifier[key] = value[:1000]
    primary = _proposal_falsifier_primary(normalized_falsifier["test_name"])
    if (
        primary is None
        or (
            allowed_primaries is not None
            and primary not in allowed_primaries
        )
        or normalized_falsifier["test_name"]
        not in STATE_LEARNING_PRIMARY_CHECKS[primary]
        or normalized_falsifier["state_learning_primary"] != primary
        or normalized_falsifier["intervention_target"]
        != STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
        or _proposal_mechanism_target_errors(
            normalized,
            normalized_falsifier,
        )
    ):
        return None
    normalized["falsifier"] = normalized_falsifier
    raw_refs = data.get("evidence_refs")
    # Weak models sometimes produce evidence_refs as a dict instead of a list.
    # Coerce dict values to a list so we don't reject otherwise valid proposals.
    if isinstance(raw_refs, dict):
        raw_refs = list(raw_refs.values())
    if not isinstance(raw_refs, list) or not 1 <= len(raw_refs) <= 10:
        return None
    evidence_refs: list[str] = []
    snapshot_evidence: list[dict] = []
    source_ref_symbols: set[str] = set()
    snapshot_ref_count = 0
    for raw_ref in raw_refs:
        text = str(raw_ref or "").strip()
        normalized_ref = None
        # Accept source:, call_index:, code:, ref: prefixes as source references.
        # Weak models frequently use the wrong prefix; the integrity guarantee
        # is that the symbol exists in the frozen source graph, not the label.
        matched_source = False
        for prefix in ("source:", "call_index:", "code:", "ref:"):
            if text.lower().startswith(prefix):
                matched_source = True
                remainder = text[len(prefix):].strip()
                # Strip trailing descriptions that weak models append.
                for sep in (" [", " \u2014", " -", " (", "\t"):
                    idx = remainder.find(sep)
                    if idx > 0:
                        remainder = remainder[:idx].strip()
                symbol = _normalize_source_symbol(remainder)
                if symbol is not None:
                    if symbol not in source_symbols and source_graph is not None:
                        resolved = _fuzzy_resolve_symbol(symbol, source_graph)
                        if resolved is not None and resolved in source_symbols:
                            symbol = resolved
                    if symbol in source_symbols:
                        normalized_ref = f"source:{symbol}"
                        source_ref_symbols.add(symbol)
                break
        if not matched_source and text.startswith("snapshot:"):
            binding = _snapshot_reference_evidence_binding(text, snapshot_dir)
            if binding is not None:
                normalized_ref = binding["reference"]
                snapshot_evidence.append(binding)
                snapshot_ref_count += 1
        if normalized_ref is None or normalized_ref in evidence_refs:
            return None
        evidence_refs.append(normalized_ref)
    if source_ref_symbols != set(source_symbols):
        return None
    if snapshot_ref_count > 3:
        return None
    if require_snapshot_evidence and snapshot_ref_count < 1:
        return None
    normalized["evidence_refs"] = evidence_refs
    normalized["snapshot_evidence"] = snapshot_evidence
    if (
        evidence_mode == "frozen_strength_snapshot"
        and not _measurement_target_bound_to_snapshot(
            normalized["measurement"],
            snapshot_evidence,
        )
    ):
        return None
    if expected_measurement_target is not None:
        parsed_measurement = _parsed_proposal_measurement(
            normalized["measurement"]
        )
        if (
            parsed_measurement is None
            or parsed_measurement["target"]
            != str(expected_measurement_target).strip().lower()
        ):
            return None
    if forbidden_measurement_target is not None:
        parsed_measurement = _parsed_proposal_measurement(
            normalized["measurement"]
        )
        if (
            parsed_measurement is None
            or parsed_measurement["target"]
            == str(forbidden_measurement_target).strip().lower()
        ):
            return None

    risks = str(data.get("risks") or "").strip()
    if len(risks) < 20:
        return None
    normalized["risks"] = risks[:1200]

    # Identity is a pure function of the proposal claims and verified evidence,
    # not scout identity, critic order, generation number, or wall clock.
    normalized["proposal_id"] = _proposal_identity(normalized)
    if enforce_bindability and _proposal_worker_bindability_error(normalized):
        return None
    return normalized


def _master_proposal_projection_hints(
    output: str,
    *,
    source_graph: dict[str, set[str]] | None = None,
    snapshot_dir: Path | None = None,
    national_policy_only: bool = False,
    require_snapshot_evidence: bool = False,
    evidence_mode: str | None = None,
    allowed_primaries: tuple[str, ...] | None = None,
) -> list[str]:
    """Return stable field-level hints without weakening proposal validation.

    Acceptance remains owned exclusively by :func:`_validated_master_proposal`.
    These codes explain common deterministic rejection points to the one
    existing schema-repair attempt, so it does not have to guess which part of
    the large object failed.  Hints never contain provider prose or paths.
    """

    from llm_query import parse_json_output_with_mode

    try:
        allowed_primaries = _canonical_proposal_primaries(allowed_primaries)
    except ValueError:
        return ["proposal_allowed_primaries_invalid"]

    data, _mode = parse_json_output_with_mode(output or "")
    if not isinstance(data, dict):
        return ["proposal_json_object_required"]
    errors: list[str] = []
    if any(data.get(key) for key in (
        "branch_from", "source_override", "source_v_override",
    )):
        errors.append("proposal_source_override_forbidden")
    for key in (
        "targeted_failure",
        "structural_change",
        "counterfactual",
        "measurement",
        "why_not_threshold_tuning",
        "expected_diff",
    ):
        if len(str(data.get(key) or "").strip()) < 20:
            errors.append(f"proposal_required_text_invalid:{key}")
    mechanism_target = data.get("mechanism_target")
    if (
        not isinstance(mechanism_target, str)
        or mechanism_target.strip()
        not in set(STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS.values())
    ):
        errors.append("proposal_mechanism_target_invalid")
    measurement = (
        _system_bound_proposal_measurement(data.get("measurement"), evidence_mode)
        or str(data.get("measurement") or "")
    )
    if evidence_mode is not None and not _proposal_measurement_contract_valid(
        measurement, evidence_mode
    ):
        errors.append("proposal_measurement_contract_invalid")

    raw_files = data.get("target_files")
    target_files = []
    if isinstance(raw_files, list):
        for value in raw_files[:3]:
            normalized = _safe_relative_python_path(value)
            if normalized is not None and normalized not in target_files:
                target_files.append(normalized)
    if not target_files or (
        national_policy_only and target_files != ["policy.py"]
    ):
        errors.append("proposal_target_files_invalid")

    raw_symbols = data.get("source_symbols")
    source_symbols: list[str] = []
    if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= 8:
        errors.append("proposal_source_symbols_count_invalid")
    else:
        for value in raw_symbols:
            symbol = _normalize_source_symbol(value)
            if symbol is not None and source_graph is not None and symbol not in source_graph:
                symbol = _fuzzy_resolve_symbol(
                    symbol,
                    source_graph,
                    emit_event=False,
                )
            if symbol is None or symbol in source_symbols:
                errors.append("proposal_source_symbols_invalid")
                break
            source_symbols.append(symbol)

    change_symbol = _normalize_source_symbol(data.get("change_symbol"))
    if change_symbol is not None and source_graph is not None and change_symbol not in source_graph:
        change_symbol = _fuzzy_resolve_symbol(
            change_symbol,
            source_graph,
            emit_event=False,
        )
    if change_symbol is None or change_symbol not in source_symbols:
        errors.append("proposal_change_symbol_not_in_source_symbols")
    else:
        if change_symbol.rsplit(":", 1)[0] not in target_files:
            errors.append("proposal_change_symbol_not_in_target_files")

    raw_chain = data.get("reachable_chain")
    chain: list[str] = []
    if not isinstance(raw_chain, list) or not 2 <= len(raw_chain) <= 8:
        errors.append("proposal_reachable_chain_count_invalid")
    else:
        for value in raw_chain:
            symbol = _normalize_source_symbol(value)
            if symbol is None:
                errors.append("proposal_reachable_chain_symbol_invalid")
                break
            if symbol not in source_symbols and source_graph is not None:
                resolved = _fuzzy_resolve_symbol(
                    symbol,
                    source_graph,
                    emit_event=False,
                )
                if resolved is not None and resolved in source_symbols:
                    symbol = resolved
            chain.append(symbol)
        if len(chain) != len(set(chain)):
            errors.append("proposal_reachable_chain_duplicate")
        if chain and chain[-1] != change_symbol:
            errors.append("proposal_change_symbol_not_chain_terminal")
        if chain and any(symbol not in source_symbols for symbol in chain):
            errors.append("proposal_reachable_chain_member_not_in_source_symbols")
        if source_graph is not None and len(chain) >= 2:
            verified_edges = _verified_source_edges(source_graph)
            if any(
                callee not in verified_edges.get(caller, ())
                for caller, callee in zip(chain, chain[1:])
            ):
                errors.append("proposal_reachable_chain_edge_not_current")
            if (
                national_policy_only
                and chain[0] not in _policy_abi_reachable_depths(source_graph)
            ):
                errors.append(
                    "proposal_reachable_chain_not_policy_abi_reachable"
                )
        if chain and not {
            symbol.rsplit(":", 1)[0] for symbol in chain
        }.intersection(target_files):
            errors.append("proposal_reachable_chain_target_file_missing")

    falsifier = data.get("falsifier")
    falsifier_fields = {
        "test_name",
        "state_learning_primary",
        "intervention_target",
        "control",
        "intervention",
        "expected_observation",
    }
    if (
        not isinstance(falsifier, dict)
        or set(falsifier) != falsifier_fields
        or any(not isinstance(falsifier.get(key), str) for key in falsifier_fields)
        or any(
            len(falsifier[key].strip())
            < (
                3
                if key in {
                    "test_name",
                    "state_learning_primary",
                    "intervention_target",
                }
                else 20
            )
            for key in falsifier_fields
        )
    ):
        errors.append("proposal_falsifier_invalid")
    elif falsifier["test_name"].strip() not in _PROPOSAL_FALSIFIER_TESTS:
        errors.append("proposal_falsifier_test_name_invalid")
    else:
        test_name = falsifier["test_name"].strip()
        primary = _proposal_falsifier_primary(test_name)
        if primary is None or test_name not in STATE_LEARNING_PRIMARY_CHECKS[primary]:
            errors.append("proposal_falsifier_primary_mapping_invalid")
        elif (
            allowed_primaries is not None
            and primary not in allowed_primaries
        ):
            errors.append("proposal_falsifier_primary_not_permitted")
        elif falsifier["state_learning_primary"].strip() != primary:
            errors.append(
                "proposal_falsifier_state_learning_primary_mismatch:"
                f"expected={primary}:actual="
                f"{falsifier['state_learning_primary'].strip()}"
            )
        elif falsifier["intervention_target"].strip() != (
            STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
        ):
            errors.append(
                "proposal_falsifier_intervention_target_mismatch:"
                "expected="
                f"{STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]}:"
                f"actual={falsifier['intervention_target'].strip()}"
            )
        else:
            normalized_target_probe = dict(data)
            normalized_target_probe["mechanism_target"] = (
                mechanism_target.strip()
                if isinstance(mechanism_target, str)
                else mechanism_target
            )
            normalized_falsifier_probe = {
                key: value.strip()
                for key, value in falsifier.items()
                if isinstance(value, str)
            }
            errors.extend(_proposal_mechanism_target_errors(
                normalized_target_probe,
                normalized_falsifier_probe,
            ))

    raw_refs = data.get("evidence_refs")
    if isinstance(raw_refs, dict):
        raw_refs = list(raw_refs.values())
    referenced: set[str] = set()
    normalized_refs: set[str] = set()
    snapshot_ref_count = 0
    if not isinstance(raw_refs, list) or not 1 <= len(raw_refs) <= 10:
        errors.append("proposal_evidence_refs_shape_invalid")
    else:
        for value in raw_refs:
            text = str(value or "").strip()
            normalized_ref = None
            matched_source = False
            for prefix in ("source:", "call_index:", "code:", "ref:"):
                if text.lower().startswith(prefix):
                    matched_source = True
                    remainder = text[len(prefix):].strip()
                    for separator in (" [", " —", " -", " (", "\t"):
                        index = remainder.find(separator)
                        if index > 0:
                            remainder = remainder[:index].strip()
                    symbol = _normalize_source_symbol(remainder)
                    if (
                        symbol is not None
                        and symbol not in source_symbols
                        and source_graph is not None
                    ):
                        resolved = _fuzzy_resolve_symbol(
                            symbol,
                            source_graph,
                            emit_event=False,
                        )
                        if resolved is not None and resolved in source_symbols:
                            symbol = resolved
                    if symbol is not None:
                        if symbol in source_symbols:
                            referenced.add(symbol)
                            normalized_ref = f"source:{symbol}"
                    break
            if not matched_source and text.startswith("snapshot:"):
                binding = _snapshot_reference_evidence_binding(
                    text,
                    snapshot_dir,
                )
                normalized_ref = (
                    binding.get("reference")
                    if isinstance(binding, dict)
                    else None
                )
                if normalized_ref is not None:
                    snapshot_ref_count += 1
            if normalized_ref is None or normalized_ref in normalized_refs:
                errors.append("proposal_evidence_ref_invalid")
            else:
                normalized_refs.add(normalized_ref)
        if source_symbols and referenced != set(source_symbols):
            errors.append("proposal_evidence_refs_incomplete")
        if require_snapshot_evidence and snapshot_ref_count < 1:
            errors.append("proposal_snapshot_evidence_required")
        if snapshot_ref_count > 2:
            errors.append("proposal_snapshot_evidence_too_many")
    if len(str(data.get("risks") or "").strip()) < 20:
        errors.append("proposal_risks_invalid")
    budget_probe = _validated_master_proposal(
        output,
        "projection",
        source_graph=source_graph,
        snapshot_dir=snapshot_dir,
        national_policy_only=national_policy_only,
        require_snapshot_evidence=require_snapshot_evidence,
        execution_mode=(
            "fixed_blueprint_capability_audit"
            if evidence_mode == "fresh_strict_control_no_strength"
            else "strategy_implementation"
        ),
        evidence_mode=evidence_mode,
        enforce_bindability=False,
        allowed_primaries=allowed_primaries,
    )
    if isinstance(budget_probe, dict):
        bindability_error = _proposal_worker_bindability_error(budget_probe)
        if bindability_error:
            errors.append(bindability_error)
    return list(dict.fromkeys(errors))


def _validated_proposal_critique(output: str, proposal_ids: set[str]) -> dict | None:
    from llm_query import parse_json_output_with_mode

    data, _mode = parse_json_output_with_mode(output or "")
    if not isinstance(data, dict) or set(data) != {"ballots"}:
        return None
    raw_ballots = data.get("ballots")
    if not isinstance(raw_ballots, list) or len(raw_ballots) != len(proposal_ids):
        return None
    ballots = []
    seen: set[str] = set()
    for raw_ballot in raw_ballots:
        if not isinstance(raw_ballot, dict) or set(raw_ballot) != {
            "proposal_id",
            "scores",
            "reject",
            "reason",
        }:
            return None
        proposal_id = raw_ballot.get("proposal_id")
        scores = raw_ballot.get("scores")
        reason_value = raw_ballot.get("reason")
        reason = reason_value.strip() if isinstance(reason_value, str) else ""
        reject = raw_ballot.get("reject")
        if (
            not isinstance(proposal_id, str)
            or proposal_id not in proposal_ids
            or proposal_id in seen
            or not isinstance(scores, dict)
            or set(scores) != set(_PROPOSAL_CRITIC_CRITERIA)
            or not isinstance(reject, bool)
            or len(reason) < 12
        ):
            return None
        normalized_scores = {}
        for criterion in _PROPOSAL_CRITIC_CRITERIA:
            score = scores.get(criterion)
            if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
                return None
            normalized_scores[criterion] = score
        seen.add(proposal_id)
        ballots.append({
            "proposal_id": proposal_id,
            "scores": normalized_scores,
            "total_score": sum(normalized_scores.values()),
            "reject": reject,
            "reason": reason[:1000],
        })
    if seen != proposal_ids:
        return None
    ranking = [
        item["proposal_id"]
        for item in sorted(
            ballots,
            key=lambda item: (
                item["reject"],
                -item["total_score"],
                item["proposal_id"],
            ),
        )
    ]
    return {
        "ranking": ranking,
        "reject": [item["proposal_id"] for item in ballots if item["reject"]],
        "ballots": ballots,
    }


def _proposal_packet_error(
    reason: str,
    *,
    context_digest: str = "",
    source_code_digest: str = "",
) -> str:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._proposal_packet_error(reason, context_digest=context_digest, source_code_digest=source_code_digest)


def _parse_valid_proposal_packet_impl(
    packet_text: str,
) -> tuple[dict | None, list[str]]:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._parse_valid_proposal_packet_impl(packet_text)


def _parse_valid_proposal_packet(packet_text: str) -> tuple[dict | None, list[str]]:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._parse_valid_proposal_packet(packet_text)


def _proposal_binding_error(code: str, payload: dict) -> str:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._proposal_binding_error(code, payload)


def _provider_prompt_reserved_markers(prompt: str) -> tuple[str, ...]:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._provider_prompt_reserved_markers(prompt)


def _canonical_provider_worker_prompt(prompt: str) -> str:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._canonical_provider_worker_prompt(prompt)


def _task_proposal_scope_paths(task: dict) -> tuple[set[str], tuple[dict, ...]]:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._task_proposal_scope_paths(task)


def _resolve_allowed_selected_proposal(
    data: dict,
    packet: dict,
) -> tuple[dict | None, list[str]]:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._resolve_allowed_selected_proposal(data, packet)


def _canonicalize_selected_proposal_metadata(
    data: dict,
    packet: dict,
) -> tuple[dict, dict | None, list[str], tuple[str, ...]]:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._canonicalize_selected_proposal_metadata(data, packet)


def _validate_final_proposal_binding(data: dict, packet: dict) -> list[str]:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._validate_final_proposal_binding(data, packet)


def _selected_proposal_contract(proposal: dict) -> dict:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._selected_proposal_contract(proposal)


def _selected_proposal_binding(proposal: dict, packet: dict) -> dict:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._selected_proposal_binding(proposal, packet)


def _selected_proposal_worker_block(proposal: dict) -> str:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._selected_proposal_worker_block(proposal)


def _selected_proposal_compilation_contract(proposal: dict) -> dict:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._selected_proposal_compilation_contract(proposal)


def _proposal_worker_bindability_error(proposal: dict) -> str | None:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._proposal_worker_bindability_error(proposal)


def _proposal_compilation_contract_text(packet: dict) -> str:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._proposal_compilation_contract_text(packet)


def _master_final_emission_guard(packet: dict) -> str:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._master_final_emission_guard(packet)


def _bind_selected_proposal_workers(data: dict, proposal: dict) -> dict:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._bind_selected_proposal_workers(data, proposal)


def _project_strict_final_master_result(
    output: str,
    *,
    proposal_packet: dict | None,
    architecture_policy: dict | None,
) -> tuple[dict | None, list[str]]:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._project_strict_final_master_result(output, proposal_packet=proposal_packet, architecture_policy=architecture_policy)


def _record_master_invocation_evidence(
    result: dict,
    *,
    output: str,
    role_result: dict,
) -> dict:
    """Delegate to agent_master_proposal_packet companion."""
    return _packet._record_master_invocation_evidence(result, output=output, role_result=role_result)
