"""Immutable execution plans for national precommit evaluation.

The live ladder is allowed to change while a candidate is repaired.  The
candidate's final comparison set is not: opponents, evaluator semantics and
randomness are frozen the first time precommit starts and revalidated before
every retry and commit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from bot_artifact import canonical_digest, hash_path, published_bot_identity


ROOT = Path(__file__).resolve().parents[2]
PLAN_SCHEMA_VERSION = 8
EVALUATION_CONTRACT_SCHEMA_VERSION = 5
DEFAULT_DECK_SEED_BASE = 91_000
NATIVE_PRECOMMIT_BATCH_PLAN_SCHEMA_VERSION = 1

SEMANTIC_PATHS = (
    "sever/engine/deck.py",
    "sever/engine/evaluator.py",
    "sever/engine/game.py",
    "sever/engine/validator.py",
    "sever/server/protocol.py",
    "web/core/eval_stats.py",
    "web/core/first_strict_control.py",
    "web/core/first_strict_execution_journal.py",
    "web/core/bootstrap_assets/first_strict_control_v1/manifest.json",
    "web/core/bootstrap_assets/first_strict_control_v1/policy.py",
    "web/core/managed_bot_executor.py",
    "web/core/managed_bot_socket.py",
    "web/core/national_bot_launcher.py",
    "web/core/national_game_runtime.py",
    "web/core/national_native.py",
    "web/core/pipeline_state.py",
    "web/core/orchestrator.py",
    "web/core/national_runtime_authority.py",
    "sever/server/transport.py",
    "web/core/precommit_eval_contract.py",
    "web/core/pool_ratchet_gate.py",
    "web/core/strength_order.py",
    "web/core/tool_eval.py",
    "web/core/tool_gates.py",
    "web/core/tool_commit.py",
    "web/core/elo_daemon.py",
    "web/core/rating_snapshot.py",
    "web/core/replay_analysis.py",
)

_PUBLISHED_IDENTITY_FIELDS = (
    "artifact_hash",
    "tag",
    "tag_type",
    "tag_object",
    "commit_oid",
    "completion_tree_oid",
    "main_tree_oid",
)


class PrecommitEvalContractError(RuntimeError):
    """Raised when a precommit plan cannot be created without ambiguity."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evaluator_identity(
    *,
    profile_id: str,
    execution_mode: str,
    evaluation_protocol: str,
) -> dict[str, Any]:
    from bot_namespace import (
        NATIONAL_ARTIFACT_CONTRACT_SCHEMA_VERSION,
        NATIONAL_ENTRYPOINT,
        NATIONAL_RUNTIME_CONTRACT_ID,
        NATIONAL_RUNTIME_MANIFEST,
        POLICY_ENTRYPOINT,
        POLICY_EPOCH_RECEIPT,
        PRECOMPUTE_ENTRYPOINT,
    )

    artifact_execution_contract = {
        "schema_version": NATIONAL_ARTIFACT_CONTRACT_SCHEMA_VERSION,
        "mode": "direct_content_bound_policy_artifact",
        "runtime_contract_id": NATIONAL_RUNTIME_CONTRACT_ID,
        "entrypoint": NATIONAL_ENTRYPOINT,
        "policy_entrypoint": POLICY_ENTRYPOINT,
        "precompute_entrypoint": PRECOMPUTE_ENTRYPOINT,
        "runtime_manifest": NATIONAL_RUNTIME_MANIFEST,
        "epoch_receipt": POLICY_EPOCH_RECEIPT,
    }
    payload = {
        "schema_version": 1,
        "authority": "precommit_eval",
        "profile_id": str(profile_id),
        "execution_mode": str(execution_mode),
        "evaluation_protocol": str(evaluation_protocol),
        "artifact_execution_contract": artifact_execution_contract,
        "semantic_files": {
            relative: _sha256(ROOT / relative) if (ROOT / relative).is_file() else "missing"
            for relative in SEMANTIC_PATHS
        },
    }
    return {**payload, "identity_digest": canonical_digest(payload)}


def _per_opponent_matches(opponent: dict[str, Any], default_matches: int) -> int:
    """Resolve one opponent's frozen match count.

    ``opponent["matches"]`` (an int >= 1, written by ``create_precommit_plan``)
    overrides the plan-wide ``matches_per_opponent`` — the pool-ratchet
    admission leg (2026-10-16 calibration) freezes a deeper sample than the
    parent/top legs.  Anything else (absent, non-int, < 1) falls back to the
    plan-wide count.
    """

    raw = opponent.get("matches")
    if type(raw) is int and raw >= 1:
        return raw
    return max(1, int(default_matches))


def build_sample_plan(
    opponents: list[dict[str, Any]],
    matches_per_opponent: int,
    *,
    deck_seed_base: int = DEFAULT_DECK_SEED_BASE,
    bot_seed_base_root: int | None = None,
    native_match_timing_plan_digest: str | None = None,
) -> list[dict[str, Any]]:
    """Freeze the per-row deck/bot seed schedule.

    ``bot_seed_base_root=None`` (every historical caller, the explicit/diagnostic
    base path, and the removed first-strict control authority) keeps the
    historical linear relation ``bot_seed == deck_seed + 1e9``.  The nonce path
    passes an INDEPENDENT root (domain-separated hash of the plan nonce —
    ``pool_ratchet_gate.derive_bot_seed_root``): the evaluated bot process
    receives its own seed via POK_NATIVE_BOT_SEED, and a linear relation to
    the deck seed would hand the candidate a complete deck-prediction oracle
    (red-team 2026-10-12).  With an independent root, observing the bot-side
    seed reveals nothing about the deck; the row expansion shape (index
    linear) is identical for both.

    Each opponent expands to its own frozen count
    (``_per_opponent_matches``): an opponent item carrying ``matches`` gets a
    deeper leg while the plan-wide count applies to the rest.
    """
    rows: list[dict[str, Any]] = []
    for opponent_index, opponent in enumerate(opponents):
        name = str(opponent.get("name") or "")
        for repeat_index in range(
            _per_opponent_matches(opponent, matches_per_opponent)
        ):
            deck_seed = (
                int(deck_seed_base)
                + opponent_index * 100_000
                + repeat_index * 1_000
            )
            bot_seed = (
                int(bot_seed_base_root)
                + opponent_index * 100_000
                + repeat_index * 1_000
                if bot_seed_base_root is not None
                else deck_seed + 1_000_000_000
            )
            row = {
                "opponent": name,
                "opponent_index": opponent_index,
                "repeat": repeat_index + 1,
                "deck_seed_base": deck_seed,
                "bot_seed_base": bot_seed,
            }
            if native_match_timing_plan_digest is not None:
                row["native_match_timing_plan_digest"] = (
                    str(native_match_timing_plan_digest)
                )
            rows.append(row)
    return rows


def build_native_precommit_batch_plan(
    sample_plan: list[dict[str, Any]],
    *,
    native_timing_plan: Any,
    first_strict_control: bool,
    bot_seed_base_root: int | None = None,
) -> dict[str, Any]:
    """Freeze the bounded execution shape for one native precommit batch.

    A native match plan explains one runner invocation; it cannot by itself
    authorize a provider session to run an arbitrary number of matches.  This
    companion plan binds the ordered seed schedule and the exact number of
    fresh samples that one provider invocation may launch.  First-strict's
    eight system-control samples deliberately advance one durable receipt at a
    time, while ordinary precommit retains its existing bounded batch shape.

    ``bot_seed_base_root`` mirrors ``build_sample_plan``: ``None`` enforces the
    historical linear bot/deck relation (first-strict + explicit-base paths);
    an independent root enforces the domain-separated expansion (nonce path —
    see the red-team seed-decoupling note in ``build_sample_plan``).
    """

    if not isinstance(sample_plan, list) or not sample_plan:
        raise PrecommitEvalContractError(
            "native precommit batch plan requires a non-empty sample plan"
        )
    if getattr(native_timing_plan, "hands", None) != 70:
        raise PrecommitEvalContractError(
            "native precommit batch plan requires a 70-hand timing plan"
        )
    timing_digest = str(native_timing_plan.digest() or "")
    if len(timing_digest) != 64:
        raise PrecommitEvalContractError(
            "native precommit batch timing plan digest is invalid"
        )
    normalized_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for index, row in enumerate(sample_plan):
        if not isinstance(row, dict):
            raise PrecommitEvalContractError(
                f"native precommit batch sample {index} is not an object"
            )
        opponent = str(row.get("opponent") or "")
        repeat = row.get("repeat")
        opponent_index = row.get("opponent_index")
        deck_seed = row.get("deck_seed_base")
        bot_seed = row.get("bot_seed_base")
        expected_bot_seed = None
        if type(deck_seed) is int and bot_seed_base_root is None:
            expected_bot_seed = deck_seed + 1_000_000_000
        elif (
            bot_seed_base_root is not None
            and type(repeat) is int
            and type(opponent_index) is int
            and opponent_index >= 0
        ):
            expected_bot_seed = (
                int(bot_seed_base_root)
                + opponent_index * 100_000
                + (repeat - 1) * 1_000
            )
        if (
            not opponent
            or type(repeat) is not int
            or repeat < 1
            or type(opponent_index) is not int
            or opponent_index < 0
            or type(deck_seed) is not int
            or type(bot_seed) is not int
            or bot_seed != expected_bot_seed
            or row.get("native_match_timing_plan_digest") != timing_digest
        ):
            raise PrecommitEvalContractError(
                f"native precommit batch sample {index} is invalid"
            )
        key = (opponent, repeat)
        if key in seen:
            raise PrecommitEvalContractError(
                "native precommit batch sample key is duplicated"
            )
        seen.add(key)
        normalized_rows.append({
            "opponent": opponent,
            "opponent_index": opponent_index,
            "repeat": repeat,
            "deck_seed_base": deck_seed,
            "bot_seed_base": bot_seed,
            "native_match_timing_plan_digest": timing_digest,
        })
    per_sample_execution_timeout_us = int(
        getattr(native_timing_plan, "execution_timeout_us", 0) or 0
    )
    per_sample_lease_timeout_us = int(
        getattr(native_timing_plan, "first_strict_lease_timeout_us", 0) or 0
    )
    if per_sample_execution_timeout_us <= 0 or per_sample_lease_timeout_us <= 0:
        raise PrecommitEvalContractError(
            "native precommit batch timing phases are invalid"
        )
    payload = {
        "schema_version": NATIVE_PRECOMMIT_BATCH_PLAN_SCHEMA_VERSION,
        "authority": "native_precommit_batch_v1",
        "timing_plan_digest": timing_digest,
        "sample_plan_digest": canonical_digest({"sample_plan": normalized_rows}),
        "sample_count": len(normalized_rows),
        # The first strict control has a journalled receipt boundary after each
        # physical match.  It must never depend on a single SDK stream lasting
        # eight full official-safe envelopes.
        "max_new_samples_per_invocation": 1 if first_strict_control else len(normalized_rows),
        "per_sample_execution_timeout_us": per_sample_execution_timeout_us,
        "per_sample_first_strict_lease_timeout_us": per_sample_lease_timeout_us,
        "batch_execution_timeout_us": (
            len(normalized_rows) * per_sample_execution_timeout_us
        ),
        "batch_effect_lease_timeout_us": (
            len(normalized_rows) * per_sample_lease_timeout_us
        ),
        "ordered_samples": normalized_rows,
    }
    return {**payload, "batch_plan_digest": canonical_digest(payload)}


def _portable_published_identity(path: Path, *, require_published: bool) -> dict[str, Any]:
    if require_published:
        identity = published_bot_identity(path)
        if not identity.get("published"):
            details = ",".join(str(item) for item in identity.get("issues", [])[:8])
            raise PrecommitEvalContractError(
                f"precommit opponent {path.name} is not an immutable published bot: {details}"
            )
        return {
            "published": True,
            **{field: identity.get(field) for field in _PUBLISHED_IDENTITY_FIELDS},
        }
    return {
        "published": False,
        "artifact_hash": hash_path(path),
    }


def _system_control_identity(
    item: dict[str, Any],
    path: Path,
    *,
    candidate_version: int,
    source_version: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    # first_strict_control module removed; the system_first_strict_control
    # opponent authority is no longer supported.  Any caller that reaches this
    # path is rejected (callers wrap the call in try/except and report it as an
    # identity error).
    raise PrecommitEvalContractError(
        "first_strict_control_removed: system_first_strict_control authority "
        "is no longer supported"
    )


def _system_control_would_be_planned(
    opponents: list[dict[str, Any]],
) -> bool:
    return any(
        str(item.get("authority") or "") == "system_first_strict_control"
        for item in opponents
        if isinstance(item, dict)
    )


def create_precommit_plan(
    *,
    candidate_version: int,
    source_version: int,
    profile_id: str,
    execution_mode: str,
    evaluation_protocol: str,
    opponents: list[dict[str, Any]],
    hands_per_match: int,
    matches_per_opponent: int,
    path_resolver: Callable[[dict[str, Any]], str | Path],
    require_published_opponents: bool,
    deck_seed_base: int | None = None,
    pool_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from bot_namespace import ARCHIVED_VERSION_HIGH_WATER

    # Deck seed provenance.  The production path (deck_seed_base=None)
    # generates a fresh one-time 128-bit nonce and derives the base through
    # the identity-bound formula in pool_ratchet_gate — the seed cannot be
    # predicted from the version number or any checkpoint field.  An explicit
    # base is the test/diagnostic path (seed_derivation='explicit_base').
    # This is enabled in BOTH publication modes: the seed fix is a
    # correctness fix, not a model switch.
    if deck_seed_base is None:
        from pool_ratchet_gate import (
            SEED_DERIVATION_NONCE,
            derive_bot_seed_root,
            derive_deck_seed_base,
            generate_deck_seed_nonce,
        )

        deck_seed_nonce = generate_deck_seed_nonce()
        effective_deck_seed_base = derive_deck_seed_base(deck_seed_nonce)
        # Independent, domain-separated bot-side root: the evaluated bot
        # process receives its own seed (POK_NATIVE_BOT_SEED) and must not be
        # able to derive the deck schedule from it (red-team 2026-10-12).
        effective_bot_seed_root = derive_bot_seed_root(deck_seed_nonce)
        seed_derivation = SEED_DERIVATION_NONCE
    else:
        deck_seed_nonce = None
        effective_deck_seed_base = int(deck_seed_base)
        effective_bot_seed_root = None
        seed_derivation = "explicit_base"
    try:
        from pool_ratchet import publish_mode as _resolve_publish_mode

        resolved_publish_mode = _resolve_publish_mode()
    except Exception as exc:
        raise PrecommitEvalContractError(
            f"precommit publish mode invalid: {type(exc).__name__}: {str(exc)[:240]}"
        ) from exc
    # The first-strict system control is the empty-pool bootstrap authority;
    # it carries no pool semantics and its plan must never freeze a pool gate.
    if pool_gate is not None and _system_control_would_be_planned(opponents):
        pool_gate = None
    if pool_gate is not None:
        from pool_ratchet_gate import validate_pool_gate_settings

        try:
            pool_gate = validate_pool_gate_settings(pool_gate)
        except ValueError as exc:
            raise PrecommitEvalContractError(
                f"precommit pool gate invalid: {exc}"
            ) from exc
    else:
        pool_gate = None

    if str(execution_mode) == "native_tcp" and int(hands_per_match) != 70:
        raise PrecommitEvalContractError(
            "native TCP precommit requires exactly 70 hands per strength sample"
        )
    if not opponents:
        raise PrecommitEvalContractError("precommit plan requires at least one opponent")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    system_control_count = 0
    for index, item in enumerate(opponents):
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            raise PrecommitEvalContractError(
                f"precommit opponent at index {index} has a missing or duplicate name"
            )
        seen.add(name)
        path = Path(path_resolver(item)).absolute()
        if not path.exists():
            raise PrecommitEvalContractError(f"precommit opponent path is missing: {path}")
        authority = str(item.get("authority") or "published_bot")
        if authority == "system_first_strict_control":
            identity, control_receipt = _system_control_identity(
                item,
                path,
                candidate_version=int(candidate_version),
                source_version=int(source_version),
            )
            system_control_count += 1
        elif authority == "published_bot":
            identity = _portable_published_identity(
                path,
                require_published=require_published_opponents,
            )
            control_receipt = None
        else:
            raise PrecommitEvalContractError(
                f"precommit opponent {name} has unsupported authority {authority!r}"
            )
        normalized_item = {
            "name": name,
            "reason": str(item.get("reason") or "precommit"),
            "path": str(path),
            "authority": authority,
            "identity": identity,
            "precommit_gate_admitted": bool(
                item.get("precommit_gate_admitted", True)
            ),
            "formal_bootstrap_opponent_admitted": bool(
                item.get("formal_bootstrap_opponent_admitted", False)
            ),
            "formal_bootstrap_scope": str(
                item.get("formal_bootstrap_scope") or ""
            ),
            "strength_admitted": bool(item.get("strength_admitted", True)),
            "rating_eligible": bool(item.get("rating_eligible", True)),
            "official_opponent_eligible": bool(
                item.get("official_opponent_eligible", True)
            ),
        }
        if control_receipt is not None:
            normalized_item["control_receipt"] = control_receipt
        normalized.append(normalized_item)

    if system_control_count:
        if system_control_count != 1 or len(normalized) != 1:
            raise PrecommitEvalContractError(
                "first-strict system control cannot be mixed with published opponents"
            )
        if int(source_version) != ARCHIVED_VERSION_HIGH_WATER:
            raise PrecommitEvalContractError(
                f"first-strict system control requires source version {ARCHIVED_VERSION_HIGH_WATER}"
            )

    from strength_order import (
        PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN,
        PRECOMMIT_AGGREGATE_MIN_SAMPLES,
        PRECOMMIT_PARENT_MAX_SCORE,
        PRECOMMIT_PARENT_MIN_SAMPLES,
    )
    if system_control_count:
        # first_strict_control module removed; use a placeholder profile id.
        CONTROL_GATE_PROFILE_ID = "first_strict_control_removed"
        # ``_system_control_identity`` has already validated the receipt back
        # to the checked-in package.  The manifest-owned gate contract, not the
        # caller/LLM's n_games suggestion, therefore fixes the execution shape.
        control_contract = dict(
            (normalized[0].get("control_receipt") or {}).get("gate_contract")
            or {}
        )
        effective_matches_per_opponent = int(control_contract["exact_samples"])
        control_min_samples = int(control_contract["minimum_samples"])
        control_min_match_score = float(
            control_contract["minimum_match_score"]
        )
    else:
        control_contract = {}
        effective_matches_per_opponent = int(matches_per_opponent)
        control_min_samples = None
        control_min_match_score = None

    # Per-opponent frozen match counts (2026-10-16 calibration): a caller may
    # freeze a deeper leg for one opponent (the pool-ratchet admission
    # opponent) by passing ``matches`` on that opponent item; every other leg
    # keeps the plan-wide count.  The system control's exact count comes from
    # its gate contract and always wins over any caller value.  This second
    # pass runs after ``effective_matches_per_opponent`` exists because the
    # control contract is read from the normalized receipt.
    for source_item, normalized_item in zip(opponents, normalized):
        normalized_item["matches"] = (
            effective_matches_per_opponent
            if normalized_item["authority"] == "system_first_strict_control"
            else _per_opponent_matches(source_item, matches_per_opponent)
        )
    matches_by_opponent = {
        str(item["name"]): int(item["matches"]) for item in normalized
    }

    native_timing_plan = None
    if str(execution_mode) == "native_tcp":
        from national_native import (
            LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            build_native_match_timing_plan,
        )

        native_timing_plan = build_native_match_timing_plan(
            hands=int(hands_per_match),
            requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
        )
    sample_plan = build_sample_plan(
        normalized,
        effective_matches_per_opponent,
        deck_seed_base=int(effective_deck_seed_base),
        bot_seed_base_root=(
            int(effective_bot_seed_root)
            if effective_bot_seed_root is not None
            else None
        ),
        native_match_timing_plan_digest=(
            native_timing_plan.digest()
            if native_timing_plan is not None
            else None
        ),
    )
    native_batch_plan = (
        build_native_precommit_batch_plan(
            sample_plan,
            native_timing_plan=native_timing_plan,
            first_strict_control=bool(system_control_count),
            bot_seed_base_root=(
                int(effective_bot_seed_root)
                if effective_bot_seed_root is not None
                else None
            ),
        )
        if native_timing_plan is not None
        else None
    )
    settings = {
        "sample_unit": "70_hand_match" if int(hands_per_match) == 70 else "national_match",
        "hands_per_match": int(hands_per_match),
        "matches_per_opponent": effective_matches_per_opponent,
        # Per-opponent frozen counts (introduced by the 2026-10-16 pool-leg
        # calibration): ``matches_per_opponent`` remains the plan-wide default
        # (parent / top legs); a deeper leg (pool-weakest admission) records
        # its exact count here and re-proves through the sample plan.
        "matches_by_opponent": matches_by_opponent,
        "deck_seed_base": int(effective_deck_seed_base),
        "deck_seed_nonce": deck_seed_nonce,
        "seed_derivation": seed_derivation,
        "publish_mode": resolved_publish_mode,
        "pool_gate": pool_gate,
        "parent_min_samples": PRECOMMIT_PARENT_MIN_SAMPLES,
        "parent_max_score": PRECOMMIT_PARENT_MAX_SCORE,
        "aggregate_min_samples": PRECOMMIT_AGGREGATE_MIN_SAMPLES,
        "aggregate_min_loss_margin": PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN,
        "gate_profile_id": (
            CONTROL_GATE_PROFILE_ID
            if system_control_count
            else "national_strength_precommit_v1"
        ),
        "strength_evidence_required": not bool(system_control_count),
        "control_min_samples": (
            control_min_samples if system_control_count else None
        ),
        "control_exact_samples": (
            effective_matches_per_opponent if system_control_count else None
        ),
        "control_min_match_score": (
            control_min_match_score if system_control_count else None
        ),
        "native_match_timing_plan": (
            native_timing_plan.snapshot() if native_timing_plan is not None else None
        ),
        "native_match_timing_plan_digest": (
            native_timing_plan.digest() if native_timing_plan is not None else None
        ),
        "native_precommit_batch_plan": native_batch_plan,
        "native_precommit_batch_plan_digest": (
            native_batch_plan.get("batch_plan_digest")
            if native_batch_plan is not None
            else None
        ),
    }
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "candidate_version": int(candidate_version),
        "source_version": int(source_version),
        "profile_id": str(profile_id),
        "execution_mode": str(execution_mode),
        "evaluation_protocol": str(evaluation_protocol),
        "require_published_opponents": bool(require_published_opponents),
        "evaluator_identity": evaluator_identity(
            profile_id=profile_id,
            execution_mode=execution_mode,
            evaluation_protocol=evaluation_protocol,
        ),
        "opponents": normalized,
        "settings": settings,
        "sample_plan": sample_plan,
    }
    return {**payload, "plan_digest": canonical_digest(payload)}


def opponents_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(item.get("name") or ""),
            "reason": str(item.get("reason") or "precommit"),
            "path": str(item.get("path") or ""),
            "authority": str(item.get("authority") or "published_bot"),
            "identity": dict(item.get("identity") or {}),
            "matches": int(item.get("matches") or 0),
            "control_receipt": dict(item.get("control_receipt") or {}),
            "precommit_gate_admitted": bool(
                item.get("precommit_gate_admitted", True)
            ),
            "formal_bootstrap_opponent_admitted": bool(
                item.get("formal_bootstrap_opponent_admitted", False)
            ),
            "formal_bootstrap_scope": str(
                item.get("formal_bootstrap_scope") or ""
            ),
            "strength_admitted": bool(item.get("strength_admitted", True)),
            "rating_eligible": bool(item.get("rating_eligible", True)),
            "official_opponent_eligible": bool(
                item.get("official_opponent_eligible", True)
            ),
        }
        for item in plan.get("opponents", [])
        if isinstance(item, dict)
    ]


def validate_precommit_plan(
    plan: dict[str, Any] | None,
    *,
    candidate_version: int,
    source_version: int,
    profile_id: str,
    execution_mode: str,
    evaluation_protocol: str,
    frozen_evaluator_identity: bool = False,
) -> list[str]:
    from bot_namespace import ARCHIVED_VERSION_HIGH_WATER

    if not isinstance(plan, dict):
        return ["precommit_plan_missing"]
    issues: list[str] = []
    unsigned = {key: value for key, value in plan.items() if key != "plan_digest"}
    if plan.get("plan_digest") != canonical_digest(unsigned):
        issues.append("precommit_plan_digest_mismatch")
    expected_scalars = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "candidate_version": int(candidate_version),
        "source_version": int(source_version),
        "profile_id": str(profile_id),
        "execution_mode": str(execution_mode),
        "evaluation_protocol": str(evaluation_protocol),
    }
    for key, expected in expected_scalars.items():
        if plan.get(key) != expected:
            issues.append(f"precommit_plan_{key}_mismatch")

    current_evaluator = evaluator_identity(
        profile_id=profile_id,
        execution_mode=execution_mode,
        evaluation_protocol=evaluation_protocol,
    )
    if plan.get("evaluator_identity") != current_evaluator:
        if not frozen_evaluator_identity:
            issues.append("precommit_evaluator_identity_drift")
        # 2026-10-18 (v566 convergence, root cause B): inside the fully
        # proven intent-bound publication window the evaluation is already
        # frozen in the published artifacts — the gate ledger must re-derive
        # its digest from the plan's OWN evaluator identity instead of the
        # live SEMANTIC_PATHS hash, or any post-publication deploy that
        # touched a semantic file permanently wedges the pre-push authority
        # (and, on a transient remote-verification failure, the resume
        # ledger branch) with precommit_evaluator_identity_drift. The window
        # predicate itself is proven by the caller; outside it this stays a
        # hard gate.

    opponents = plan.get("opponents")
    if not isinstance(opponents, list) or not opponents:
        issues.append("precommit_plan_opponents_missing")
        opponents = []
    names: set[str] = set()
    require_published = bool(plan.get("require_published_opponents"))
    system_control_count = 0
    for index, item in enumerate(opponents):
        if not isinstance(item, dict):
            issues.append(f"precommit_opponent_{index}_invalid")
            continue
        name = str(item.get("name") or "")
        if not name or name in names:
            issues.append(f"precommit_opponent_{index}_name_invalid")
        names.add(name)
        path = Path(str(item.get("path") or ""))
        if not path.exists():
            issues.append(f"precommit_opponent_{name or index}_missing")
            continue
        authority = str(item.get("authority") or "published_bot")
        try:
            if authority == "system_first_strict_control":
                current, receipt = _system_control_identity(
                    item,
                    path,
                    candidate_version=int(candidate_version),
                    source_version=int(source_version),
                )
                if item.get("control_receipt") != receipt:
                    issues.append(
                        f"precommit_opponent_{name or index}_control_receipt_drift"
                    )
                system_control_count += 1
            elif authority == "published_bot":
                if item.get("formal_bootstrap_opponent_admitted") is not False:
                    issues.append(
                        f"precommit_opponent_{name or index}_formal_bootstrap_flag_invalid"
                    )
                if str(item.get("formal_bootstrap_scope") or ""):
                    issues.append(
                        f"precommit_opponent_{name or index}_formal_bootstrap_scope_invalid"
                    )
                current = _portable_published_identity(
                    path,
                    require_published=require_published,
                )
            else:
                issues.append(
                    f"precommit_opponent_{name or index}_authority_invalid"
                )
                continue
        except Exception as exc:
            issues.append(
                f"precommit_opponent_{name or index}_identity_error:{type(exc).__name__}"
            )
            continue
        if item.get("identity") != current:
            issues.append(f"precommit_opponent_{name or index}_identity_drift")

    if system_control_count and (
        system_control_count != 1
        or len(opponents) != 1
        or int(source_version) != ARCHIVED_VERSION_HIGH_WATER
    ):
        issues.append("precommit_first_strict_control_shape_invalid")

    settings = plan.get("settings") if isinstance(plan.get("settings"), dict) else {}
    # Seed provenance, publication model, and pool-gate shape re-proofs.  The
    # nonce->base derivation lives in the identity-bound pool_ratchet_gate
    # member, so a changed derivation formula or a tampered nonce fails here
    # (and in the plan digest) instead of silently re-seeding a replay.
    seed_derivation = settings.get("seed_derivation")
    expected_bot_seed_root = None
    if seed_derivation == "nonce_sha256_v1":
        from pool_ratchet_gate import (
            derive_bot_seed_root,
            derive_deck_seed_base,
        )

        try:
            expected_seed_base = derive_deck_seed_base(
                settings.get("deck_seed_nonce")
            )
        except ValueError:
            expected_seed_base = None
        try:
            expected_bot_seed_root = derive_bot_seed_root(
                settings.get("deck_seed_nonce")
            )
        except ValueError:
            expected_bot_seed_root = None
        if (
            expected_seed_base is None
            or settings.get("deck_seed_base") != expected_seed_base
        ):
            issues.append("precommit_seed_derivation_mismatch")
    elif seed_derivation == "explicit_base":
        if settings.get("deck_seed_nonce") is not None:
            issues.append("precommit_seed_derivation_mismatch")
    else:
        issues.append("precommit_seed_derivation_invalid")
    if settings.get("publish_mode") not in ("legacy", "pool_ratchet"):
        issues.append("precommit_publish_mode_invalid")
    plan_pool_gate = settings.get("pool_gate")
    if plan_pool_gate is not None:
        from pool_ratchet_gate import validate_pool_gate_settings

        try:
            normalized_pool_gate = validate_pool_gate_settings(plan_pool_gate)
        except ValueError:
            normalized_pool_gate = None
        if normalized_pool_gate is None:
            issues.append("precommit_pool_gate_thresholds_invalid")
        else:
            if plan_pool_gate != normalized_pool_gate:
                issues.append("precommit_pool_gate_not_canonical")
            if not any(
                isinstance(item, dict)
                and str(item.get("name") or "") == normalized_pool_gate["label"]
                and str(item.get("reason") or "") == "pool_weakest_gate"
                for item in opponents
            ):
                issues.append("precommit_pool_gate_label_unbound")
    native_timing_plan = None
    if str(execution_mode) == "native_tcp":
        try:
            from national_native import (
                LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
                require_native_match_timing_plan,
            )

            native_timing_plan = require_native_match_timing_plan(
                settings.get("native_match_timing_plan"),
                hands=70,
                requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            )
            if (
                settings.get("native_match_timing_plan_digest")
                != native_timing_plan.digest()
            ):
                issues.append("precommit_native_timing_plan_digest_mismatch")
        except Exception:
            issues.append("precommit_native_timing_plan_invalid")
        from strength_order import (
            PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN,
            PRECOMMIT_AGGREGATE_MIN_SAMPLES,
            PRECOMMIT_PARENT_MAX_SCORE,
            PRECOMMIT_PARENT_MIN_SAMPLES,
        )
        if system_control_count:
            # first_strict_control module removed; placeholder profile id.
            CONTROL_GATE_PROFILE_ID = "first_strict_control_removed"
            control_contract = dict(
                ((opponents[0].get("control_receipt") or {}).get(
                    "gate_contract"
                ) or {})
            )
            control_exact_samples = int(
                control_contract.get("exact_samples") or 0
            )
            control_min_samples = int(
                control_contract.get("minimum_samples") or 0
            )
            control_min_match_score = control_contract.get(
                "minimum_match_score"
            )
        else:
            control_exact_samples = None
            control_min_samples = None
            control_min_match_score = None

        if int(settings.get("hands_per_match", 0) or 0) != 70:
            issues.append("precommit_native_hands_per_match_not_70")
        if settings.get("sample_unit") != "70_hand_match":
            issues.append("precommit_native_sample_unit_mismatch")
        expected_outcome_settings = {
            "parent_min_samples": PRECOMMIT_PARENT_MIN_SAMPLES,
            "parent_max_score": PRECOMMIT_PARENT_MAX_SCORE,
            "aggregate_min_samples": PRECOMMIT_AGGREGATE_MIN_SAMPLES,
            "aggregate_min_loss_margin": PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN,
            "gate_profile_id": (
                CONTROL_GATE_PROFILE_ID
                if system_control_count
                else "national_strength_precommit_v1"
            ),
            "strength_evidence_required": not bool(system_control_count),
            "control_min_samples": (
                control_min_samples if system_control_count else None
            ),
            "control_exact_samples": (
                control_exact_samples if system_control_count else None
            ),
            "control_min_match_score": (
                control_min_match_score if system_control_count else None
            ),
        }
        for key, expected in expected_outcome_settings.items():
            if settings.get(key) != expected:
                issues.append(f"precommit_native_{key}_mismatch")
        if system_control_count and int(
            settings.get("matches_per_opponent", 0) or 0
        ) != control_exact_samples:
            issues.append("precommit_first_strict_control_sample_count_mismatch")
    try:
        expected_samples = build_sample_plan(
            opponents,
            int(settings.get("matches_per_opponent")),
            deck_seed_base=int(settings.get("deck_seed_base")),
            bot_seed_base_root=expected_bot_seed_root,
            native_match_timing_plan_digest=(
                native_timing_plan.digest()
                if native_timing_plan is not None
                else None
            ),
        )
        expected_matches_by_opponent = {
            str(item.get("name") or ""): _per_opponent_matches(
                item, int(settings.get("matches_per_opponent"))
            )
            for item in opponents
            if isinstance(item, dict)
        }
    except Exception:
        expected_samples = []
        expected_matches_by_opponent = None
        issues.append("precommit_sample_settings_invalid")
    if plan.get("sample_plan") != expected_samples:
        issues.append("precommit_sample_plan_mismatch")
    if settings.get("matches_by_opponent") != expected_matches_by_opponent:
        issues.append("precommit_matches_by_opponent_mismatch")
    if native_timing_plan is not None:
        try:
            expected_batch_plan = build_native_precommit_batch_plan(
                expected_samples,
                native_timing_plan=native_timing_plan,
                first_strict_control=bool(system_control_count),
                bot_seed_base_root=expected_bot_seed_root,
            )
        except Exception:
            expected_batch_plan = None
            issues.append("precommit_native_batch_plan_invalid")
        if expected_batch_plan is not None:
            if settings.get("native_precommit_batch_plan") != expected_batch_plan:
                issues.append("precommit_native_batch_plan_mismatch")
            if settings.get("native_precommit_batch_plan_digest") != (
                expected_batch_plan.get("batch_plan_digest")
            ):
                issues.append("precommit_native_batch_plan_digest_mismatch")
    return list(dict.fromkeys(issues))


def build_evaluation_contract(
    plan: dict[str, Any],
    *,
    candidate_code_fingerprint: str,
) -> dict[str, Any]:
    """Bind the frozen plan to the candidate's complete artifact manifest hash.

    ``candidate_code_fingerprint`` is retained as a checkpoint-schema field
    name for compatibility; callers provide ``bot_artifact.hash_path`` through
    the shared gate fingerprint helper, not a Python-only source digest.
    """
    payload = {
        "schema_version": EVALUATION_CONTRACT_SCHEMA_VERSION,
        "precommit_plan_digest": str(plan.get("plan_digest") or ""),
        "candidate_code_fingerprint": str(candidate_code_fingerprint),
    }
    return {**payload, "contract_digest": canonical_digest(payload)}


def validate_evaluation_contract(
    contract: dict[str, Any] | None,
    plan: dict[str, Any],
    *,
    candidate_code_fingerprint: str,
) -> list[str]:
    if not isinstance(contract, dict):
        return ["precommit_evaluation_contract_missing"]
    expected = build_evaluation_contract(
        plan,
        candidate_code_fingerprint=candidate_code_fingerprint,
    )
    return [] if contract == expected else ["precommit_evaluation_contract_mismatch"]
