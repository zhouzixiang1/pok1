#!/usr/bin/env python3
"""Analyze structured native-TCP decision traces from neural lab evaluations.

The input is the JSON produced by ``native_tcp_evaluate.py --trace-decisions``.
This tool stays on the native national TCP path: it reads the candidate bot's
own trace rows and optionally recomputes that version's neural-policy scores.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import importlib
import json
from dataclasses import dataclass
from pathlib import Path
import statistics
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
MODULE_PREFIXES = {
    "main",
    "strategy",
    "state",
    "neural_policy",
    "neural_features",
    "card_utils",
    "constants",
    "opponent",
    "simulation",
    "postflop",
    "tournament",
    "strategy_helpers",
    "line_reading",
    "overbet",
    "donk_probe",
    "passive_exploit",
    "reachability_test",
}


@dataclass
class LoadedBot:
    path: Path
    neural: Any | None
    features: Any | None


def _resolve(path: str | Path) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def _forget_bot_modules() -> None:
    for name in list(sys.modules):
        if name.split(".", 1)[0] in MODULE_PREFIXES:
            del sys.modules[name]


def _load_bot(version_dir: Path) -> LoadedBot:
    _forget_bot_modules()
    sys.path.insert(0, str(version_dir))
    try:
        try:
            neural = importlib.import_module("neural_policy")
        except Exception as exc:
            print(f"TRACE_ANALYZER_IMPORT_NEURAL_ERROR {version_dir}: {exc}", file=sys.stderr)
            neural = None
        try:
            features = importlib.import_module("neural_features")
        except Exception as exc:
            print(f"TRACE_ANALYZER_IMPORT_FEATURES_ERROR {version_dir}: {exc}", file=sys.stderr)
            features = None
    finally:
        try:
            sys.path.remove(str(version_dir))
        except ValueError:
            pass
    return LoadedBot(path=version_dir, neural=neural, features=features)


def _label_name(bot: LoadedBot, label: int | None) -> str:
    labels = getattr(bot.neural, "LABELS", None) or getattr(bot.features, "LABELS", None) or ()
    if label is None:
        return "unknown"
    try:
        return str(labels[int(label)])
    except Exception:
        return f"label_{label}"


def _rule_label(bot: LoadedBot, action: int) -> int | None:
    neural = bot.neural
    if neural is not None and hasattr(neural, "_rule_label"):
        try:
            return int(neural._rule_label(int(action)))
        except Exception:
            return None
    if action == -1:
        return 0
    if action == -2:
        return 5
    if action == 0:
        return 1
    return 3


def _action_label(bot: LoadedBot, action: int, req: dict[str, Any]) -> int | None:
    features = bot.features
    if features is not None and hasattr(features, "label_action"):
        try:
            return int(features.label_action(int(action), req, None))
        except Exception:
            pass
    return _rule_label(bot, action)


def _round(value: Any, digits: int = 5) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _hand_outcome(trace: dict[str, Any], hand_net_chips: list[Any]) -> int | None:
    try:
        hand = int(trace.get("hand", 0) or 0)
    except (TypeError, ValueError):
        return None
    if 1 <= hand <= len(hand_net_chips):
        try:
            return int(hand_net_chips[hand - 1])
        except (TypeError, ValueError):
            return None
    return None


def _stats(values: list[int | float]) -> dict[str, Any]:
    clean = [float(v) for v in values]
    if not clean:
        return {"count": 0, "sum": 0, "mean": 0.0, "median": 0.0}
    return {
        "count": len(clean),
        "sum": round(sum(clean), 3),
        "mean": round(statistics.mean(clean), 3),
        "median": round(statistics.median(clean), 3),
        "min": round(min(clean), 3),
        "max": round(max(clean), 3),
    }


def _neural_snapshot(bot: LoadedBot, trace: dict[str, Any]) -> dict[str, Any]:
    neural = bot.neural
    if neural is None:
        return {"available": False}
    req = trace.get("request") if isinstance(trace.get("request"), dict) else {}
    state = trace.get("state") if isinstance(trace.get("state"), dict) else {}
    rule_action = _as_int(trace.get("rule_action"), 0)
    try:
        feature_req = dict(req)
        for key in ("pot", "to_call", "my_stage_bet", "opponent_stage_bet", "opponent_allin"):
            if key in state:
                feature_req[key] = state[key]
        model = neural._model() if hasattr(neural, "_model") else None
        if model is None:
            return {"available": False, "reason": "no_policy_model"}
        probs = neural._predict(model, neural.encode_features(feature_req, None))
        legal = neural._legal_mask(req, state)
        policy_label, policy_conf, masked_probs = neural._masked_top(probs, legal)
        policy_label = int(policy_label)
        rule_label = _rule_label(bot, rule_action)
        out: dict[str, Any] = {
            "available": True,
            "policy_label": policy_label,
            "policy_label_name": _label_name(bot, policy_label),
            "policy_conf": _round(policy_conf),
            "raw_probs": [_round(value) for value in probs],
            "masked_probs": [_round(value) for value in masked_probs],
            "legal_mask": [int(v) for v in legal],
            "rule_policy_label": rule_label,
            "rule_policy_label_name": _label_name(bot, rule_label),
        }
        candidate = neural._candidate_action(policy_label, req, state)
        out["policy_candidate_action"] = int(candidate)
        if hasattr(neural, "_passes_runtime_gate"):
            out["policy_runtime_gate"] = bool(
                neural._passes_runtime_gate(policy_label, policy_conf, masked_probs, req, state, rule_action, candidate)
            )
        if hasattr(neural, "_multi_action_value_model"):
            value_model = neural._multi_action_value_model()
        else:
            value_model = None
        if value_model is not None:
            features = neural._multi_action_value_features(req, state, rule_action, policy_label, policy_conf, probs)
            if len(features) == int(value_model.get("input_dim", 0)):
                values = neural._predict_multi_action_values(value_model, features)
                out["multi_action_values"] = [_round(value) for value in values]
                if rule_label is not None and 0 <= rule_label < len(values):
                    rule_value = float(values[rule_label])
                    out["rule_value"] = _round(rule_value)
                    final_label = _action_label(bot, _as_int(trace.get("final_action"), rule_action), req)
                    advised_label = _action_label(bot, _as_int(trace.get("advised_action"), rule_action), req)
                    out["final_value_delta_vs_rule"] = (
                        _round(float(values[final_label]) - rule_value)
                        if final_label is not None and 0 <= final_label < len(values)
                        else None
                    )
                    out["advised_value_delta_vs_rule"] = (
                        _round(float(values[advised_label]) - rule_value)
                        if advised_label is not None and 0 <= advised_label < len(values)
                        else None
                    )
                    best_label = max(range(len(values)), key=lambda idx: float(values[idx]))
                    out["best_value_label"] = int(best_label)
                    out["best_value_label_name"] = _label_name(bot, best_label)
                    out["best_value_delta_vs_rule"] = _round(float(values[best_label]) - rule_value)
                if hasattr(neural, "_multi_action_value_proposal"):
                    proposal = neural._multi_action_value_proposal(
                        req, state, rule_action, probs, masked_probs, policy_label, policy_conf
                    )
                    out["multi_action_proposal"] = int(proposal) if proposal is not None else None
                    out["multi_action_proposal_label"] = (
                        _label_name(bot, _action_label(bot, int(proposal), req)) if proposal is not None else None
                    )
            else:
                out["multi_action_error"] = {
                    "feature_dim": len(features),
                    "model_input_dim": int(value_model.get("input_dim", 0)),
                }
        return out
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _iter_trace_sources(payload: dict[str, Any], file_path: Path):
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_rows = row.get("legs") if isinstance(row.get("legs"), list) else [row]
        for leg in source_rows:
            if not isinstance(leg, dict):
                continue
            native = leg.get("candidate_native") if isinstance(leg.get("candidate_native"), dict) else {}
            trace = native.get("decision_trace") if isinstance(native.get("decision_trace"), list) else []
            yield {
                "file": file_path,
                "payload_candidate_path": payload.get("candidate_path", ""),
                "candidate": leg.get("candidate", row.get("candidate", "")),
                "opponent": leg.get("opponent", row.get("opponent", "")),
                "opponent_path": leg.get("opponent_path", row.get("opponent_path", "")),
                "match_idx": leg.get("match_idx", row.get("match_idx", 0)),
                "leg": leg.get("leg", row.get("leg", "")),
                "net_chips": leg.get("net_chips", 0),
                "hand_net_chips": leg.get("hand_net_chips", []),
                "trace": trace,
            }


def _add_counter(counter: Counter[str], key: Any) -> None:
    counter[str(key)] += 1


def _sorted_counter(counter: Counter[str]) -> dict[str, int]:
    return {key: int(value) for key, value in counter.most_common()}


def _analyze_file(
    file_path: Path,
    bot_cache: dict[Path, LoadedBot],
    max_examples: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    candidate_path = _resolve(str(payload.get("candidate_path", "")))
    if candidate_path not in bot_cache:
        bot_cache[candidate_path] = _load_bot(candidate_path)
    bot = bot_cache[candidate_path]
    decisions: list[dict[str, Any]] = []
    source_stats = {"files": 1, "sources": 0, "sources_without_trace": 0, "trace_rows": 0}

    for source in _iter_trace_sources(payload, file_path):
        source_stats["sources"] += 1
        trace_rows = source["trace"]
        if not trace_rows:
            source_stats["sources_without_trace"] += 1
            continue
        source_stats["trace_rows"] += len(trace_rows)
        hand_net_chips = source["hand_net_chips"] if isinstance(source["hand_net_chips"], list) else []
        for trace in trace_rows:
            if not isinstance(trace, dict) or trace.get("type") != "decision":
                continue
            req = trace.get("request") if isinstance(trace.get("request"), dict) else {}
            state = trace.get("state") if isinstance(trace.get("state"), dict) else {}
            rule_action = _as_int(trace.get("rule_action"), 0)
            advised_action = _as_int(trace.get("advised_action"), rule_action)
            final_action = _as_int(trace.get("final_action"), advised_action)
            rule_label = _action_label(bot, rule_action, req)
            advised_label = _action_label(bot, advised_action, req)
            final_label = _action_label(bot, final_action, req)
            neural = _neural_snapshot(bot, trace)
            outcome = _hand_outcome(trace, hand_net_chips)
            decision = {
                "source_file": str(file_path.relative_to(ROOT)) if file_path.is_relative_to(ROOT) else str(file_path),
                "candidate_path": str(candidate_path.relative_to(ROOT)) if candidate_path.is_relative_to(ROOT) else str(candidate_path),
                "candidate": source["candidate"],
                "opponent": source["opponent"],
                "opponent_path": source["opponent_path"],
                "match_idx": int(source["match_idx"] or 0),
                "leg": source["leg"],
                "hand": int(trace.get("hand", 0) or 0),
                "decision_serial": int(trace.get("decision_serial", 0) or 0),
                "hand_decision_index": int(trace.get("hand_decision_index", 0) or 0),
                "stage": trace.get("stage", ""),
                "round": int(trace.get("round", 0) or 0),
                "is_small_blind": bool(trace.get("is_small_blind")),
                "pot": int(state.get("pot", req.get("pot", 0)) or 0),
                "to_call": int(state.get("to_call", req.get("to_call", 0)) or 0),
                "my_stage_bet": int(state.get("my_stage_bet", req.get("my_stage_bet", 0)) or 0),
                "opponent_stage_bet": int(state.get("opponent_stage_bet", req.get("opponent_stage_bet", 0)) or 0),
                "public_count": len(req.get("public_cards") or []),
                "rule_action": rule_action,
                "advised_action": advised_action,
                "final_action": final_action,
                "rule_label": _label_name(bot, rule_label),
                "advised_label": _label_name(bot, advised_label),
                "final_label": _label_name(bot, final_label),
                "raw_changed": int(advised_action) != int(rule_action),
                "final_changed": int(final_action) != int(rule_action),
                "advised_to_final_changed": int(final_action) != int(advised_action),
                "hand_net_chips": outcome,
                "leg_net_chips": int(source["net_chips"] or 0),
                "neural": neural,
            }
            decisions.append(decision)
    return decisions, source_stats


def _summarize(decisions: list[dict[str, Any]], source_stats: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(decisions)
    neural_changed = [row for row in decisions if row["raw_changed"]]
    final_changed = [row for row in decisions if row["final_changed"]]
    sanitizer_changed = [row for row in decisions if row["advised_to_final_changed"]]
    neural_unchanged = [row for row in decisions if not row["raw_changed"]]
    final_unchanged = [row for row in decisions if not row["final_changed"]]
    counters: dict[str, Counter[str]] = {
        "by_opponent": Counter(),
        "by_stage": Counter(),
        "rule_to_advised": Counter(),
        "rule_to_final": Counter(),
        "advised_to_final": Counter(),
        "policy_top_label": Counter(),
        "multi_action_proposal_label": Counter(),
        "neural_changed_by_opponent": Counter(),
        "neural_changed_by_stage": Counter(),
        "final_changed_by_opponent": Counter(),
        "final_changed_by_stage": Counter(),
        "sanitizer_changed_by_stage": Counter(),
    }
    opponent_outcomes: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {"neural_changed": [], "neural_unchanged": [], "final_changed": [], "final_unchanged": []}
    )
    stage_outcomes: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {"neural_changed": [], "neural_unchanged": [], "final_changed": [], "final_unchanged": []}
    )
    value_deltas_neural_changed: list[float] = []
    value_deltas_neural_unchanged: list[float] = []

    for row in decisions:
        opponent = str(row["opponent"])
        stage = str(row["stage"])
        _add_counter(counters["by_opponent"], opponent)
        _add_counter(counters["by_stage"], stage)
        _add_counter(counters["rule_to_advised"], f"{row['rule_label']}->{row['advised_label']}")
        _add_counter(counters["rule_to_final"], f"{row['rule_label']}->{row['final_label']}")
        _add_counter(counters["advised_to_final"], f"{row['advised_label']}->{row['final_label']}")
        neural = row.get("neural") or {}
        if neural.get("policy_label_name"):
            _add_counter(counters["policy_top_label"], neural["policy_label_name"])
        if neural.get("multi_action_proposal_label"):
            _add_counter(counters["multi_action_proposal_label"], neural["multi_action_proposal_label"])
        if row["raw_changed"]:
            _add_counter(counters["neural_changed_by_opponent"], opponent)
            _add_counter(counters["neural_changed_by_stage"], stage)
        if row["final_changed"]:
            _add_counter(counters["final_changed_by_opponent"], opponent)
            _add_counter(counters["final_changed_by_stage"], stage)
        if row["advised_to_final_changed"]:
            _add_counter(counters["sanitizer_changed_by_stage"], stage)
        outcome = row.get("hand_net_chips")
        if outcome is not None:
            neural_bucket = "neural_changed" if row["raw_changed"] else "neural_unchanged"
            final_bucket = "final_changed" if row["final_changed"] else "final_unchanged"
            opponent_outcomes[opponent][neural_bucket].append(int(outcome))
            opponent_outcomes[opponent][final_bucket].append(int(outcome))
            stage_outcomes[stage][neural_bucket].append(int(outcome))
            stage_outcomes[stage][final_bucket].append(int(outcome))
        delta = neural.get("final_value_delta_vs_rule")
        if delta is not None:
            if row["raw_changed"]:
                value_deltas_neural_changed.append(float(delta))
            else:
                value_deltas_neural_unchanged.append(float(delta))

    return {
        "sources": {
            "files": sum(int(item.get("files", 0)) for item in source_stats),
            "match_sources": sum(int(item.get("sources", 0)) for item in source_stats),
            "sources_without_trace": sum(int(item.get("sources_without_trace", 0)) for item in source_stats),
            "trace_rows": sum(int(item.get("trace_rows", 0)) for item in source_stats),
        },
        "decisions": {
            "total": total,
            "neural_changed": len(neural_changed),
            "sanitizer_changed": len(sanitizer_changed),
            "final_changed": len(final_changed),
            "neural_change_rate": round(len(neural_changed) / max(1, total), 5),
            "sanitizer_change_rate": round(len(sanitizer_changed) / max(1, total), 5),
            "final_change_rate": round(len(final_changed) / max(1, total), 5),
        },
        "counts": {key: _sorted_counter(value) for key, value in counters.items()},
        "outcomes": {
            "neural_changed": _stats([row["hand_net_chips"] for row in neural_changed if row.get("hand_net_chips") is not None]),
            "neural_unchanged": _stats([row["hand_net_chips"] for row in neural_unchanged if row.get("hand_net_chips") is not None]),
            "final_changed": _stats([row["hand_net_chips"] for row in final_changed if row.get("hand_net_chips") is not None]),
            "final_unchanged": _stats([row["hand_net_chips"] for row in final_unchanged if row.get("hand_net_chips") is not None]),
            "by_opponent": {
                key: {bucket: _stats(rows) for bucket, rows in value.items()}
                for key, value in sorted(opponent_outcomes.items())
            },
            "by_stage": {
                key: {bucket: _stats(rows) for bucket, rows in value.items()}
                for key, value in sorted(stage_outcomes.items())
            },
        },
        "neural_value_delta_vs_rule": {
            "neural_changed": _stats(value_deltas_neural_changed),
            "neural_unchanged": _stats(value_deltas_neural_unchanged),
        },
    }


def _compact_example(row: dict[str, Any]) -> dict[str, Any]:
    neural = row.get("neural") or {}
    return {
        "opponent": row["opponent"],
        "match_idx": row["match_idx"],
        "leg": row["leg"],
        "hand": row["hand"],
        "decision_serial": row["decision_serial"],
        "stage": row["stage"],
        "is_small_blind": row["is_small_blind"],
        "pot": row["pot"],
        "to_call": row["to_call"],
        "rule_action": row["rule_action"],
        "advised_action": row["advised_action"],
        "final_action": row["final_action"],
        "rule_label": row["rule_label"],
        "final_label": row["final_label"],
        "hand_net_chips": row["hand_net_chips"],
        "policy_label": neural.get("policy_label_name"),
        "policy_conf": neural.get("policy_conf"),
        "multi_action_proposal": neural.get("multi_action_proposal"),
        "multi_action_proposal_label": neural.get("multi_action_proposal_label"),
        "final_value_delta_vs_rule": neural.get("final_value_delta_vs_rule"),
        "best_value_label": neural.get("best_value_label_name"),
        "best_value_delta_vs_rule": neural.get("best_value_delta_vs_rule"),
    }


def _write(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    output = _resolve(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze native TCP neural decision traces.")
    parser.add_argument("--eval-json", action="append", required=True, help="native_tcp_evaluate JSON output. Repeatable.")
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    parser.add_argument("--examples", type=int, default=20, help="Number of changed-decision examples to keep.")
    args = parser.parse_args()

    bot_cache: dict[Path, LoadedBot] = {}
    all_decisions: list[dict[str, Any]] = []
    stats: list[dict[str, Any]] = []
    for raw_path in args.eval_json:
        decisions, source_stats = _analyze_file(_resolve(raw_path), bot_cache, max(0, int(args.examples)))
        all_decisions.extend(decisions)
        stats.append(source_stats)

    neural_changed = [row for row in all_decisions if row["raw_changed"]]
    changed_with_outcome = [row for row in neural_changed if row.get("hand_net_chips") is not None]
    worst = sorted(changed_with_outcome, key=lambda row: (int(row["hand_net_chips"]), row["opponent"], row["hand"]))
    best = sorted(changed_with_outcome, key=lambda row: (-int(row["hand_net_chips"]), row["opponent"], row["hand"]))
    payload = {
        "eval_json": [str(_resolve(path).relative_to(ROOT)) if _resolve(path).is_relative_to(ROOT) else str(_resolve(path)) for path in args.eval_json],
        "bot_paths": [str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path) for path in sorted(bot_cache)],
        "summary": _summarize(all_decisions, stats),
        "examples": {
            "worst_neural_changed": [_compact_example(row) for row in worst[: max(0, int(args.examples))]],
            "best_neural_changed": [_compact_example(row) for row in best[: max(0, int(args.examples))]],
        },
    }
    _write(args.output, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
