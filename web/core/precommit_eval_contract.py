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
PLAN_SCHEMA_VERSION = 1
EVALUATION_CONTRACT_SCHEMA_VERSION = 1
DEFAULT_DECK_SEED_BASE = 91_000

SEMANTIC_PATHS = (
    "sever/engine/deck.py",
    "sever/engine/evaluator.py",
    "sever/engine/game.py",
    "sever/engine/validator.py",
    "sever/server/protocol.py",
    "web/core/eval_stats.py",
    "web/core/national_bot_launcher.py",
    "web/core/national_game_runtime.py",
    "web/core/national_native.py",
    "web/core/national_transport.py",
    "web/core/precommit_eval_contract.py",
    "web/core/tool_eval.py",
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
    payload = {
        "schema_version": 1,
        "authority": "precommit_eval",
        "profile_id": str(profile_id),
        "execution_mode": str(execution_mode),
        "evaluation_protocol": str(evaluation_protocol),
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
            rows.append({
                "opponent": name,
                "opponent_index": opponent_index,
                "repeat": repeat_index + 1,
                "deck_seed_base": deck_seed,
                "bot_seed_base": deck_seed + 1_000_000_000,
            })
    return rows


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
    parent_loss_threshold: float,
    aggregate_loss_threshold: float,
    path_resolver: Callable[[dict[str, Any]], str | Path],
    require_published_opponents: bool,
    deck_seed_base: int = DEFAULT_DECK_SEED_BASE,
) -> dict[str, Any]:
    if not opponents:
        raise PrecommitEvalContractError("precommit plan requires at least one opponent")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
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
        normalized.append({
            "name": name,
            "reason": str(item.get("reason") or "precommit"),
            "path": str(path),
            "identity": _portable_published_identity(
                path,
                require_published=require_published_opponents,
            ),
        })

    settings = {
        "sample_unit": "70_hand_match" if int(hands_per_match) == 70 else "national_match",
        "hands_per_match": int(hands_per_match),
        "matches_per_opponent": int(matches_per_opponent),
        "deck_seed_base": int(deck_seed_base),
        "parent_loss_threshold": float(parent_loss_threshold),
        "aggregate_loss_threshold": float(aggregate_loss_threshold),
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
        "sample_plan": build_sample_plan(
            normalized,
            int(matches_per_opponent),
            deck_seed_base=int(deck_seed_base),
        ),
    }
    return {**payload, "plan_digest": canonical_digest(payload)}


def opponents_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(item.get("name") or ""),
            "reason": str(item.get("reason") or "precommit"),
            "path": str(item.get("path") or ""),
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
        try:
            current = _portable_published_identity(
                path,
                require_published=require_published,
            )
        except Exception as exc:
            issues.append(
                f"precommit_opponent_{name or index}_identity_error:{type(exc).__name__}"
            )
            continue
        if item.get("identity") != current:
            issues.append(f"precommit_opponent_{name or index}_identity_drift")

    settings = plan.get("settings") if isinstance(plan.get("settings"), dict) else {}
    try:
        expected_samples = build_sample_plan(
            opponents,
            int(settings.get("matches_per_opponent")),
            deck_seed_base=int(settings.get("deck_seed_base")),
        )
    except Exception:
        expected_samples = []
        issues.append("precommit_sample_settings_invalid")
    if plan.get("sample_plan") != expected_samples:
        issues.append("precommit_sample_plan_mismatch")
    return list(dict.fromkeys(issues))


def build_evaluation_contract(
    plan: dict[str, Any],
    *,
    candidate_code_fingerprint: str,
) -> dict[str, Any]:
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
