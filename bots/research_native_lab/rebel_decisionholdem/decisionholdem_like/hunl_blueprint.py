"""Strict content-bound sparse HUNL blueprint artifact for route A2."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...common_contracts.constants import (
    BIG_BLIND,
    CONTRACT_VERSION,
    INITIAL_CHIPS,
    SMALL_BLIND,
)
from .hunl_abstraction import (
    HUNL_ACTION_IDS,
    HUNL_INFOSET_VERSION,
    HUNLInformationAbstraction,
    abstraction_contract,
    parse_infoset_key,
)
from .hunl_external_sampling import (
    HUNL_LCFR_ALGORITHM,
    HUNLExternalSamplingLCFR,
    HUNLTrainingConfig,
    strict_json_loads,
    training_identity_digest,
)
from .secure_files import (
    atomic_json_write,
    canonical_bytes,
    secure_file_map,
    stable_read_path,
    stable_selected_file_map,
)


HUNL_BLUEPRINT_SCHEMA = "route-a2-hunl-sparse-blueprint-v6"
HUNL_BLUEPRINT_FIDELITY = {
    "claim": "low-budget-sampled-hunl-blueprint-smoke-not-strength-frozen",
    "decisionholdem_reproduction": False,
    "full_game": "real-common-hunl-rules-and-four-street-sampled-trajectories",
    "online_resolve_or_search": False,
    "opponent_specific_heuristic": False,
    "trained_hierarchical_backoff": True,
}
HUNL_TRAINED_BACKOFF_VERSION = "route-a2-hunl-trained-backoff-v2"
HUNL_MATERIAL_POLICY_L1_THRESHOLD = 1e-6
HUNL_TRAINED_BACKOFF_LEVELS = (
    (
        "public_action_context",
        ("street", "position", "pot", "spr", "to_call", "raises", "legal"),
    ),
    ("street_position_legal", ("street", "position", "legal")),
    ("legal_signature", ("legal",)),
)
HUNL_TRAINED_BACKOFF_CONTRACT = {
    "aggregation": (
        "sum LCFR linear-weighted simple sampled average-strategy mass by "
        "canonical backoff key and action, then normalize"
    ),
    "hierarchy": [
        {"fields": list(fields), "level": level}
        for level, fields in HUNL_TRAINED_BACKOFF_LEVELS
    ],
    "mode": HUNL_TRAINED_BACKOFF_VERSION,
    "material_nonuniform_l1_threshold": HUNL_MATERIAL_POLICY_L1_THRESHOLD,
    "opponent_specific": False,
    "selection": "first matching trained level after exact row",
    "smoke_deck_specific": False,
}
HUNL_FALLBACK_CONTRACT = {
    "mode": "artifact-bound-uniform-emergency-current-legal-signature-v2",
    "purpose": "total coverage only after exact and trained-derived lookup miss",
    "opponent_specific": False,
    "smoke_deck_specific": False,
}
HUNL_RULES_CONTRACT = {
    "big_blind": BIG_BLIND,
    "initial_chips": INITIAL_CHIPS,
    "players": 2,
    "small_blind": SMALL_BLIND,
    "streets": ["preflop", "flop", "turn", "river"],
}
HUNL_ALGORITHM_CONTRACT = {
    "average_strategy": (
        "linear-weighted-simple-sampled-average-on-opponent-traversal"
    ),
    "chance_sampling": "one-counter-based-exact-52-card-deal-per-iteration",
    "name": HUNL_LCFR_ALGORITHM,
    "opponent_sampling": "external-sampling-from-current-regret-matched-policy",
    "parallel_checkpoint_segment_merge": False,
    "regret_weight": "global-iteration-index",
    "resume_unit": "sequential-digest-bound-checkpoint-segment",
    "trained_backoff_policy": (
        "normalize-strategy-sums-aggregated-by-fixed-hierarchy"
    ),
    "traversals_per_iteration": 2,
}

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
COMMON_ROOT = PACKAGE_ROOT.parent / "common_contracts"
SOURCE_FILES = (
    "__init__.py",
    "decisionholdem_like/__init__.py",
    "decisionholdem_like/common_native_entry.py",
    "decisionholdem_like/hunl_abstraction.py",
    "decisionholdem_like/hunl_blueprint.py",
    "decisionholdem_like/hunl_common_adapter.py",
    "decisionholdem_like/hunl_external_sampling.py",
    "decisionholdem_like/hunl_tcp_client.py",
    "decisionholdem_like/secure_files.py",
    "tools/__init__.py",
    "tools/train_hunl_blueprint.py",
)
_HEX_40 = re.compile(r"[0-9a-f]{40}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_IGNORED_PARTS = {"__pycache__", ".pytest_cache", "data", "results", "checkpoints"}


def _canonical_bytes(value: object) -> bytes:
    return canonical_bytes(value)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, payload: object) -> None:
    atomic_json_write(path, payload)


def _file_map_digest(files: Mapping[str, str]) -> str:
    return _sha256_bytes(_canonical_bytes(dict(files)))


def common_package_snapshot() -> dict[str, object]:
    files = secure_file_map(COMMON_ROOT, ignored_directories=_IGNORED_PARTS)
    return {
        "contract_version": CONTRACT_VERSION,
        "file_count": len(files),
        "tree_sha256": _file_map_digest(files),
    }


def route_source_snapshot(source_commit: str) -> dict[str, object]:
    if type(source_commit) is not str or _HEX_40.fullmatch(source_commit) is None:
        raise ValueError("source_commit must be a lowercase 40-character Git SHA")
    files = stable_selected_file_map(PACKAGE_ROOT, SOURCE_FILES)
    return {
        "base_commit": source_commit,
        "files": files,
        "tree_sha256": _file_map_digest(files),
    }


def _exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _validate_probability_row(
    legal: Sequence[str],
    raw_row: object,
) -> dict[str, float]:
    actions = tuple(legal)
    if not actions or len(set(actions)) != len(actions):
        raise ValueError("blueprint probability row has an invalid legal signature")
    if not isinstance(raw_row, dict) or set(raw_row) != set(actions):
        raise ValueError("blueprint row does not match its legal action signature")
    row: dict[str, float] = {}
    for action in actions:
        value = raw_row[action]
        if type(value) not in (int, float):
            raise ValueError("blueprint probability must be numeric")
        probability = float(value)
        if not math.isfinite(probability) or probability < 0.0:
            raise ValueError("blueprint probability must be finite and nonnegative")
        row[action] = probability
    if abs(sum(row.values()) - 1.0) > 1e-12:
        raise ValueError("blueprint probability row must sum to one")
    return row


def policy_l1_from_uniform(probabilities: Mapping[str, float]) -> float:
    if not isinstance(probabilities, Mapping) or not probabilities:
        raise ValueError("policy must be a non-empty mapping")
    values: list[float] = []
    for value in probabilities.values():
        if type(value) not in (int, float):
            raise ValueError("policy probability must be numeric")
        probability = float(value)
        if not math.isfinite(probability) or probability < 0.0:
            raise ValueError("policy probability must be finite and nonnegative")
        values.append(probability)
    if abs(sum(values) - 1.0) > 1e-12:
        raise ValueError("policy probability mass must sum to one")
    uniform = 1.0 / len(values)
    return sum(abs(probability - uniform) for probability in values)


def _backoff_spec(level: str) -> tuple[str, ...]:
    for candidate, fields in HUNL_TRAINED_BACKOFF_LEVELS:
        if level == candidate:
            return fields
    raise ValueError("unknown HUNL trained-backoff level")


def _backoff_key_from_infoset_payload(
    infoset: Mapping[str, Any],
    level: str,
) -> str:
    fields = _backoff_spec(level)
    context = {field: infoset[field] for field in fields}
    payload = {
        "context": context,
        "level": level,
        "version": HUNL_TRAINED_BACKOFF_VERSION,
    }
    return HUNL_TRAINED_BACKOFF_VERSION + "|" + json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )


def trained_backoff_key(
    abstraction: HUNLInformationAbstraction,
    level: str,
) -> str:
    if type(abstraction) is not HUNLInformationAbstraction:
        raise TypeError("abstraction must be an exact HUNLInformationAbstraction")
    return _backoff_key_from_infoset_payload(parse_infoset_key(abstraction.key), level)


def parse_trained_backoff_key(key: str) -> dict[str, Any]:
    prefix = HUNL_TRAINED_BACKOFF_VERSION + "|"
    if type(key) is not str or not key.startswith(prefix):
        raise ValueError("HUNL trained-backoff key has the wrong version prefix")
    try:
        payload = strict_json_loads(key[len(prefix) :])
    except ValueError as exc:
        raise ValueError("HUNL trained-backoff key is not strict JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "context",
        "level",
        "version",
    }:
        raise ValueError("HUNL trained-backoff key fields are invalid")
    if payload["version"] != HUNL_TRAINED_BACKOFF_VERSION:
        raise ValueError("HUNL trained-backoff key version mismatch")
    level = payload["level"]
    if type(level) is not str:
        raise ValueError("HUNL trained-backoff level must be a string")
    fields = _backoff_spec(level)
    context = payload["context"]
    if not isinstance(context, dict) or set(context) != set(fields):
        raise ValueError("HUNL trained-backoff context fields are invalid")
    if any(
        type(value) is not str
        for field, value in context.items()
        if field != "legal"
    ):
        raise ValueError("HUNL trained-backoff scalar fields must be strings")
    legal = context.get("legal")
    if (
        not isinstance(legal, list)
        or not legal
        or any(type(action) is not str or action not in HUNL_ACTION_IDS for action in legal)
        or len(set(legal)) != len(legal)
    ):
        raise ValueError("HUNL trained-backoff legal signature is invalid")
    probe = {
        "action_recall": [],
        "betting_line": "root",
        "card_bucket": "trained_backoff_validation",
        "legal": legal,
        "observation_recall": [],
        "position": "sb",
        "pot": "p2",
        "raises": "0",
        "spr": "spr16plus",
        "street": "preflop",
        "to_call": "none",
        "version": HUNL_INFOSET_VERSION,
    }
    probe.update(context)
    street_sequence = ("preflop", "flop", "turn", "river")
    probe["observation_recall"] = [
        {"card_bucket": f"validation:{street}", "street": street}
        for street in street_sequence[: street_sequence.index(probe["street"]) + 1]
    ]
    parse_infoset_key(
        HUNL_INFOSET_VERSION + "|"
        + json.dumps(probe, sort_keys=True, separators=(",", ":"))
    )
    if key != _backoff_key_from_infoset_payload(context, level):
        raise ValueError("HUNL trained-backoff key is not canonical")
    return payload


def build_trained_backoff_policies(
    trainer: HUNLExternalSamplingLCFR,
) -> dict[str, dict[str, dict[str, float]]]:
    """Aggregate LCFR average-strategy mass by fixed hierarchy; never smoke states."""

    if type(trainer) is not HUNLExternalSamplingLCFR:
        raise TypeError("trainer must be the exact HUNLExternalSamplingLCFR type")
    accumulators: dict[str, dict[str, dict[str, float]]] = {
        level: {} for level, _ in HUNL_TRAINED_BACKOFF_LEVELS
    }
    for infoset_key in sorted(trainer.strategy_sums):
        infoset = parse_infoset_key(infoset_key)
        legal = tuple(infoset["legal"])
        source = trainer.strategy_sums[infoset_key]
        if set(source) != set(legal):
            raise ValueError("LCFR strategy-sum action signature drifted")
        values: dict[str, float] = {}
        for action in legal:
            raw = source[action]
            if type(raw) not in (int, float):
                raise ValueError("LCFR strategy sum must be numeric")
            value = float(raw)
            if not math.isfinite(value) or value < 0.0:
                raise ValueError("LCFR strategy sum must be finite and nonnegative")
            values[action] = value
        if sum(values.values()) <= 0.0:
            continue
        for level, _ in HUNL_TRAINED_BACKOFF_LEVELS:
            key = _backoff_key_from_infoset_payload(infoset, level)
            row = accumulators[level].setdefault(
                key, {action: 0.0 for action in legal}
            )
            if set(row) != set(legal):
                raise AssertionError("trained-backoff grouping crossed legal signatures")
            for action, value in values.items():
                row[action] += value

    result: dict[str, dict[str, dict[str, float]]] = {}
    for level, _ in HUNL_TRAINED_BACKOFF_LEVELS:
        table: dict[str, dict[str, float]] = {}
        for key, values in sorted(accumulators[level].items()):
            total = sum(values.values())
            if not math.isfinite(total) or total <= 0.0:
                raise ValueError("trained-backoff aggregate has no positive strategy mass")
            table[key] = {action: value / total for action, value in values.items()}
        if not table:
            raise ValueError(f"trained-backoff level {level} has no policy rows")
        result[level] = table
    return result


@dataclass(frozen=True, slots=True)
class HUNLPolicyLookup:
    requested_key: str
    matched_key: str | None
    source: str
    probabilities: Mapping[str, float]


class HUNLBlueprint:
    """Validated exact/coarse trained policy plus uniform emergency coverage."""

    def __init__(self, payload: object):
        if not isinstance(payload, dict) or set(payload) != {
            "body",
            "body_sha256",
            "schema",
        }:
            raise ValueError("HUNL blueprint wrapper fields are invalid")
        if payload["schema"] != HUNL_BLUEPRINT_SCHEMA:
            raise ValueError("HUNL blueprint schema mismatch")
        body = payload["body"]
        if not isinstance(body, dict) or set(body) != {
            "abstraction",
            "algorithm",
            "backoff",
            "common",
            "fallback",
            "fidelity",
            "policies",
            "rules",
            "source",
            "trained_backoff_policies",
            "training",
        }:
            raise ValueError("HUNL blueprint body fields are invalid")
        digest = payload["body_sha256"]
        if (
            type(digest) is not str
            or _HEX_64.fullmatch(digest) is None
            or digest != _sha256_bytes(_canonical_bytes(body))
        ):
            raise ValueError("HUNL blueprint content hash mismatch")
        if body["abstraction"] != abstraction_contract():
            raise ValueError("HUNL blueprint abstraction contract mismatch")
        if body["algorithm"] != HUNL_ALGORITHM_CONTRACT:
            raise ValueError("HUNL blueprint algorithm contract mismatch")
        if body["backoff"] != HUNL_TRAINED_BACKOFF_CONTRACT:
            raise ValueError("HUNL blueprint trained-backoff contract mismatch")
        if body["fallback"] != HUNL_FALLBACK_CONTRACT:
            raise ValueError("HUNL blueprint fallback contract mismatch")
        if body["fidelity"] != HUNL_BLUEPRINT_FIDELITY:
            raise ValueError("HUNL blueprint fidelity boundary mismatch")
        if body["rules"] != HUNL_RULES_CONTRACT:
            raise ValueError("HUNL blueprint rules contract mismatch")
        if body["common"] != common_package_snapshot():
            raise ValueError("HUNL blueprint Common package binding mismatch")
        source = body["source"]
        if not isinstance(source, dict) or set(source) != {
            "base_commit",
            "files",
            "tree_sha256",
        }:
            raise ValueError("HUNL blueprint source binding fields are invalid")
        if source != route_source_snapshot(source["base_commit"]):
            raise ValueError("HUNL blueprint route source binding mismatch")
        training = body["training"]
        if not isinstance(training, dict) or set(training) != {
            "checkpoint_sha256",
            "checkpoint_training_identity_sha256",
            "config",
            "iterations_completed",
            "nodes_visited",
            "policy_rows",
            "sampled_deals",
            "traversals_completed",
        }:
            raise ValueError("HUNL blueprint training metadata fields are invalid")
        HUNLTrainingConfig.from_dict(training["config"])
        iterations = _exact_int(
            training["iterations_completed"], "iterations_completed", minimum=1
        )
        traversals = _exact_int(
            training["traversals_completed"], "traversals_completed", minimum=2
        )
        sampled = _exact_int(training["sampled_deals"], "sampled_deals", minimum=1)
        _exact_int(training["nodes_visited"], "nodes_visited", minimum=1)
        policy_rows = _exact_int(training["policy_rows"], "policy_rows", minimum=1)
        checkpoint_digest = training["checkpoint_sha256"]
        if type(checkpoint_digest) is not str or _HEX_64.fullmatch(checkpoint_digest) is None:
            raise ValueError("HUNL blueprint checkpoint digest is invalid")
        identity_digest = training["checkpoint_training_identity_sha256"]
        if (
            type(identity_digest) is not str
            or _HEX_64.fullmatch(identity_digest) is None
            or identity_digest != training_identity_digest()
        ):
            raise ValueError("HUNL blueprint checkpoint training identity is invalid")
        if traversals != iterations * 2 or sampled != iterations:
            raise ValueError("HUNL blueprint training counters are inconsistent")
        raw_policies = body["policies"]
        if not isinstance(raw_policies, dict) or len(raw_policies) != policy_rows:
            raise ValueError("HUNL blueprint policy row count mismatch")
        policies = {
            key: _validate_probability_row(
                tuple(parse_infoset_key(key)["legal"]), row
            )
            for key, row in raw_policies.items()
        }
        raw_backoff = body["trained_backoff_policies"]
        expected_levels = {level for level, _ in HUNL_TRAINED_BACKOFF_LEVELS}
        if not isinstance(raw_backoff, dict) or set(raw_backoff) != expected_levels:
            raise ValueError("HUNL trained-backoff tables are invalid")
        backoff_policies: dict[str, dict[str, dict[str, float]]] = {}
        for level, _ in HUNL_TRAINED_BACKOFF_LEVELS:
            raw_table = raw_backoff[level]
            if not isinstance(raw_table, dict) or not raw_table:
                raise ValueError("HUNL trained-backoff table must be non-empty")
            table: dict[str, dict[str, float]] = {}
            for key, raw_row in raw_table.items():
                parsed = parse_trained_backoff_key(key)
                if parsed["level"] != level:
                    raise ValueError("HUNL trained-backoff row is in the wrong level")
                table[key] = _validate_probability_row(
                    tuple(parsed["context"]["legal"]), raw_row
                )
            backoff_policies[level] = table
        canonical_payload = strict_json_loads(
            json.dumps(payload, sort_keys=True, separators=(",", ":"))
        )
        self.payload = canonical_payload
        self.body = canonical_payload["body"]
        self.digest = digest
        self.policies = policies
        self.trained_backoff_policies = backoff_policies
        self.training = dict(training)

    @classmethod
    def load(cls, path: str | Path) -> "HUNLBlueprint":
        return cls(strict_json_loads(stable_read_path(path)))

    def save(self, path: str | Path) -> None:
        _atomic_write(Path(path), self.payload)

    def lookup(
        self,
        abstraction: HUNLInformationAbstraction,
        action_ids: Sequence[str],
    ) -> HUNLPolicyLookup:
        if type(abstraction) is not HUNLInformationAbstraction:
            raise TypeError("abstraction must be an exact HUNLInformationAbstraction")
        actions = tuple(action_ids)
        if actions != abstraction.legal_signature:
            raise ValueError("runtime action signature differs from its abstraction key")
        if not actions or len(set(actions)) != len(actions):
            raise ValueError("runtime action signature must be non-empty and unique")
        if any(action not in HUNL_ACTION_IDS for action in actions):
            raise ValueError("runtime action signature contains an unknown action")
        row = self.policies.get(abstraction.key)
        if row is not None:
            if set(row) != set(actions):
                raise ValueError("matched blueprint row legality drifted")
            return HUNLPolicyLookup(
                abstraction.key,
                abstraction.key,
                "trained_exact_row",
                dict(row),
            )
        for level, _ in HUNL_TRAINED_BACKOFF_LEVELS:
            key = trained_backoff_key(abstraction, level)
            row = self.trained_backoff_policies[level].get(key)
            if row is not None:
                if set(row) != set(actions):
                    raise ValueError("matched trained-backoff row legality drifted")
                return HUNLPolicyLookup(
                    abstraction.key,
                    key,
                    f"trained_backoff_{level}",
                    dict(row),
                )
        probability = 1.0 / len(actions)
        return HUNLPolicyLookup(
            abstraction.key,
            None,
            HUNL_FALLBACK_CONTRACT["mode"],
            {action: probability for action in actions},
        )


def build_hunl_blueprint_payload(
    trainer: HUNLExternalSamplingLCFR,
    *,
    source_commit: str,
) -> dict[str, object]:
    if type(trainer) is not HUNLExternalSamplingLCFR:
        raise TypeError("trainer must be the exact HUNLExternalSamplingLCFR type")
    if trainer.iterations_completed < 1:
        raise ValueError("cannot export an untrained HUNL blueprint")
    policies = trainer.average_strategy()
    trained_backoff_policies = build_trained_backoff_policies(trainer)
    body = {
        "abstraction": abstraction_contract(),
        "algorithm": HUNL_ALGORITHM_CONTRACT,
        "backoff": HUNL_TRAINED_BACKOFF_CONTRACT,
        "common": common_package_snapshot(),
        "fallback": HUNL_FALLBACK_CONTRACT,
        "fidelity": HUNL_BLUEPRINT_FIDELITY,
        "policies": policies,
        "rules": HUNL_RULES_CONTRACT,
        "source": route_source_snapshot(source_commit),
        "trained_backoff_policies": trained_backoff_policies,
        "training": {
            "checkpoint_sha256": trainer.checkpoint_digest(),
            "checkpoint_training_identity_sha256": training_identity_digest(),
            "config": trainer.config.to_dict(),
            "iterations_completed": trainer.iterations_completed,
            "nodes_visited": trainer.nodes_visited,
            "policy_rows": len(policies),
            "sampled_deals": trainer.sampled_deals,
            "traversals_completed": trainer.traversals_completed,
        },
    }
    payload = {
        "body": body,
        "body_sha256": _sha256_bytes(_canonical_bytes(body)),
        "schema": HUNL_BLUEPRINT_SCHEMA,
    }
    HUNLBlueprint(payload)
    return payload


def save_hunl_blueprint(path: str | Path, payload: object) -> None:
    blueprint = HUNLBlueprint(payload)
    blueprint.save(path)
