"""Master Architect agent: plans worker tasks from frozen generation evidence."""

import ast
import hashlib
import json
import time
from pathlib import Path

from bot_namespace import bot_name, bot_relpath
from evolution_infra import (
    run_claude_query, substitute_template,
    get_logs_dir, _trim_to_budget, PROMPTS_DIR,
    MAX_MASTER_RETRIES,
    get_bot_dir, MAX_LINES_HARD_CAP,
)

from output_schema import master_plan_executable_contract_text
from llm_availability import LLMAvailabilityBlocked, gather_llm_fail_fast


def _render_master_proposal_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "planning_context", "direction", "directive", "source_v", "next_v",
        "protocol_bootstrap_prepared_only", "source_symbol_index",
        "repair_kind", "invocation_id",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Master proposal renderer input contract mismatch")
    planning_context = str(inputs["planning_context"])
    direction = str(inputs["direction"])
    directive = str(inputs["directive"])
    source_v = int(inputs["source_v"])
    next_v = int(inputs["next_v"])
    bootstrap = bool(inputs["protocol_bootstrap_prepared_only"])
    repair_kind = str(inputs["repair_kind"] or "")
    is_repair = bool(repair_kind)
    is_distinctness_repair = repair_kind == "distinctness"
    output_contract = (
        "Return one JSON object with exactly: targeted_failure, structural_change, "
        "counterfactual, measurement, why_not_threshold_tuning, target_files "
        "(exactly [\"policy.py\"]), expected_diff, source_symbols (1-8 exact "
        "source-relative file.py:symbol references), reachable_chain (2-8 of those "
        "symbols in direct caller-to-callee order), falsifier {test_name, control, "
        "intervention, expected_observation}, evidence_refs (source:file.py:symbol "
        "for EVERY source_symbols item; optional "
        "snapshot:relative/file.json#/verified/json/pointer), "
        "and risks. Every chain edge must be a direct syntactic call in the baseline. "
        "Do not invent a symbol or snapshot file. Do not emit tasks, a worker plan, "
        "source choice, proposal_id, Markdown, or commentary."
        " This is national_tcp_policy_v1. policy.py is the only candidate-owned "
        "writable source file; national_bot.py and precompute.py are system-owned "
        "read-only files, and candidate helper modules/assets are forbidden. Propose "
        "a causally distinct policy mechanism over decision_context that returns only "
        "typed pass/fold/allin/raise intents (raise uses raise_to). "
        "IMPORTANT: falsifier.test_name MUST be exactly one of: fast_policy_baseline, "
        "incremental_refinement_protocol, incremental_opponent_model, "
        "terminal_response_adaptation, showdown_range_adaptation, "
        "donk_line_reachability, delayed_probe_line_reachability. "
        "Choose the one that best matches your proposed mechanism."
    )
    code_scope = (
        f"Read only the prepared target code at {bot_relpath(next_v)}/ and "
        "typed references. The historical lineage source code is quarantined "
        "and is not an admissible planning input.\n\n"
        if bootstrap else
        "Read only the allowed frozen snapshot and source/target code.\n\n"
    )
    lineage_scope = (
        f"Historical completion high-water v{source_v} is numeric identity only, "
        f"not a source or parent. The prepared v{next_v} target is the sole "
        "system-owned planning baseline; never open, infer, or inherit "
        f"high-water v{source_v}."
        if bootstrap else
        f"The system-owned source is fixed at v{source_v} and target at "
        f"v{next_v}; never rerank, branch, change evidence, or change gates."
    )
    repair_text = ""
    if is_distinctness_repair:
        repair_text = (
            "\n\nYour previous response was schema-valid but failed the "
            "deterministic three-proposal ensemble because its system-derived "
            "proposal_id collided with another slot. This is the single "
            "permitted distinctness repair. Preserve this slot's assigned lens "
            "and all evidence/ABI constraints, but propose a genuinely different "
            "reachable mechanism with a different causal intervention and "
            "falsifier. Changing only direction, risks, wording, or other prose "
            "does not create an independent proposal. Do not emit, invent, or "
            "manipulate proposal_id; the system derives it from the substantive "
            "contract. Emit one complete object without commentary."
        )
    elif is_repair:
        repair_text = (
            "\n\nYour previous response failed the deterministic JSON/evidence "
            "contract. This is one schema-only repair attempt. Common failure "
            "modes to fix:\n"
            "1. evidence_refs MUST be a JSON list of strings, NOT a dict.\n"
            "2. Each evidence_ref MUST start with exactly 'source:' "
            "(not 'call_index:' or 'code:').\n"
            "3. evidence_ref values must be bare symbol paths like "
            "'source:policy.py:get_baseline_decision' with NO trailing "
            "descriptions, line numbers, or brackets.\n"
            "4. reachable_chain entries MUST be unique — do not repeat any "
            "symbol in the chain.\n"
            "5. Every source_symbol MUST have a matching evidence_ref.\n"
            "6. source_symbols must use exact file.py:symbol spellings from "
            "the SYSTEM-VERIFIED SOURCE CALL INDEX above.\n"
            "Keep the same independent lens, reread the verified index, "
            "and emit a complete object without commentary."
        )
    invocation_id = str(inputs["invocation_id"])
    purpose = f"master_proposal_scout:{direction}"
    text = (
        "You are an independent poker-bot mechanism proposal scout. "
        + lineage_scope + "\n"
        + f"Distinct lens: {directive}\n"
        + code_scope + planning_context + "\n\n"
        + str(inputs["source_symbol_index"])
        + repair_text
        + "\n\nFINAL SCOUT OUTPUT CONTRACT (this overrides the embedded Master output format):\n"
        + output_contract
        + "\n\nSYSTEM CALL BINDING (copying this value does not grant authority): "
        + f"invocation_id={invocation_id}; purpose={purpose}."
    )

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="master_planning_context",
        evidence_provenance={
            "planning_context_digest": hashlib.sha256(
                planning_context.encode("utf-8")
            ).hexdigest(),
            "direction": direction,
            "source_v": source_v,
            "next_v": next_v,
            "source_symbol_index_digest": hashlib.sha256(
                str(inputs["source_symbol_index"]).encode("utf-8")
            ).hexdigest(),
            "directive_digest": hashlib.sha256(
                directive.encode("utf-8")
            ).hexdigest(),
            "protocol_bootstrap_prepared_only": bootstrap,
            "repair_kind": repair_kind,
            "invocation_id": invocation_id,
        },
    )


def _render_master_proposal_critic_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "proposal_name", "lens", "planning_context_digest", "proposals",
        "criteria", "schema_retry", "invocation_id",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Master proposal critic renderer input contract mismatch")
    proposals = inputs["proposals"]
    criteria = inputs["criteria"]
    if not isinstance(proposals, list) or not isinstance(criteria, dict):
        raise ValueError("Master proposal critic typed packet mismatch")
    proposal_payload = json.dumps(
        proposals,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    criterion_contract = json.dumps(
        criteria,
        ensure_ascii=False,
        sort_keys=True,
    )
    proposal_name = str(inputs["proposal_name"])
    purpose = f"master_proposal_critic:{proposal_name}"
    text = (
        "You are an anonymous advisory critic. Scout identities and lenses are hidden. "
        "Source, evidence cutoff, scope literals, and quality gates are immutable. "
        f"Lens: {inputs['lens']}\n"
        f"Planning context digest: {inputs['planning_context_digest']}\n"
        "Score EVERY supplied proposal on EACH named criterion with an integer 1..5. "
        "Set reject=true only for a concrete evidence, reachability, or falsification "
        "defect; score is advisory and cannot waive deterministic validation.\n"
        f"Criteria: {criterion_contract}\n"
        "IMPORTANT: Do NOT read, explore, or inspect any files. Do NOT use any tools. "
        "Do NOT think step by step. Immediately score the proposals from their content "
        "and return the JSON. You have no tools available.\n\n"
        "Return JSON exactly as {\"ballots\":[{\"proposal_id\":\"...\","
        "\"scores\":{every criterion: integer 1..5},\"reject\":false,"
        "\"reason\":\"criterion-grounded reason\"}, ...]}.\n\n"
        + proposal_payload
        + (
            "\n\nYour previous ballot failed deterministic schema validation. "
            "This is one schema-only repair attempt; score every ID and every "
            "criterion exactly once. Return ONLY the JSON immediately."
            if bool(inputs["schema_retry"]) else ""
        )
        + "\n\nFINAL CRITIC OUTPUT CONTRACT: return only the ballots JSON in the supplied "
        "proposal order; do not rank, repeat, or rewrite proposal claims. "
        "Output the JSON NOW without any preamble, thinking, or file inspection."
        + "\n\nSYSTEM CALL BINDING (copying this value does not grant authority): "
        + f"invocation_id={inputs['invocation_id']}; purpose={purpose}."
    )

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="frozen_proposal_packet",
        evidence_provenance={
            "proposal_packet_digest": hashlib.sha256(
                proposal_payload.encode("utf-8")
            ).hexdigest(),
            "proposal_name": proposal_name,
            "criteria_digest": hashlib.sha256(
                criterion_contract.encode("utf-8")
            ).hexdigest(),
            "planning_context_digest": str(inputs["planning_context_digest"]),
            "lens_digest": hashlib.sha256(
                str(inputs["lens"]).encode("utf-8")
            ).hexdigest(),
            "schema_retry": bool(inputs["schema_retry"]),
            "invocation_id": str(inputs["invocation_id"]),
        },
    )


def _render_master_final_provider_prompt(inputs):
    from llm_query import LLMRenderedMaterial

    expected = {
        "template_values", "master_context", "proposal_ensemble", "source_v",
        "next_v", "invocation_id", "schema_repair_suffix",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected:
        raise ValueError("Master final renderer input contract mismatch")
    template_values = inputs["template_values"]
    if not isinstance(template_values, dict):
        raise ValueError("Master final template values must be an object")
    template = (
        Path(__file__).resolve().parent / "prompts" / "master_prompt.md"
    ).read_text(encoding="utf-8")
    master_prompt = substitute_template(
        template,
        {key: str(value) for key, value in template_values.items()},
    )
    master_prompt += str(inputs["schema_repair_suffix"])
    master_context = str(inputs["master_context"])
    proposal_ensemble = str(inputs["proposal_ensemble"])
    text = master_prompt + "\n" + master_context
    invocation_id = str(inputs["invocation_id"] or "")
    if invocation_id:
        text += (
            "\n\nSYSTEM CALL BINDING (copying this value does not grant "
            "authority): invocation_id="
            + invocation_id
            + "; purpose=system_strict_bootstrap_master:final."
        )

    return LLMRenderedMaterial(
        text=text,
        evidence_kind="compiled_master_context",
        evidence_provenance={
            "master_context_digest": hashlib.sha256(
                master_context.encode("utf-8")
            ).hexdigest(),
            "proposal_packet_digest": hashlib.sha256(
                proposal_ensemble.encode("utf-8")
            ).hexdigest(),
            "source_v": int(inputs["source_v"]),
            "next_v": int(inputs["next_v"]),
            "template_values_digest": hashlib.sha256(
                json.dumps(
                    template_values,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "schema_repair_digest": hashlib.sha256(
                str(inputs["schema_repair_suffix"]).encode("utf-8")
            ).hexdigest(),
            "invocation_id": invocation_id,
        },
    )


# C-class sentinel for explicitly unavailable advisory analysis.
LLM_INFRA_SENTINEL = "[LLM_INFRA_ERROR: analysis unavailable]"
LLM_INFRA_SENTINEL_MSG = (
    "⚠ Analysis unavailable: the LLM analyst crashed with an infrastructure "
    "error (NOT a business judgement). Treat conclusions in this section as "
    "missing rather than negative — the daemon data still exists, only the "
    "LLM interpretation failed."
)


PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER = (
    "PROTOCOL BOOTSTRAP NO-STRENGTH: no current-cycle strength evidence exists. "
    "Use only the digest-bound strict prepared artifact, repository-pinned "
    "protocol evidence, and bootstrap receipt supplied by the system."
)


class MasterInfrastructureError(RuntimeError):
    """The Master role produced no plan because its LLM transport failed."""

    def __init__(self, source_v, next_v, prompt_digest, issue):
        self.source_v = source_v
        self.next_v = next_v
        self.prompt_digest = prompt_digest
        self.issue = str(issue)[:500]
        super().__init__(self.issue)


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


_PROPOSAL_SCHEMA_VERSION = "master-proposal-v2"
_PROPOSAL_PACKET_SCHEMA_VERSION = "master-proposal-packet-v2"
_PROPOSAL_CRITIC_CRITERIA = {
    "evidence_traceability": (
        "Every claimed source fact is bound to a verified source symbol or frozen "
        "snapshot locator."
    ),
    "runtime_reachability": (
        "The verified parent call chain reaches a file that the proposal will edit."
    ),
    "falsifiability": (
        "The control/intervention/expected observation can disprove the mechanism."
    ),
    "causal_attribution": (
        "The measurement distinguishes the mechanism from unrelated threshold drift."
    ),
    "bounded_regression_risk": (
        "The implementation scope and fallback make regressions observable and bounded."
    ),
}

_PROPOSAL_SUBSTANTIVE_FIELDS = (
    "schema_version",
    "targeted_failure",
    "structural_change",
    "counterfactual",
    "measurement",
    "why_not_threshold_tuning",
    "expected_diff",
    "target_files",
    "source_symbols",
    "reachable_chain",
    "falsifier",
    "evidence_refs",
)


def _safe_relative_python_path(value: object) -> str | None:
    """Return one normalized source-relative Python path, never an escape."""
    raw = str(value or "").strip().replace("\\", "/")
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix != ".py"
    ):
        return None
    return path.as_posix()


def _source_symbol_graph(source_dir: Path) -> tuple[dict[str, set[str]], str]:
    """Index real top-level functions/methods and their direct call leaves.

    The graph deliberately proves only a small, deterministic claim: every
    symbol exists in the frozen baseline and every adjacent item in a submitted
    reachability chain is a direct syntactic call.  It does not ask an LLM to
    judge whether prose merely *sounds* reachable.
    """
    graph: dict[str, set[str]] = {}
    digest = hashlib.sha256()
    source_dir = Path(source_dir).resolve()
    for path in sorted(source_dir.rglob("*.py")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            relative = path.resolve().relative_to(source_dir).as_posix()
            payload = path.read_bytes()
        except (OSError, ValueError):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        try:
            tree = ast.parse(payload, filename=relative)
        except SyntaxError:
            # Syntax-invalid files cannot supply evidence, but they still bind
            # the source artifact digest and therefore cannot drift invisibly.
            continue

        def calls(node: ast.AST) -> set[str]:
            result: set[str] = set()
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                target = child.func
                if isinstance(target, ast.Name):
                    result.add(target.id)
                elif isinstance(target, ast.Attribute):
                    result.add(target.attr)
            return result

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                graph[f"{relative}:{node.name}"] = calls(node)
            elif isinstance(node, ast.ClassDef):
                graph[f"{relative}:{node.name}"] = calls(node)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        graph[f"{relative}:{node.name}.{child.name}"] = calls(child)
    return graph, digest.hexdigest()


def _source_symbol_prompt_index(
    graph: dict[str, set[str]],
    *,
    maximum_chars: int = 18_000,
) -> str:
    """Render deterministic, validator-matching call evidence for weak scouts.

    Asking a weaker model to rediscover exact ``file.py:symbol`` spellings and
    direct call leaves wastes both calls and context.  The system has already
    parsed the frozen source, so expose the accepted edge vocabulary directly.
    Lines are kept whole under a hard bound; omitted tails remain available via
    the read-only source tool but cannot be invented in a proposal.
    """
    symbols_by_leaf: dict[str, list[str]] = {}
    for symbol in sorted(graph):
        leaf = symbol.rsplit(":", 1)[1].rsplit(".", 1)[-1]
        symbols_by_leaf.setdefault(leaf, []).append(symbol)
    lines = [
        "SYSTEM-VERIFIED SOURCE CALL INDEX (exact proposal spellings; each arrow "
        "is a validator-accepted direct syntactic call leaf):"
    ]
    for caller in sorted(graph):
        callees = sorted({
            candidates[0]
            for leaf in graph[caller]
            if len(candidates := symbols_by_leaf.get(leaf, [])) == 1
            and candidates[0] != caller
        })
        if not callees:
            continue
        line = f"- {caller} -> {', '.join(callees)}"
        if sum(len(item) + 1 for item in lines) + len(line) + 1 > maximum_chars:
            lines.append("- [remaining verified edges omitted by deterministic size bound]")
            break
        lines.append(line)
    if len(lines) == 1:
        lines.append("- [no validator-accepted internal call edges]")
    return "\n".join(lines)


def _normalize_source_symbol(value: object) -> str | None:
    text = str(value or "").strip()
    if ":" not in text:
        return None
    filename, symbol = text.rsplit(":", 1)
    filename = _safe_relative_python_path(filename)
    if filename is None:
        return None
    symbol_parts = symbol.split(".")
    if not symbol_parts or any(not part.isidentifier() for part in symbol_parts):
        return None
    return f"{filename}:{symbol}"


def _fuzzy_resolve_symbol(symbol: str, source_graph: dict) -> str | None:
    """Resolve a source symbol that may have a minor naming error.

    The system injects the exact call index into every scout prompt, but weak
    models still misspell function names (e.g. ``_hole_ids`` instead of
    ``_card_ids``).  This resolver corrects unambiguous within-file mismatches
    without weakening the existence guarantee: the resolved symbol must still
    be a real entry in the frozen source graph.
    """
    if symbol in source_graph:
        return symbol
    file_part, _, leaf = symbol.rpartition(":")
    if not file_part or not leaf:
        return None
    candidates = []
    for key in source_graph:
        key_file, _, key_leaf = key.rpartition(":")
        if key_file == file_part:
            candidates.append((key, key_leaf.rsplit(".", 1)[-1]))
    if not candidates:
        return None
    bare_leaf = leaf.rsplit(".", 1)[-1]
    exact = [k for k, cl in candidates if cl == bare_leaf]
    if len(exact) == 1:
        return exact[0]
    import difflib
    close = difflib.get_close_matches(
        bare_leaf, [cl for _, cl in candidates], n=1, cutoff=0.5)
    if not close:
        return None
    matches = [k for k, cl in candidates if cl == close[0]]
    if len(matches) == 1:
        from system_log import log_system_event
        log_system_event("proposal.fuzzy_symbol_resolution", "info",
            f"fuzzy resolved {symbol} to {matches[0]}",
            {"claimed": symbol, "resolved": matches[0]})
        return matches[0]
    return None


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


def _validated_master_proposal(
    output: str,
    direction: str,
    *,
    source_graph: dict[str, set[str]] | None = None,
    snapshot_dir: Path | None = None,
    national_policy_only: bool = False,
) -> dict | None:
    """Normalize one evidence-bound proposal before critics or Master see it."""
    from llm_query import parse_json_output_with_mode

    data, _mode = parse_json_output_with_mode(output or "")
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
    for key in required:
        value = str(data.get(key) or "").strip()
        if len(value) < 20:
            return None
        normalized[key] = value[:1600]
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
    if source_graph is not None:
        symbols_by_leaf: dict[str, list[str]] = {}
        for source_symbol in source_graph:
            source_leaf = source_symbol.rsplit(":", 1)[1].rsplit(".", 1)[-1]
            symbols_by_leaf.setdefault(source_leaf, []).append(source_symbol)
        for caller, callee in zip(chain, chain[1:]):
            callee_leaf = callee.rsplit(":", 1)[1].rsplit(".", 1)[-1]
            resolved = symbols_by_leaf.get(callee_leaf, [])
            if (
                callee_leaf not in source_graph.get(caller, set())
                or len(resolved) != 1
                or resolved[0] != callee
            ):
                return None
    chain_files = {item.rsplit(":", 1)[0] for item in chain}
    if not chain_files.intersection(target_files):
        return None
    normalized["reachable_chain"] = chain

    falsifier = data.get("falsifier")
    if not isinstance(falsifier, dict):
        return None
    normalized_falsifier = {}
    for key in ("test_name", "control", "intervention", "expected_observation"):
        value = str(falsifier.get(key) or "").strip()
        minimum = 3 if key == "test_name" else 20
        if len(value) < minimum:
            return None
        if key == "test_name" and not value.replace("_", "").isalnum():
            return None
        normalized_falsifier[key] = value[:1000]
    normalized["falsifier"] = normalized_falsifier
    raw_refs = data.get("evidence_refs")
    # Weak models sometimes produce evidence_refs as a dict instead of a list.
    # Coerce dict values to a list so we don't reject otherwise valid proposals.
    if isinstance(raw_refs, dict):
        raw_refs = list(raw_refs.values())
    if not isinstance(raw_refs, list) or not 1 <= len(raw_refs) <= 10:
        return None
    evidence_refs: list[str] = []
    source_ref_symbols: set[str] = set()
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
            normalized_ref = _validated_snapshot_reference(text, snapshot_dir)
        if normalized_ref is None or normalized_ref in evidence_refs:
            return None
        evidence_refs.append(normalized_ref)
    if source_ref_symbols != set(source_symbols):
        return None
    normalized["evidence_refs"] = evidence_refs

    risks = str(data.get("risks") or "").strip()
    if len(risks) < 20:
        return None
    normalized["risks"] = risks[:1200]

    # Identity is a pure function of the proposal claims and verified evidence,
    # not scout identity, critic order, generation number, or wall clock.
    normalized["proposal_id"] = _proposal_identity(normalized)
    return normalized


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
    return json.dumps({
        "schema_version": _PROPOSAL_PACKET_SCHEMA_VERSION,
        "valid": False,
        "reason": str(reason)[:500],
        "context_digest": context_digest,
        "source_code_digest": source_code_digest,
        "proposal_count": 0,
        "valid_critic_count": 0,
        "allowed_proposal_ids": [],
        "ordered_proposals": [],
        "proposal_invocations": {},
        "critic_reviews": [],
    }, ensure_ascii=False, sort_keys=True)


def _parse_valid_proposal_packet(packet_text: str) -> tuple[dict | None, list[str]]:
    """Validate the machine packet again at the final-Master trust boundary."""
    try:
        packet = json.loads(packet_text)
    except (TypeError, json.JSONDecodeError):
        return None, ["proposal_packet_not_json"]
    if not isinstance(packet, dict):
        return None, ["proposal_packet_not_object"]
    errors = []
    if packet.get("schema_version") != _PROPOSAL_PACKET_SCHEMA_VERSION:
        errors.append("proposal_packet_schema_mismatch")
    if packet.get("valid") is not True:
        errors.append(f"proposal_packet_invalid:{packet.get('reason', 'unknown')}")
    expected_packet_fields = {
        "schema_version",
        "valid",
        "authority",
        "context_digest",
        "source_code_digest",
        "critic_criteria",
        "proposal_count",
        "valid_critic_count",
        "allowed_proposal_ids",
        "ordered_proposals",
        "proposal_invocations",
        "critic_reviews",
    }
    if set(packet) != expected_packet_fields:
        errors.append("proposal_packet_fields_mismatch")
    proposals = packet.get("ordered_proposals")
    allowed = packet.get("allowed_proposal_ids")
    if not isinstance(proposals, list) or len(proposals) != 3:
        errors.append("proposal_packet_requires_exactly_three_proposals")
        proposals = []
    if packet.get("proposal_count") != 3:
        errors.append("proposal_packet_count_must_be_three")
    proposal_ids = [
        str(item.get("proposal_id") or "")
        for item in proposals
        if isinstance(item, dict)
    ]
    if (
        len(proposal_ids) != len(proposals)
        or len(set(proposal_ids)) != len(proposal_ids)
        or not isinstance(allowed, list)
        or set(map(str, allowed)) != set(proposal_ids)
    ):
        errors.append("proposal_packet_id_set_mismatch")
    required_proposal_fields = {
        "schema_version",
        "direction",
        "proposal_id",
        "targeted_failure",
        "structural_change",
        "counterfactual",
        "measurement",
        "why_not_threshold_tuning",
        "expected_diff",
        "target_files",
        "source_symbols",
        "reachable_chain",
        "falsifier",
        "evidence_refs",
        "risks",
    }
    for item in proposals:
        if not isinstance(item, dict):
            continue
        if set(item) != required_proposal_fields:
            errors.append(f"proposal_packet_fields_missing:{item.get('proposal_id', '')}")
            continue
        if item.get("schema_version") != _PROPOSAL_SCHEMA_VERSION:
            errors.append(f"proposal_schema_mismatch:{item.get('proposal_id', '')}")
        if item.get("proposal_id") != _proposal_identity(item):
            errors.append(f"proposal_identity_mismatch:{item.get('proposal_id', '')}")
    proposal_invocations = packet.get("proposal_invocations")
    if (
        not isinstance(proposal_invocations, dict)
        or set(proposal_invocations) != set(proposal_ids)
    ):
        errors.append("proposal_invocation_set_mismatch")
        proposal_invocations = {}
    invocation_ids: list[str] = []
    try:
        from bot_artifact import canonical_digest
        from system_strict_bootstrap import validate_llm_invocation_evidence

        for proposal in proposals:
            if not isinstance(proposal, dict):
                continue
            proposal_id = str(proposal.get("proposal_id") or "")
            evidence = proposal_invocations.get(proposal_id)
            direction = str(proposal.get("direction") or "")
            evidence_errors = validate_llm_invocation_evidence(
                evidence,
                expected_purpose=f"master_proposal_scout:{direction}",
            )
            errors.extend(
                f"proposal_invocation_invalid:{proposal_id}:{item}"
                for item in evidence_errors
            )
            if isinstance(evidence, dict):
                invocation_ids.append(str(evidence.get("invocation_id") or ""))
                role = str(evidence.get("role") or "")
                if not role.startswith(f"MASTER PROPOSAL {direction}"):
                    errors.append(
                        f"proposal_invocation_role_mismatch:{proposal_id}"
                    )
                if evidence.get("role_result_digest") != canonical_digest(proposal):
                    errors.append(
                        f"proposal_invocation_result_mismatch:{proposal_id}"
                    )
    except Exception as exc:
        errors.append(
            f"proposal_invocation_validation_error:{type(exc).__name__}"
        )
    if packet.get("valid_critic_count") != 2:
        errors.append("proposal_packet_requires_two_valid_critics")
    reviews = packet.get("critic_reviews")
    expected_critic_ids = {"falsification", "scope"}
    if not isinstance(reviews, list) or len(reviews) != 2:
        errors.append("proposal_packet_requires_two_critic_reviews")
        reviews = []
    critic_ids = {
        str(review.get("critic_id") or "")
        for review in reviews
        if isinstance(review, dict)
    }
    if critic_ids != expected_critic_ids:
        errors.append("proposal_critic_identity_set_mismatch")
    try:
        from bot_artifact import canonical_digest
        from system_strict_bootstrap import validate_llm_invocation_evidence

        for review in reviews:
            if not isinstance(review, dict):
                errors.append("proposal_critic_review_not_object")
                continue
            critic_id = str(review.get("critic_id") or "")
            if set(review) != {
                "critic_id",
                "ranking",
                "reject",
                "ballots",
                "invocation_evidence",
            }:
                errors.append(f"proposal_critic_review_fields_mismatch:{critic_id}")
            ballots = review.get("ballots")
            if not isinstance(ballots, list) or len(ballots) != len(proposal_ids):
                errors.append(f"proposal_critic_ballot_count_mismatch:{critic_id}")
                ballots = []
            seen_ballots: set[str] = set()
            normalized_ballots = []
            for ballot in ballots:
                if not isinstance(ballot, dict) or set(ballot) != {
                    "proposal_id",
                    "scores",
                    "total_score",
                    "reject",
                    "reason",
                }:
                    errors.append(
                        f"proposal_critic_ballot_fields_mismatch:{critic_id}"
                    )
                    continue
                proposal_id = ballot.get("proposal_id")
                scores = ballot.get("scores")
                reject = ballot.get("reject")
                reason = ballot.get("reason")
                if (
                    not isinstance(proposal_id, str)
                    or proposal_id not in set(proposal_ids)
                    or proposal_id in seen_ballots
                    or not isinstance(scores, dict)
                    or set(scores) != set(_PROPOSAL_CRITIC_CRITERIA)
                    or not isinstance(reject, bool)
                    or not isinstance(reason, str)
                    or len(reason.strip()) < 12
                ):
                    errors.append(
                        f"proposal_critic_ballot_schema_invalid:{critic_id}"
                    )
                    continue
                if any(
                    isinstance(score, bool)
                    or not isinstance(score, int)
                    or not 1 <= score <= 5
                    for score in scores.values()
                ):
                    errors.append(
                        f"proposal_critic_ballot_score_invalid:{critic_id}"
                    )
                    continue
                total = sum(scores.values())
                if ballot.get("total_score") != total:
                    errors.append(
                        f"proposal_critic_ballot_total_mismatch:{critic_id}"
                    )
                seen_ballots.add(proposal_id)
                normalized_ballots.append(ballot)
            if seen_ballots != set(proposal_ids):
                errors.append(f"proposal_critic_ballot_set_mismatch:{critic_id}")
            expected_ranking = [
                item["proposal_id"]
                for item in sorted(
                    normalized_ballots,
                    key=lambda item: (
                        item["reject"],
                        -item["total_score"],
                        item["proposal_id"],
                    ),
                )
            ]
            expected_reject = [
                item["proposal_id"]
                for item in normalized_ballots
                if item["reject"]
            ]
            if review.get("ranking") != expected_ranking:
                errors.append(f"proposal_critic_ranking_mismatch:{critic_id}")
            if review.get("reject") != expected_reject:
                errors.append(f"proposal_critic_reject_mismatch:{critic_id}")
            evidence = review.get("invocation_evidence")
            evidence_errors = validate_llm_invocation_evidence(
                evidence,
                expected_purpose=f"master_proposal_critic:{critic_id}",
            )
            errors.extend(
                f"proposal_critic_invocation_invalid:{critic_id}:{item}"
                for item in evidence_errors
            )
            if isinstance(evidence, dict):
                invocation_ids.append(str(evidence.get("invocation_id") or ""))
                role = str(evidence.get("role") or "")
                if not role.startswith(f"MASTER PROPOSAL CRITIC {critic_id}"):
                    errors.append(
                        f"proposal_critic_invocation_role_mismatch:{critic_id}"
                    )
                role_result = {
                    key: value
                    for key, value in review.items()
                    if key not in {"critic_id", "invocation_evidence"}
                }
                if evidence.get("role_result_digest") != canonical_digest(
                    role_result
                ):
                    errors.append(
                        f"proposal_critic_invocation_result_mismatch:{critic_id}"
                    )
    except Exception as exc:
        errors.append(
            f"proposal_critic_invocation_validation_error:{type(exc).__name__}"
        )
    if len(invocation_ids) != 5 or len(set(invocation_ids)) != 5:
        errors.append("proposal_packet_invocations_not_independent")
    if packet.get("critic_criteria") != _PROPOSAL_CRITIC_CRITERIA:
        errors.append("proposal_critic_criteria_mismatch")
    context_digest = str(packet.get("context_digest") or "")
    source_digest = str(packet.get("source_code_digest") or "")
    if (
        len(context_digest) != 64
        or len(source_digest) != 64
        or any(char not in "0123456789abcdef" for char in context_digest + source_digest)
    ):
        errors.append("proposal_packet_digest_invalid")
    return (None, errors) if errors else (packet, [])


def _validate_final_proposal_binding(data: dict, packet: dict) -> list[str]:
    """Require one exact proposal selection and its writable-file contract."""
    if not isinstance(data, dict):
        return ["master_output_not_object"]
    selected = data.get("selected_proposal_id")
    if not isinstance(selected, str):
        return ["selected_proposal_id_must_be_one_string"]
    proposals = {
        item["proposal_id"]: item
        for item in packet.get("ordered_proposals", [])
        if isinstance(item, dict) and isinstance(item.get("proposal_id"), str)
    }
    proposal = proposals.get(selected)
    if proposal is None:
        return [f"selected_proposal_id_not_allowed:{selected}"]
    errors = []
    if str(data.get("targeted_failure") or "").strip() != proposal["targeted_failure"]:
        errors.append("targeted_failure_must_exactly_copy_selected_proposal")
    writable: set[str] = set()
    tasks = data.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            for key in ("target_files", "files_allowed"):
                values = task.get(key) or []
                if isinstance(values, list):
                    writable.update(
                        path
                        for value in values
                        if (path := _safe_relative_python_path(value)) is not None
                    )
    missing_files = sorted(set(proposal["target_files"]) - writable)
    if missing_files:
        errors.append(f"selected_proposal_target_files_not_writable:{missing_files}")
    binding_block = _selected_proposal_worker_block(proposal)
    bound_task_count = 0
    try:
        from output_schema import WORKER_PROMPT_MAX_CHARS
    except Exception:
        WORKER_PROMPT_MAX_CHARS = 16_000
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_files = {
                path
                for key in ("target_files", "files_allowed")
                for value in (task.get(key) or [])
                if (path := _safe_relative_python_path(value)) is not None
            }
            if not task_files.intersection(proposal["target_files"]):
                continue
            bound_task_count += 1
            prompt = str(task.get("worker_prompt") or "")
            if len(prompt) + len(binding_block) + 2 > WORKER_PROMPT_MAX_CHARS:
                errors.append(
                    "selected_proposal_worker_prompt_has_no_binding_budget:"
                    f"{task.get('worker_id', bound_task_count)}"
                )
    if bound_task_count == 0 and not missing_files:
        errors.append("selected_proposal_has_no_bound_worker_task")
    return errors


def _selected_proposal_contract(proposal: dict) -> dict:
    contract = {
        "schema_version": 1,
        "proposal_id": str(proposal["proposal_id"]),
        "structural_change": str(proposal["structural_change"]),
        "expected_diff": str(proposal["expected_diff"]),
        "reachable_chain": list(proposal["reachable_chain"]),
        "falsifier": dict(proposal["falsifier"]),
        "why_not_threshold_tuning": str(proposal["why_not_threshold_tuning"]),
    }
    contract["contract_digest"] = hashlib.sha256(
        json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return contract


def _selected_proposal_worker_block(proposal: dict) -> str:
    from plan_compiler import SELECTED_PROPOSAL_BEGIN, SELECTED_PROPOSAL_END

    contract = _selected_proposal_contract(proposal)
    return "\n".join((
        SELECTED_PROPOSAL_BEGIN,
        "# SYSTEM-BOUND SELECTED PROPOSAL CONTRACT",
        f"proposal_id={contract['proposal_id']}",
        f"contract_digest={contract['contract_digest']}",
        f"structural_change={contract['structural_change']}",
        f"expected_diff={contract['expected_diff']}",
        "reachable_chain=" + json.dumps(
            contract["reachable_chain"], ensure_ascii=False, separators=(",", ":")
        ),
        "falsifier=" + json.dumps(
            contract["falsifier"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "not_threshold_tuning=" + contract["why_not_threshold_tuning"],
        "Implement this one mechanism through the named reachable chain. Do not "
        "substitute a threshold-only edit, a second mechanism, or telemetry-only code.",
        SELECTED_PROPOSAL_END,
    ))


def _bind_selected_proposal_workers(data: dict, proposal: dict) -> dict:
    """Compile the selected mechanism into every writable target task."""
    result = json.loads(json.dumps(data, ensure_ascii=False))
    block = _selected_proposal_worker_block(proposal)
    target_files = set(proposal["target_files"])
    for task in result.get("tasks") or []:
        task_files = {
            path
            for key in ("target_files", "files_allowed")
            for value in (task.get(key) or [])
            if (path := _safe_relative_python_path(value)) is not None
        }
        if task_files.intersection(target_files):
            task["worker_prompt"] = (
                str(task.get("worker_prompt") or "").rstrip()
                + "\n\n"
                + block
            )
    return result


def _project_strict_final_master_result(
    output: str,
    *,
    proposal_packet: dict | None,
    architecture_policy: dict | None,
) -> tuple[dict | None, list[str]]:
    """Deterministically project provider text to the accepted strict plan.

    This is intentionally the same post-provider transformation used by the
    first strict Master path: proposal selection, system-owned policy ABI terms,
    canonical Master schema normalization, and the frozen proposal bindings.
    Strict authority calls this function before completing the provider effect,
    so an unrelated caller-supplied plan can never be accepted as LLM output.
    """

    if not isinstance(proposal_packet, dict):
        return None, ["proposal_packet_missing"]
    packet, packet_errors = _parse_valid_proposal_packet(json.dumps(
        proposal_packet,
        ensure_ascii=False,
        sort_keys=True,
    ))
    if packet_errors or packet is None:
        return None, ["proposal_packet_invalid:" + item for item in packet_errors]
    if not isinstance(architecture_policy, dict) or not architecture_policy:
        return None, ["architecture_policy_missing"]

    from llm_query import parse_json_output_with_mode

    data, failure_mode = parse_json_output_with_mode(output or "")
    if not isinstance(data, dict) or "tasks" not in data:
        return None, [f"master_output_invalid:{failure_mode}"]
    binding_errors = _validate_final_proposal_binding(data, packet)
    if binding_errors:
        return None, binding_errors
    selected_proposal_id = data.pop("selected_proposal_id")
    selected_proposal = next(
        item
        for item in packet["ordered_proposals"]
        if item["proposal_id"] == selected_proposal_id
    )
    data = _bind_selected_proposal_workers(data, selected_proposal)

    from plan_compiler import (
        bind_system_owned_policy_abi,
        bind_system_owned_worker_contract_terms,
    )

    data, _policy_abi = bind_system_owned_policy_abi(
        data,
        policy=architecture_policy,
    )
    data, _terms = bind_system_owned_worker_contract_terms(data)
    if any(data.get(field) for field in (
        "branch_from",
        "source_override",
        "source_v_override",
    )):
        return None, ["master_source_override_forbidden"]

    from output_schema import validate_agent_output

    data, schema_errors = validate_agent_output("master", data)
    if schema_errors:
        return None, ["master_schema:" + item for item in schema_errors]

    selected_contract = _selected_proposal_contract(selected_proposal)
    data["selected_proposal_id"] = selected_proposal_id
    data["proposal_binding"] = {
        "schema_version": _PROPOSAL_PACKET_SCHEMA_VERSION,
        "selected_proposal_id": selected_proposal_id,
        "contract_digest": selected_contract["contract_digest"],
        "context_digest": packet["context_digest"],
        "source_code_digest": packet["source_code_digest"],
        "target_files": list(selected_proposal["target_files"]),
        "source_symbols": list(selected_proposal["source_symbols"]),
        "reachable_chain": list(selected_proposal["reachable_chain"]),
        "falsifier": dict(selected_proposal["falsifier"]),
        "evidence_refs": list(selected_proposal["evidence_refs"]),
        "structural_change": selected_contract["structural_change"],
        "expected_diff": selected_contract["expected_diff"],
        "why_not_threshold_tuning": selected_contract[
            "why_not_threshold_tuning"
        ],
        "selected_proposal": {
            key: value
            for key, value in selected_proposal.items()
            if key != "direction"
        },
        "proposal_packet_digest": hashlib.sha256(
            json.dumps(
                packet,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    data["proposal_ensemble"] = packet
    return data, []


async def _run_master_proposal_ensemble(
    planning_context: str,
    *,
    source_v: int,
    next_v: int,
    ui,
    log_dir: Path,
    allowed_evidence_snapshot_dir: str,
    baseline_v: int | None = None,
    protocol_bootstrap_prepared_only: bool = False,
    strict_checkpoint: dict | None = None,
) -> str:
    """Three proposals, two anonymous criterion critics, deterministic ordering.

    The ensemble is advisory.  It cannot alter lineage, evidence cutoffs,
    executable literals, or gates; the final plan still passes the canonical
    schema/compiler/validator path.
    """
    import asyncio

    context_digest = hashlib.sha256(planning_context.encode("utf-8")).hexdigest()
    try:
        baseline_dir = get_bot_dir(
            int(baseline_v) if baseline_v is not None else int(source_v)
        )
        source_graph, source_code_digest = _source_symbol_graph(baseline_dir)
    except Exception as exc:
        return _proposal_packet_error(
            f"source_symbol_index_failed:{type(exc).__name__}:{str(exc)[:240]}",
            context_digest=context_digest,
        )
    if not source_graph:
        return _proposal_packet_error(
            "source_symbol_index_empty",
            context_digest=context_digest,
        )
    snapshot_dir = Path(allowed_evidence_snapshot_dir)
    source_symbol_index = _source_symbol_prompt_index(source_graph)
    strict_authority_enabled = (
        protocol_bootstrap_prepared_only
        and isinstance(strict_checkpoint, dict)
    )
    proposal_read_dirs = (
        [get_bot_dir(int(next_v))]
        if protocol_bootstrap_prepared_only
        else [get_bot_dir(int(source_v)), get_bot_dir(int(next_v))]
    )

    async def propose(
        direction: str,
        directive: str,
        *,
        repair: dict | None = None,
    ):
        strict_call = None
        if strict_authority_enabled:
            from strict_authority_workflow import new_call, proposal_call_context

            strict_call = new_call(
                strict_checkpoint,
                slot=f"proposal:{direction}",
                context_binding=proposal_call_context(
                    context_digest=context_digest,
                    source_code_digest=source_code_digest,
                    direction=direction,
                ),
            )
            invocation_id = strict_call["invocation_id"]
            if repair is None and strict_call.get("schema_retry_required"):
                prior_rejection = strict_call.get("prior_schema_rejection") or {}
                repair = {
                    "kind": (
                        "distinctness"
                        if prior_rejection.get("rejection_kind")
                        == "proposal_identity_collision"
                        else "schema"
                    )
                }
        else:
            from system_strict_bootstrap import new_llm_invocation_id

            invocation_id = new_llm_invocation_id()
        repair_kind = str((repair or {}).get("kind") or "")
        is_repair = bool(repair_kind)
        is_distinctness_repair = repair_kind == "distinctness"
        purpose = f"master_proposal_scout:{direction}"
        log_file = log_dir / (
            f"master_proposal_{direction}_{'distinctness' if is_distinctness_repair else 'schema'}_retry_io.txt"
            if is_repair
            else f"master_proposal_{direction}_io.txt"
        )
        retry_label = (
            " DISTINCTNESS RETRY"
            if is_distinctness_repair
            else " SCHEMA RETRY" if is_repair else ""
        )
        proposal_role = f"MASTER PROPOSAL {direction}{retry_label}"
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            proposal_role,
            producer=_render_master_proposal_provider_prompt,
            renderer_inputs={
                "planning_context": planning_context,
                "direction": str(direction),
                "directive": str(directive),
                "source_v": int(source_v),
                "next_v": int(next_v),
                "protocol_bootstrap_prepared_only": bool(
                    protocol_bootstrap_prepared_only
                ),
                "source_symbol_index": source_symbol_index,
                "repair_kind": repair_kind,
                "invocation_id": str(invocation_id),
            },
        )
        output, cost_usd, usage = await run_claude_query(
            rendered_prompt,
            [],
            ui,
            proposal_role,
            log_file,
            tools=["Read"],
            allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
            allowed_read_dirs=proposal_read_dirs,
            strict_authority=strict_call,
        )
        return {
            "output": output,
            "cost_usd": cost_usd,
            "usage": usage,
            "invocation_id": invocation_id,
            "purpose": purpose,
            "role": (
                f"MASTER PROPOSAL {direction}"
                f"{retry_label}"
            ),
            "prompt": str(rendered_prompt),
            "log_file": str(log_file),
            "strict_call": strict_call,
        }

    proposal_results = await gather_llm_fail_fast(
        *(propose(direction, directive) for direction, directive in _MASTER_PROPOSAL_DIRECTIONS),
    )
    proposals = []
    proposal_invocations: dict[str, dict] = {}
    seen_proposal_ids: set[str] = set()
    proposal_exceptions = []
    invalid_proposal_specs: list[tuple[str, str, dict]] = []
    accepted_proposal_directions: dict[str, str] = {}
    for (direction, _directive), result in zip(_MASTER_PROPOSAL_DIRECTIONS, proposal_results):
        if isinstance(result, BaseException):
            proposal_exceptions.append(result)
            invalid_proposal_specs.append(
                (direction, _directive, {"kind": "schema"})
            )
            continue
        output = result.get("output", "") if isinstance(result, dict) else ""
        proposal = _validated_master_proposal(
            output,
            direction,
            source_graph=source_graph,
            snapshot_dir=snapshot_dir,
            national_policy_only=True,
        )
        if proposal is None:
            invalid_proposal_specs.append(
                (direction, _directive, {"kind": "schema"})
            )
            continue
        proposal_id = proposal["proposal_id"]
        if proposal_id in seen_proposal_ids:
            if strict_authority_enabled:
                from strict_authority_workflow import reject_duplicate_proposal

                reject_duplicate_proposal(result["strict_call"])
            invalid_proposal_specs.append((
                direction,
                _directive,
                {
                    "kind": "distinctness",
                    "proposal_id": proposal_id,
                    "conflicting_direction": accepted_proposal_directions[
                        proposal_id
                    ],
                },
            ))
            continue
        from system_strict_bootstrap import (
            llm_result_digest,
            record_llm_invocation_evidence,
        )

        if strict_authority_enabled:
            from strict_authority_workflow import accept_role_result

            accept_role_result(
                result["strict_call"],
                role_result=proposal,
                parse_contract=_PROPOSAL_SCHEMA_VERSION,
            )

        proposal_invocations[proposal_id] = record_llm_invocation_evidence(
            invocation_id=result["invocation_id"],
            purpose=result["purpose"],
            role=result["role"],
            prompt_digest=hashlib.sha256(
                result["prompt"].encode("utf-8")
            ).hexdigest(),
            raw_output_digest=hashlib.sha256(output.encode("utf-8")).hexdigest(),
            result_digest=llm_result_digest(result["cost_usd"], result["usage"]),
            role_result=proposal,
            log_file=result["log_file"],
        )
        seen_proposal_ids.add(proposal_id)
        accepted_proposal_directions[proposal_id] = direction
        proposals.append(proposal)
    if invalid_proposal_specs:
        retry_results = await gather_llm_fail_fast(
            *(
                propose(direction, directive, repair=repair)
                for direction, directive, repair in invalid_proposal_specs
            ),
        )
        for (direction, _directive, _repair), result in zip(
            invalid_proposal_specs, retry_results
        ):
            if isinstance(result, LLMAvailabilityBlocked):
                raise result
            if isinstance(result, BaseException):
                proposal_exceptions.append(result)
                continue
            output = result.get("output", "") if isinstance(result, dict) else ""
            proposal = _validated_master_proposal(
                output,
                direction,
                source_graph=source_graph,
                snapshot_dir=snapshot_dir,
                national_policy_only=True,
            )
            if proposal is None:
                continue
            proposal_id = proposal["proposal_id"]
            if proposal_id in seen_proposal_ids:
                if strict_authority_enabled:
                    from strict_authority_workflow import reject_duplicate_proposal

                    reject_duplicate_proposal(result["strict_call"])
                continue
            from system_strict_bootstrap import (
                llm_result_digest,
                record_llm_invocation_evidence,
            )

            if strict_authority_enabled:
                from strict_authority_workflow import accept_role_result

                accept_role_result(
                    result["strict_call"],
                    role_result=proposal,
                    parse_contract=_PROPOSAL_SCHEMA_VERSION,
                )

            proposal_invocations[proposal_id] = record_llm_invocation_evidence(
                invocation_id=result["invocation_id"],
                purpose=result["purpose"],
                role=result["role"],
                prompt_digest=hashlib.sha256(
                    result["prompt"].encode("utf-8")
                ).hexdigest(),
                raw_output_digest=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                result_digest=llm_result_digest(
                    result["cost_usd"], result["usage"]
                ),
                role_result=proposal,
                log_file=result["log_file"],
            )
            seen_proposal_ids.add(proposal_id)
            accepted_proposal_directions[proposal_id] = direction
            proposals.append(proposal)
    if len(proposals) != len(_MASTER_PROPOSAL_DIRECTIONS):
        if len(proposal_exceptions) == len(proposal_results) + len(
            invalid_proposal_specs
        ):
            raise RuntimeError(
                "all_proposal_scout_calls_failed:"
                f"{type(proposal_exceptions[0]).__name__}:"
                f"{str(proposal_exceptions[0])[:240]}"
            ) from proposal_exceptions[0]
        return _proposal_packet_error(
            "three_distinct_schema_valid_scout_proposals_required:"
            f"got_{len(proposals)}",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )

    async def critique(name: str, lens: str, *, schema_retry: bool = False):
        strict_call = None
        if strict_authority_enabled:
            from strict_authority_workflow import ballot_call_context, new_call

            strict_call = new_call(
                strict_checkpoint,
                slot=f"ballot:{name}",
                context_binding=ballot_call_context(
                    context_digest=context_digest,
                    source_code_digest=source_code_digest,
                    critic_id=name,
                    proposal_ids=(item["proposal_id"] for item in proposals),
                    critic_criteria=_PROPOSAL_CRITIC_CRITERIA,
                ),
            )
            invocation_id = strict_call["invocation_id"]
        else:
            from system_strict_bootstrap import new_llm_invocation_id

            invocation_id = new_llm_invocation_id()
        # No scout lens/identity is exposed.  Each critic receives a different
        # but replayable ordering derived from the immutable planning digest.
        critic_proposals = [
            {key: value for key, value in proposal.items() if key != "direction"}
            for proposal in proposals
        ]
        critic_proposals.sort(
            key=lambda item: hashlib.sha256(
                f"{context_digest}:{name}:{item['proposal_id']}".encode("utf-8")
            ).hexdigest()
        )
        purpose = f"master_proposal_critic:{name}"
        log_file = log_dir / (
            f"master_proposal_critic_{name}_schema_retry_io.txt"
            if schema_retry
            else f"master_proposal_critic_{name}_io.txt"
        )
        critic_role = (
            f"MASTER PROPOSAL CRITIC {name}"
            f"{' SCHEMA RETRY' if schema_retry else ''}"
        )
        from llm_query import render_llm_prompt

        rendered_prompt = render_llm_prompt(
            critic_role,
            producer=_render_master_proposal_critic_provider_prompt,
            renderer_inputs={
                "proposal_name": str(name),
                "lens": str(lens),
                "planning_context_digest": context_digest,
                "proposals": critic_proposals,
                "criteria": _PROPOSAL_CRITIC_CRITERIA,
                "schema_retry": bool(schema_retry),
                "invocation_id": str(invocation_id),
            },
        )
        output, cost_usd, usage = await run_claude_query(
            rendered_prompt,
            [],
            ui,
            critic_role,
            log_file,
            tools=[],
            strict_authority=strict_call,
        )
        return {
            "output": output,
            "cost_usd": cost_usd,
            "usage": usage,
            "invocation_id": invocation_id,
            "purpose": purpose,
            "role": (
                f"MASTER PROPOSAL CRITIC {name}"
                f"{' SCHEMA RETRY' if schema_retry else ''}"
            ),
            "prompt": str(rendered_prompt),
            "log_file": str(log_file),
            "critic_id": name,
            "strict_call": strict_call,
        }

    critic_results = await gather_llm_fail_fast(
        critique("falsification", "Counterfactual quality, causal attribution, and evidence support."),
        critique("scope", "Reachability, bounded implementation scope, and regression risk."),
    )
    proposal_ids = {item["proposal_id"] for item in proposals}
    critiques = []
    invalid_critics = []
    critic_specs = (
        ("falsification", "Counterfactual quality, causal attribution, and evidence support."),
        ("scope", "Reachability, bounded implementation scope, and regression risk."),
    )
    for spec, result in zip(critic_specs, critic_results):
        if isinstance(result, BaseException):
            invalid_critics.append(spec)
            continue
        output = result.get("output", "") if isinstance(result, dict) else ""
        critique_row = _validated_proposal_critique(output, proposal_ids)
        if critique_row is not None:
            from system_strict_bootstrap import (
                llm_result_digest,
                record_llm_invocation_evidence,
            )

            critique_row["critic_id"] = result["critic_id"]
            if strict_authority_enabled:
                from strict_authority_workflow import accept_role_result

                accept_role_result(
                    result["strict_call"],
                    role_result={
                        key: value
                        for key, value in critique_row.items()
                        if key not in {"critic_id", "invocation_evidence"}
                    },
                    parse_contract="master-proposal-ballot-v1",
                )
            critique_row["invocation_evidence"] = (
                record_llm_invocation_evidence(
                    invocation_id=result["invocation_id"],
                    purpose=result["purpose"],
                    role=result["role"],
                    prompt_digest=hashlib.sha256(
                        result["prompt"].encode("utf-8")
                    ).hexdigest(),
                    raw_output_digest=hashlib.sha256(
                        output.encode("utf-8")
                    ).hexdigest(),
                    result_digest=llm_result_digest(
                        result["cost_usd"], result["usage"]
                    ),
                    role_result={
                        key: value
                        for key, value in critique_row.items()
                        if key not in {"critic_id", "invocation_evidence"}
                    },
                    log_file=result["log_file"],
                )
            )
            critiques.append(critique_row)
        else:
            invalid_critics.append(spec)

    if invalid_critics:
        retry_results = await gather_llm_fail_fast(
            *(
                critique(name, lens, schema_retry=True)
                for name, lens in invalid_critics
            ),
        )
        for result in retry_results:
            if isinstance(result, LLMAvailabilityBlocked):
                raise result
            if isinstance(result, BaseException):
                raise RuntimeError(
                    "proposal_critic_call_failed:"
                    f"{type(result).__name__}:{str(result)[:240]}"
                ) from result
            output = result.get("output", "") if isinstance(result, dict) else ""
            critique_row = _validated_proposal_critique(output, proposal_ids)
            if critique_row is not None:
                from system_strict_bootstrap import (
                    llm_result_digest,
                    record_llm_invocation_evidence,
                )

                critique_row["critic_id"] = result["critic_id"]
                if strict_authority_enabled:
                    from strict_authority_workflow import accept_role_result

                    accept_role_result(
                        result["strict_call"],
                        role_result={
                            key: value
                            for key, value in critique_row.items()
                            if key not in {"critic_id", "invocation_evidence"}
                        },
                        parse_contract="master-proposal-ballot-v1",
                    )
                critique_row["invocation_evidence"] = (
                    record_llm_invocation_evidence(
                        invocation_id=result["invocation_id"],
                        purpose=result["purpose"],
                        role=result["role"],
                        prompt_digest=hashlib.sha256(
                            result["prompt"].encode("utf-8")
                        ).hexdigest(),
                        raw_output_digest=hashlib.sha256(
                            output.encode("utf-8")
                        ).hexdigest(),
                        result_digest=llm_result_digest(
                            result["cost_usd"], result["usage"]
                        ),
                        role_result={
                            key: value
                            for key, value in critique_row.items()
                            if key not in {"critic_id", "invocation_evidence"}
                        },
                        log_file=result["log_file"],
                    )
                )
                critiques.append(critique_row)

    if len(critiques) != 2:
        return _proposal_packet_error(
            f"expected_two_schema_valid_critics_got_{len(critiques)}",
            context_digest=context_digest,
            source_code_digest=source_code_digest,
        )

    # Deterministic equal-criterion aggregation. Critic prose cannot
    # create/delete a candidate, and both independent critics must reject a
    # deterministically valid candidate before rejection affects ordering.
    order = {item["proposal_id"]: index for index, item in enumerate(proposals)}
    scores = {proposal_id: 0 for proposal_id in proposal_ids}
    rejects = {proposal_id: 0 for proposal_id in proposal_ids}
    for critique_row in critiques:
        for ballot in critique_row["ballots"]:
            scores[ballot["proposal_id"]] += ballot["total_score"]
        for proposal_id in critique_row["reject"]:
            rejects[proposal_id] += 1
    proposals.sort(
        key=lambda item: (
            rejects[item["proposal_id"]] >= 2,
            -scores[item["proposal_id"]],
            order[item["proposal_id"]],
        )
    )
    packet = {
        "schema_version": _PROPOSAL_PACKET_SCHEMA_VERSION,
        "valid": True,
        "authority": (
            "advisory_only; final Master must obey frozen lineage/evidence and canonical "
            "runtime/schema/gate contracts"
        ),
        "context_digest": context_digest,
        "source_code_digest": source_code_digest,
        "critic_criteria": _PROPOSAL_CRITIC_CRITERIA,
        "proposal_count": len(proposals),
        "valid_critic_count": len(critiques),
        "allowed_proposal_ids": [item["proposal_id"] for item in proposals],
        "ordered_proposals": proposals,
        "proposal_invocations": proposal_invocations,
        "critic_reviews": critiques,
    }
    return json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _render_analysis_section(text: str, default_msg: str) -> str:
    """Map an analyst's raw return into the text injected into the Master prompt.

    - Empty/None -> default "no data" message (unchanged behaviour).
    - LLM_INFRA_SENTINEL -> explicit "LLM crashed" warning (so the Master does
      not misread a missing analysis as a negative business signal).
    - Anything else -> the actual analysis text.
    """
    if not text or not text.strip():
        return default_msg
    if text.strip() == LLM_INFRA_SENTINEL:
        return LLM_INFRA_SENTINEL_MSG
    return text


def _line_budget_summary(bot_v: int, *, baseline_label: str = "source") -> str:
    """Summarize LOC pressure for the exact baseline Workers will edit."""
    try:
        bot_dir = get_bot_dir(bot_v)
    except Exception:
        return "Line budget: unavailable."
    lines = [f"Line budget / file-size pressure ({baseline_label}={bot_dir.name}):"]
    for filename in ("policy.py",):
        path = bot_dir / filename
        if not path.exists():
            continue
        try:
            count = sum(1 for _ in path.open(encoding="utf-8"))
        except Exception:
            continue
        remaining = MAX_LINES_HARD_CAP - count
        status = "ok"
        if remaining <= 100:
            status = "near_hard_cap"
        lines.append(f"- {filename}: {count}/{MAX_LINES_HARD_CAP} lines, remaining={remaining}, status={status}")
    if len(lines) == 1:
        return "Line budget: policy.py not found."
    if any("near_hard_cap" in line for line in lines):
        lines.append(
            "MANDATORY when near_hard_cap: recover LOC inside policy.py before adding "
            "behavior; candidate helper modules are outside the artifact ABI."
        )
    return "\n".join(lines)


def _exact_official_compliance_feedback(baseline_v: int) -> str:
    """Render only compliance facts bound to this current-epoch artifact.

    Planning must not summarize the newest files in the global official status
    directory: those files may describe another version, artifact, or epoch.
    The strict epoch receipt and candidate hash must both bind a fact to the
    exact readable baseline. Advisory LLM repair prose and EXE outcomes are
    deliberately excluded.
    """

    baseline = get_bot_dir(int(baseline_v))
    label = bot_name(int(baseline_v))
    unavailable = (
        f"No exact-identity official-full-v5 compliance fact is available for "
        f"{label}; feedback from other epochs, versions, or artifact hashes is excluded."
    )
    try:
        from bot_artifact import hash_path
        from bot_namespace import ROLE_CANDIDATE, resolve_national_bot_spec
        from official_certification import (
            FULL_POLICY_ID,
            _deterministic_status_receipt_issues,
            read_status,
        )

        spec = resolve_national_bot_spec(
            baseline,
            ROLE_CANDIDATE,
            require_completion=False,
            require_certificate=False,
        )
        if not spec.eligible:
            return unavailable
        artifact_hash = hash_path(baseline)
        status = read_status(baseline)
        identity = (
            status.get("certification_identity")
            if isinstance(status, dict)
            else None
        )
        if not isinstance(identity, dict):
            return unavailable
        if (
            status.get("bot") != label
            or status.get("mode") != "full"
            or status.get("policy_id") != FULL_POLICY_ID
            or identity.get("policy_id") != FULL_POLICY_ID
            or identity.get("candidate_hash") != artifact_hash
        ):
            return unavailable
        # Mutable status JSON and issue strings are never planning authority on
        # their own. Re-open the content-bound deterministic evidence/archive
        # receipt for this exact live artifact before admitting even a
        # compliance-only issue. Signed publication certificates cover passes;
        # this path exists to carry exact failed/inconclusive protocol facts.
        if _deterministic_status_receipt_issues(status, candidate=baseline):
            return unavailable
        receipt = status.get("official_deterministic_status_receipt")
        verdict = receipt.get("verdict") if isinstance(receipt, dict) else None
        if not isinstance(verdict, dict):
            return unavailable
        issues = verdict.get("issues") or []
        if not isinstance(issues, list):
            return unavailable
        lines = [
            "Exact current-epoch artifact compliance fact only; official EXE "
            "wins, losses, chips, THP earnings, and advisory repair prose are excluded.",
            (
                f"- {label}: artifact_hash={artifact_hash}, policy={FULL_POLICY_ID}, "
                f"status={status.get('status')}, "
                f"classification={verdict.get('classification')}, "
                f"blocking={bool(verdict.get('blocking'))}, "
                f"inconclusive={bool(verdict.get('inconclusive'))}"
            ),
        ]
        if issues:
            lines.append(
                "  deterministic_issues: "
                + "; ".join(str(item)[:180] for item in issues[:5])
            )
        return "\n".join(lines)
    except Exception:
        return unavailable


# ──────────────────────────────────────────────
# Master Analysis
# ──────────────────────────────────────────────

async def _run_master_analysis(source_v, next_v, stagnation_info, ui,
                               match_analysis="", performance_verification="",
                               replay_spotlight="", bot_action_stats="",
                               opponent_profiles="", research_proposals="",
                               architecture_policy=None,
                               prepared_baseline=None,
                               protocol_bootstrap=None):
    """Run Master analysis — can run concurrently with daemon evaluation."""
    master_prompt = (
        Path(__file__).resolve().parent / "prompts" / "master_prompt.md"
    ).read_text(encoding="utf-8")
    protocol_bootstrap_active = isinstance(protocol_bootstrap, dict)
    strict_checkpoint = None
    if protocol_bootstrap_active:
        from evolution_infra import read_pipeline_checkpoint
        from system_strict_bootstrap import is_declared_native_bootstrap

        strict_checkpoint = read_pipeline_checkpoint() or {}
        if not is_declared_native_bootstrap(strict_checkpoint):
            raise MasterInfrastructureError(
                source_v,
                next_v,
                hashlib.sha256(b"strict-checkpoint-missing").hexdigest(),
                "strict_authority_checkpoint_not_declared",
            )
    # Apply section budgets so one evidence source cannot crowd out match analysis.
    # C-class: render the sentinel (returned when the analyst LLM crashed on an
    # infrastructure error) into an explicit warning BEFORE trimming, so the
    # Master sees "LLM crashed" rather than "no data" (which would be read as a
    # negative business signal). Non-sentinel text passes through unchanged.
    match_analysis_rendered = (
        PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
        if protocol_bootstrap_active
        else _render_analysis_section(match_analysis, "")
    )
    perf_rendered = (
        PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
        if protocol_bootstrap_active
        else _render_analysis_section(
            performance_verification, "No performance verification data available.",
        )
    )
    if protocol_bootstrap_active:
        stagnation_info = PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
    match_analysis_trimmed = _trim_to_budget(match_analysis_rendered, 10_000, tail=True)
    perf_trimmed = _trim_to_budget(perf_rendered, 4_000)

    if protocol_bootstrap_active:
        bot_action_stats_trimmed = PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
        opponent_profiles_trimmed = PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
        replay_spotlight_trimmed = PROTOCOL_BOOTSTRAP_NO_STRENGTH_PLACEHOLDER
    else:
        bot_action_stats_trimmed = _trim_to_budget(
            bot_action_stats or "No bot action statistics available.", 12_000)
        opponent_profiles_trimmed = _trim_to_budget(
            opponent_profiles or "No per-opponent behavior profiles available.", 8_000)
        replay_spotlight_trimmed = _trim_to_budget(
            replay_spotlight or "No replay spotlight data available.", 8_000)
    research_trimmed = _trim_to_budget(
        (
            "No admissible non-match literature receipt was supplied for this "
            "protocol-bootstrap plan. Historical matchup-derived research was not loaded."
            if protocol_bootstrap_active
            else research_proposals
            or "No web-derived research proposals this generation (run_literature_probe not triggered or returned none)."
        ),
        4_000,
    )
    planning_baseline_v = (
        next_v
        if isinstance(prepared_baseline, dict) or protocol_bootstrap_active
        else source_v
    )
    planning_baseline_label = (
        "prepared_crossover_child"
        if isinstance(prepared_baseline, dict)
        else "prepared_protocol_bootstrap_child"
        if protocol_bootstrap_active
        else "source_parent"
    )
    if protocol_bootstrap_active:
        official_feedback = (
            "Historical official-certification feedback was not loaded. Use only "
            "the repository-pinned official oracle and architecture policy."
        )
    else:
        official_feedback = _trim_to_budget(
            _exact_official_compliance_feedback(int(planning_baseline_v)),
            6_000,
        )
    try:
        from national_capability_contract import national_runtime_feedback_summary
        runtime_feedback = _trim_to_budget(
            national_runtime_feedback_summary(
                get_bot_dir(planning_baseline_v),
                source_label=(
                    f"{bot_name(planning_baseline_v)} prepared crossover baseline"
                    if isinstance(prepared_baseline, dict)
                    else f"{bot_name(planning_baseline_v)} prepared protocol bootstrap baseline"
                    if protocol_bootstrap_active
                    else bot_name(source_v)
                ),
            ),
            4_000,
        )
    except Exception as exc:
        runtime_feedback = f"National runtime architecture feedback unavailable: {type(exc).__name__}: {str(exc)[:200]}"
    if isinstance(architecture_policy, dict):
        try:
            from runtime_architecture_policy import architecture_policy_prompt
            architecture_policy_text = architecture_policy_prompt(architecture_policy)
        except Exception as exc:
            architecture_policy_text = (
                f"Runtime architecture policy rendering failed: {type(exc).__name__}: {str(exc)[:200]}"
            )
    else:
        architecture_policy_text = "System-owned runtime architecture policy: not active for this source."
    try:
        from strategy_reference_pack import master_reference_summary
        strategy_reference_packet = _trim_to_budget(master_reference_summary(), 6_000)
    except Exception as exc:
        strategy_reference_packet = (
            "Local strategy reference cards unavailable: "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        )
    try:
        from workflow_profiles import get_workflow_profile, profile_summary
        workflow_profile = get_workflow_profile()
        workflow_profile_text = profile_summary(workflow_profile)
    except Exception as exc:
        raise RuntimeError(
            "workflow_profile_contract_unavailable: "
            f"{type(exc).__name__}: {str(exc)[:240]}"
        ) from exc
    line_budget_text = _line_budget_summary(
        planning_baseline_v,
        baseline_label=planning_baseline_label,
    )
    if isinstance(prepared_baseline, dict):
        try:
            from prepared_baseline_contract import prepared_baseline_prompt

            prepared_baseline_text = _trim_to_budget(
                prepared_baseline_prompt(prepared_baseline),
                18_000,
            )
        except Exception as exc:
            prepared_baseline_text = (
                "Prepared crossover baseline rendering failed closed before this "
                f"prompt should run: {type(exc).__name__}: {str(exc)[:240]}"
            )
    elif protocol_bootstrap_active:
        prepared_baseline_text = (
            "Protocol-bootstrap baseline: Workers start from the prepared target "
            "artifact whose national_bot.py has already been replaced and verified "
            "by the system-owned current runtime. The historical source launcher is "
            "not executable evidence."
        )
    else:
        prepared_baseline_text = (
            "No two-parent prepared baseline: Workers start from the copied source parent."
        )
    if protocol_bootstrap_active:
        h2h_data_file = "UNAVAILABLE_PROTOCOL_BOOTSTRAP"
        selection_data_file = "UNAVAILABLE_PROTOCOL_BOOTSTRAP"
        h2h_snapshot_contract = (
            "PROTOCOL BOOTSTRAP: no two-bot strict executable pool exists, so "
            "ratings, H2H, rankings, match replays, and strength conclusions are "
            "intentionally unavailable. Do not read live result files or cite "
            "historical quarantined ratings. Plan only from source code, typed "
            "strategy references, and the content-bound bootstrap context. "
            f"receipt={protocol_bootstrap.get('receipt_digest')}"
        )
        allowed_evidence_snapshot_dir = str(
            get_bot_dir(next_v) / ".protocol_bootstrap_no_strength_evidence"
        )
    else:
        try:
            from evidence_snapshot import (
                h2h_snapshot_contract_text,
                load_generation_snapshot_identity,
            )
            h2h_snapshot = load_generation_snapshot_identity(next_v)
            if not h2h_snapshot.get("available"):
                raise RuntimeError(
                    f"generation evidence snapshot unavailable: {h2h_snapshot.get('reason')}"
                )
            h2h_data_file = h2h_snapshot.get("h2h_relpath", "web/core/results/head_to_head.json")
            selection_data_file = h2h_snapshot.get(
                "selection_relpath",
                f"web/core/results/v{next_v}/evidence_snapshot/selection_snapshot.json",
            )
            h2h_snapshot_contract = h2h_snapshot_contract_text(next_v, source_v=source_v)
            allowed_evidence_snapshot_dir = str(
                Path(h2h_snapshot["manifest_path"]).parent
            )
        except Exception as exc:
            ui.log_history(
                f"Master blocked: stable evaluation snapshot unavailable ({exc})",
                "error",
            )
            return None

    if protocol_bootstrap_active:
        planning_code_input_contract = (
            f"- `{bot_relpath(next_v)}/` — the sole readable prepared code "
            "baseline and the target Workers must edit and verify.\n"
            f"- v{source_v} is numeric completion high-water authority only. It "
            "is not a source, parent, reference, opponent, or readable code input; "
            "do not open, compare, cite, import, or inherit it."
        )
        source_selection_contract = (
            f"This is the one-time fresh strict bootstrap for v{next_v}. "
            f"Historical high-water v{source_v} supplies only the next version "
            "number and is not an ancestor. The prepared target artifact is the "
            "only code baseline. You MUST NOT set `branch_from`, a parent, or any "
            "source-override field."
        )
        target_path_contract = (
            f"This bootstrap plans directly on prepared target `{bot_relpath(next_v)}/`. "
            "Every Worker edit/Read/py_compile command MUST point there; imports, "
            "smoke and dynamic tests belong to the system quality gate. "
            "No historical code directory is a readable comparison input; the "
            "system-owned bootstrap receipt proves fresh lineage."
        )
    else:
        planning_code_input_contract = (
            f"- `{bot_relpath(source_v)}/` — current source bot code; read-only "
            "parent/reference.\n"
            f"- `{bot_relpath(next_v)}/` — target bot directory; Workers must edit "
            "and verify this directory."
        )
        source_selection_contract = (
            "The source ancestor to evolve from is decided automatically by the "
            "system in prepare_generation from the frozen selection evidence. You "
            "MUST NOT set `branch_from` or any source-override field in your plan; "
            "the system rejects it. Focus only on the task plan and analysis."
        )
        target_path_contract = (
            f"This generation evolves source `{bot_relpath(source_v)}/` into target "
            f"`{bot_relpath(next_v)}/`.\n\nIn every `worker_prompt`, "
            f"edit/Read/py_compile commands MUST point at "
            f"`{bot_relpath(next_v)}/`, never `{bot_relpath(source_v)}/`. The source "
            "path is read-only comparison evidence; do not ask Workers to edit, "
            "patch, compile, import from, or run checks inside it. Imports, smoke "
            "and dynamic tests are system quality-gate work. The Worker "
            "wrapper already supplies the exact parent-vs-target diff."
        )

    master_template_values = {
        "stagnation_info": stagnation_info,
        "match_analysis": match_analysis_trimmed,
        "performance_verification": perf_trimmed,
        "source_v": str(source_v),
        "next_v": str(next_v),
        "replay_spotlight": replay_spotlight_trimmed,
        "bot_action_stats": bot_action_stats_trimmed,
        "opponent_profiles": opponent_profiles_trimmed,
        "research_proposals": research_trimmed,
        "official_feedback": official_feedback,
        "runtime_feedback": runtime_feedback,
        "strategy_reference_packet": strategy_reference_packet,
        "h2h_data_file": h2h_data_file,
        "selection_data_file": selection_data_file,
        "h2h_snapshot_contract": h2h_snapshot_contract,
        "master_plan_executable_contract": master_plan_executable_contract_text(),
        "planning_code_input_contract": planning_code_input_contract,
        "source_selection_contract": source_selection_contract,
        "target_path_contract": target_path_contract,
    }
    master_prompt = substitute_template(master_prompt, master_template_values)
    evidence_context = (
        "Protocol bootstrap has no strength snapshot. Do not open live ratings, "
        "H2H, replay, bot_stats, rating_history, or eval_rounds files.\n"
        if protocol_bootstrap_active
        else
        f"Selection evidence snapshot: {selection_data_file}\n"
        f"Use only that digest-bound snapshot for ratings, RD, games, coverage, trends, and ranking; "
        f"do not reopen live glicko_ratings.json, bot_stats.json, rating_history.jsonl, "
        f"or eval_rounds.jsonl.\n"
        f"Head-to-Head data snapshot: {h2h_data_file}\n"
        f"Do not read live H2H for matchup counts during planning; use the snapshot above.\n"
    )
    source_context = (
        "Historical lineage source directory: quarantined and intentionally not "
        "provided; do not open or cite it.\n"
        if protocol_bootstrap_active
        else f"Source bot directory (read-only parent): {bot_relpath(source_v)}/\n"
    )
    generation_identity_context = (
        f"Current strict bootstrap: numeric completion high-water v{source_v}; "
        f"fresh target v{next_v}. High-water v{source_v} is not a source or parent.\n"
        if protocol_bootstrap_active
        else f"Current evolution: v{source_v} → v{next_v}\n"
    )
    master_ctx = (
        generation_identity_context
        +
        f"{source_context}"
        f"Target bot directory (workers edit/verify): {bot_relpath(next_v)}/\n"
        f"Planning baseline: {bot_relpath(planning_baseline_v)}/ ({planning_baseline_label})\n"
        f"{evidence_context}"
        f"\n{h2h_snapshot_contract}\n"
        f"\n{workflow_profile_text}\n"
        f"\nOfficial EXE Compliance Feedback:\n{official_feedback}\n"
        f"\nNational Runtime Architecture Feedback:\n{runtime_feedback}\n"
        f"\nPrepared Baseline Contract:\n{prepared_baseline_text}\n"
        f"\n{architecture_policy_text}\n"
        f"\n{line_budget_text}\n"
    )
    master_log_file = get_logs_dir(next_v) / "master_io.txt"

    try:
        proposal_ensemble = await _run_master_proposal_ensemble(
            master_prompt + "\n" + master_ctx,
            source_v=int(source_v),
            next_v=int(next_v),
            ui=ui,
            log_dir=master_log_file.parent,
            allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
            baseline_v=int(planning_baseline_v),
            protocol_bootstrap_prepared_only=protocol_bootstrap_active,
            strict_checkpoint=strict_checkpoint,
        )
    except LLMAvailabilityBlocked:
        raise
    except Exception as exc:
        raise MasterInfrastructureError(
            source_v,
            next_v,
            hashlib.sha256(
                (master_prompt + "\n" + master_ctx).encode("utf-8")
            ).hexdigest(),
            f"proposal_ensemble:{type(exc).__name__}: {str(exc)[:400]}",
        ) from exc
    proposal_packet, proposal_packet_errors = _parse_valid_proposal_packet(
        proposal_ensemble
    )
    if proposal_packet_errors:
        ui.log_history(
            "Master blocked: proposal ensemble failed closed ("
            + "; ".join(proposal_packet_errors[:4])
            + ").",
            "error",
        )
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.master_proposal_packet_rejected",
                "error",
                f"Master v{next_v} proposal packet rejected",
                {
                    "next_v": next_v,
                    "source_v": source_v,
                    "errors": proposal_packet_errors,
                },
            )
        except Exception:
            pass
        return None
    assert proposal_packet is not None
    master_ctx += (
        "\n# Weak-model proposal ensemble (evidence-validated choices)\n"
        + proposal_ensemble
        + "\nFINAL PROPOSAL BINDING CONTRACT (overrides any conflicting embedded "
        "output example): select exactly one allowed proposal and emit its ID as the "
        "top-level string field selected_proposal_id. Copy that proposal's "
        "targeted_failure EXACTLY into the plan targeted_failure. Every selected "
        "proposal target_files path must be writable in at least one task. You may "
        "elaborate implementation details, but may not synthesize a fourth proposal, "
        "combine mechanisms, or treat critic votes as permission to change source, "
        "evidence, scope, or gates.\n"
    )

    master_schema_repair_suffix = ""
    for attempt in range(MAX_MASTER_RETRIES):
        ui.clear_io()
        strict_final_call = None
        final_role = f"MASTER (Try {attempt+1})"
        if protocol_bootstrap_active:
            from strict_authority_workflow import final_master_call_context, new_call

            strict_final_call = new_call(
                strict_checkpoint,
                slot="master:final",
                role=final_role,
                context_binding=final_master_call_context(
                    proposal_packet,
                    architecture_policy if isinstance(architecture_policy, dict) else {},
                ),
            )
        try:
            from llm_query import render_llm_prompt

            rendered_prompt = render_llm_prompt(
                final_role,
                producer=_render_master_final_provider_prompt,
                renderer_inputs={
                    "template_values": master_template_values,
                    "master_context": master_ctx,
                    "proposal_ensemble": proposal_ensemble,
                    "source_v": int(source_v),
                    "next_v": int(next_v),
                    "invocation_id": str(
                        (strict_final_call or {}).get("invocation_id") or ""
                    ),
                    "schema_repair_suffix": master_schema_repair_suffix,
                },
            )
            output, _, _ = await run_claude_query(
                rendered_prompt, [], ui,
                final_role, master_log_file,
                tools=["Read"],
                allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
                allowed_read_dirs=(
                    [get_bot_dir(int(next_v))]
                    if protocol_bootstrap_active
                    else [
                        get_bot_dir(int(source_v)),
                        get_bot_dir(int(next_v)),
                    ]
                ),
                strict_authority=strict_final_call,
            )
        except LLMAvailabilityBlocked:
            raise
        except Exception as exc:
            _final_mode = f"LLM_EXCEPTION:{type(exc).__name__}"
            try:
                ui.log_history(
                    f"Master LLM call failed ({type(exc).__name__}): {str(exc)[:240]}",
                    "error",
                )
            except Exception:
                pass
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.master_llm_call_failed",
                    "error",
                    (
                        f"Master v{next_v} try {attempt+1} LLM call failed: "
                        f"{type(exc).__name__}: {str(exc)[:240]}"
                    ),
                    {
                        "next_v": next_v,
                        "source_v": source_v,
                        "attempt": attempt + 1,
                        "failure_mode": _final_mode,
                        "exception_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                )
            except Exception:
                pass
            raise MasterInfrastructureError(
                source_v,
                next_v,
                hashlib.sha256(
                    (master_prompt + "\n" + master_ctx).encode("utf-8")
                ).hexdigest(),
                f"{type(exc).__name__}: {str(exc)[:400]}",
            ) from exc
        # A2 (v125 retry-storm fix): classify the parse failure so the log
        # distinguishes NO_FENCE (model never emitted JSON) / NO_JSON (empty) /
        # PARSE_ERROR (had JSON but unparseable) — instead of the undifferentiated
        # "malformed JSON" that hid three distinct root causes.
        from llm_query import parse_json_output_with_mode
        data, _failure_mode = parse_json_output_with_mode(output)
        if data and "tasks" in data:
            proposal_binding_errors = _validate_final_proposal_binding(
                data,
                proposal_packet,
            )
            if proposal_binding_errors:
                ui.log_history(
                    "Master plan rejected by proposal binding: "
                    + "; ".join(proposal_binding_errors[:4]),
                    "warn",
                )
                if attempt + 1 < MAX_MASTER_RETRIES:
                    master_prompt += (
                        "\n\n# Previous proposal binding failed; re-emit the complete "
                        "plan and fix all items:\n- "
                        + "\n- ".join(proposal_binding_errors)[:1500]
                        + "\n"
                    )
                    import asyncio
                    await asyncio.sleep(2)
                    continue
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.master_proposal_binding_exhausted",
                        "error",
                        f"Master v{next_v} failed proposal binding after retries",
                        {
                            "next_v": next_v,
                            "source_v": source_v,
                            "errors": proposal_binding_errors,
                        },
                    )
                except Exception:
                    pass
                return None
            selected_proposal_id = data.pop("selected_proposal_id")
            selected_proposal = next(
                item
                for item in proposal_packet["ordered_proposals"]
                if item["proposal_id"] == selected_proposal_id
            )
            data = _bind_selected_proposal_workers(data, selected_proposal)
            # The structured runtime contract and reference-card choice already
            # determine a small set of literal execution anchors.  Bind those
            # system-owned terms before Pydantic validation instead of asking a
            # weaker planner model to reproduce them losslessly in free prose.
            # Invalid contracts are intentionally left untouched and still fail
            # the canonical schema gate below.
            from plan_compiler import (
                bind_system_owned_policy_abi,
                bind_system_owned_worker_contract_terms,
            )
            data, _policy_abi_binding_meta = (
                bind_system_owned_policy_abi(
                    data,
                    policy=(
                        architecture_policy
                        if isinstance(architecture_policy, dict)
                        else None
                    ),
                )
            )
            data, _binding_meta = bind_system_owned_worker_contract_terms(data)
            if _policy_abi_binding_meta.get("bound"):
                ui.log_history(
                    "Master plan compiler bound the closed national policy ABI.",
                    "info",
                )
            if _binding_meta.get("bound"):
                ui.log_history(
                    "Master plan contract compiler bound missing execution anchors "
                    f"for {len(_binding_meta.get('bound_tasks', []))} worker task(s).",
                    "info",
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.master_contract_terms_bound",
                        "info",
                        f"Master v{next_v}: bound system-owned worker contract terms",
                        {
                            "next_v": next_v,
                            "source_v": source_v,
                            "attempt": attempt + 1,
                            "binding": _binding_meta,
                        },
                    )
                except Exception:
                    pass
            # P0 修复：在 Pydantic 剥离 branch_from (extra='ignore') 之前，对原始 dict
            # 跑 Master 的 source-override 硬校验。MasterPlan 删除 branch_from 字段后，
            # model_validate 会静默丢弃该键，必须在丢弃前拦截。
            # This pre-schema check catches source override fields before
            # Pydantic strips unknown keys. Canonical validation runs again
            # after plan normalization in the tool layer.
            _src_override = any(data.get(f) for f in ("branch_from", "source_override", "source_v_override"))
            if _src_override:
                ui.log_history(
                    "Master plan rejected: must not set branch_from.",
                    "warn",
                )
                import asyncio as _asyncio
                await _asyncio.sleep(2)
                continue
            from output_schema import validate_agent_output
            data, errors = validate_agent_output("master", data)
            if errors:
                ui.log_history(f"Master plan validation issues: {'; '.join(errors[:3])}", "warn")
                # Hard gate: inject schema errors into the next retry's prompt so
                # the Master re-emits strictly schema-conformant JSON rather than
                # silently returning the malformed plan. errors text is truncated
                # to avoid unbounded prompt growth across retries.
                if attempt + 1 < MAX_MASTER_RETRIES:
                    err_block = "\n".join(f"- {e}" for e in errors)[:1500]
                    master_schema_repair_suffix += (
                        "\n\n# 上一轮计划校验失败，必须修正：\n"
                        + err_block
                        + "\n请重新输出严格符合 schema 的 JSON。"
                    )
                    ui.log_history("Master plan rejected by schema. Retrying with errors...", "warn")
                    import asyncio
                    await asyncio.sleep(2)
                    continue
                # Retries exhausted: fail closed. A malformed plan cannot become
                # an executable worker contract merely because retries ran out.
                ui.log_history(
                    f"Master plan still violates schema after {MAX_MASTER_RETRIES} retries; "
                    "rejecting generation plan.",
                    "error"
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.master_schema_gate_exhausted", "error",
                        f"Master plan schema validation failed after {MAX_MASTER_RETRIES} retries: "
                        + "; ".join(errors[:5]),
                    )
                except Exception:
                    pass
                return None
            selected_contract = _selected_proposal_contract(selected_proposal)
            data["selected_proposal_id"] = selected_proposal_id
            data["proposal_binding"] = {
                "schema_version": _PROPOSAL_PACKET_SCHEMA_VERSION,
                "selected_proposal_id": selected_proposal_id,
                "contract_digest": selected_contract["contract_digest"],
                "context_digest": proposal_packet["context_digest"],
                "source_code_digest": proposal_packet["source_code_digest"],
                "target_files": list(selected_proposal["target_files"]),
                "source_symbols": list(selected_proposal["source_symbols"]),
                "reachable_chain": list(selected_proposal["reachable_chain"]),
                "falsifier": dict(selected_proposal["falsifier"]),
                "evidence_refs": list(selected_proposal["evidence_refs"]),
                "structural_change": selected_contract["structural_change"],
                "expected_diff": selected_contract["expected_diff"],
                "why_not_threshold_tuning": selected_contract[
                    "why_not_threshold_tuning"
                ],
                "selected_proposal": {
                    key: value
                    for key, value in selected_proposal.items()
                    if key != "direction"
                },
                "proposal_packet_digest": hashlib.sha256(
                    json.dumps(
                        proposal_packet,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
            # Freeze all three independent proposals and both anonymous ballots
            # with the selected plan.  The deterministic Worker envelope then
            # binds the actual governance evidence, not merely an invocation bit.
            data["proposal_ensemble"] = proposal_packet
            if protocol_bootstrap_active:
                from strict_authority_workflow import accept_role_result

                accept_role_result(
                    strict_final_call,
                    role_result=data,
                    parse_contract="master-plan-schema-v1",
                )
            # SUCCESS path (BUGFIX, root cause of the v107–v127 Master deadlock):
            # the plan parsed with `tasks`, carries no branch_from override, and
            # passed schema validation with NO errors. This `return data` was
            # MISSING for 11+ generations: every valid plan fell through to the
            # "Master output malformed JSON" branch below, burned all
            # MAX_MASTER_RETRIES, and returned None. The SDK-signature fix
            # (48b51f2/c537ff1) only cured the EMPTY-output case — once plans
            # came back non-empty and valid, this missing return STILL discarded
            # them, which is exactly why "malformed-JSON persists post-fix" was
            # observed. NOT a schema/SDK-sig/direction-audit problem.
            ui.log_history("Master plan accepted (valid JSON, schema-clean).", "info")
            # RC1 (success-path symmetry): emit the success terminal event here so
            # the clean-success path is as visible as the failure paths above. The
            # degraded path (:177) already emits pipeline.master_schema_gate_exhausted
            # (error) — only this clean branch was event-silent. Without it, a
            # master-success-return-bug regression (valid plan parsed but the
            # function then failed to return) is invisible in the event stream;
            # prepare_done=N vs master_plan_accepted=0 would now expose it at once.
            try:
                from event_bus import success
                success("pipeline.master_plan_accepted",
                        f"Master v{next_v} plan accepted (schema-clean, try {attempt+1})",
                        next_v=next_v, source_v=source_v,
                        master_try=attempt + 1,
                        num_tasks=len(data.get("tasks", [])),
                        selected_proposal_id=selected_proposal_id,
                        proposal_context_digest=proposal_packet["context_digest"])
            except Exception:
                pass
            return data
        ui.log_history(
            f"Master output malformed JSON (mode={_failure_mode}). Retrying...",
            "warn",
        )
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.master_malformed_json", "warn",
                f"Master v{next_v} try {attempt+1} output parse failed (mode={_failure_mode})",
                {"next_v": next_v, "source_v": source_v, "attempt": attempt + 1,
                 "failure_mode": _failure_mode, "output_len": len(output or "")},
            )
        except Exception:
            pass
        import asyncio
        await asyncio.sleep(2)

    _final_mode = locals().get("_failure_mode", "UNKNOWN")
    ui.log_history(
        f"Master failed to plan after {MAX_MASTER_RETRIES} retries (last mode={_final_mode}).",
        "error",
    )
    try:
        from system_log import log_system_event
        log_system_event(
            "pipeline.master_failed_to_plan", "error",
            f"Master v{next_v} failed to plan after {MAX_MASTER_RETRIES} retries (last mode={_final_mode})",
            {"next_v": next_v, "source_v": source_v,
             "last_failure_mode": _final_mode, "retries": MAX_MASTER_RETRIES},
        )
    except Exception:
        pass
    return None
