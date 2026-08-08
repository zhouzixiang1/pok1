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
PLAN_SCHEMA_VERSION = 7
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


def build_sample_plan(
    opponents: list[dict[str, Any]],
    matches_per_opponent: int,
    *,
    deck_seed_base: int = DEFAULT_DECK_SEED_BASE,
    native_match_timing_plan_digest: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for opponent_index, opponent in enumerate(opponents):
        name = str(opponent.get("name") or "")
        for repeat_index in range(max(1, int(matches_per_opponent))):
            deck_seed = (
                int(deck_seed_base)
                + opponent_index * 100_000
                + repeat_index * 1_000
            )
            row = {
                "opponent": name,
                "opponent_index": opponent_index,
                "repeat": repeat_index + 1,
                "deck_seed_base": deck_seed,
                "bot_seed_base": deck_seed + 1_000_000_000,
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
) -> dict[str, Any]:
    """Freeze the bounded execution shape for one native precommit batch.

    A native match plan explains one runner invocation; it cannot by itself
    authorize a provider session to run an arbitrary number of matches.  This
    companion plan binds the ordered seed schedule and the exact number of
    fresh samples that one provider invocation may launch.  First-strict's
    eight system-control samples deliberately advance one durable receipt at a
    time, while ordinary precommit retains its existing bounded batch shape.
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
        if (
            not opponent
            or type(repeat) is not int
            or repeat < 1
            or type(opponent_index) is not int
            or opponent_index < 0
            or type(deck_seed) is not int
            or type(bot_seed) is not int
            or bot_seed != deck_seed + 1_000_000_000
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
    deck_seed_base: int = DEFAULT_DECK_SEED_BASE,
) -> dict[str, Any]:
    from bot_namespace import ARCHIVED_VERSION_HIGH_WATER

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
        deck_seed_base=int(deck_seed_base),
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
        )
        if native_timing_plan is not None
        else None
    )
    settings = {
        "sample_unit": "70_hand_match" if int(hands_per_match) == 70 else "national_match",
        "hands_per_match": int(hands_per_match),
        "matches_per_opponent": effective_matches_per_opponent,
        "deck_seed_base": int(deck_seed_base),
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
        issues.append("precommit_evaluator_identity_drift")

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
            native_match_timing_plan_digest=(
                native_timing_plan.digest()
                if native_timing_plan is not None
                else None
            ),
        )
    except Exception:
        expected_samples = []
        issues.append("precommit_sample_settings_invalid")
    if plan.get("sample_plan") != expected_samples:
        issues.append("precommit_sample_plan_mismatch")
    if native_timing_plan is not None:
        try:
            expected_batch_plan = build_native_precommit_batch_plan(
                expected_samples,
                native_timing_plan=native_timing_plan,
                first_strict_control=bool(system_control_count),
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
