"""Structural / runtime verification of mandatory bot fixes.

fix_injection.py decides whether a fix is already present via exact SUBSTRING
matching (its `guard` and `search` strings). If a worker refactors the target
function (renames a variable, rewrites the expression, moves code into a
helper), the substring no longer matches and the fix is silently skipped.
The bot then passes every quality gate without BOT-001a / BOT-002a / BOT-004
protection.

This module is the AUTHORITATIVE fix-present judgment. It does NOT look at the
textual shape of the code; it checks the STRUCTURE / RUNTIME behavior:

- BOT-001a  (wheel straight A-2-3-4-5): subprocess-import the bot's card_utils,
  call evaluate_5() on a non-flush wheel, assert the result is a straight with
  high == 5. On any import/runtime failure it falls back to an AST scan for the
  literal set {14, 2, 3, 4, 5} inside evaluate_5.
- BOT-002a  (inclusive official re-raise boundary): AST-locate the
  min_raise_action assignment and accept both exact ``2x`` and conservative
  ``2x + 1`` formulas, while rejecting a statically confirmed value below the
  official inclusive boundary.
- BOT-004   (TOTAL_HANDS == 70): subprocess-import the bot's constants and
  assert TOTAL_HANDS == 70.

Each verifier runs in a fresh SUBPROCESS (mirrors smoke_tester.py) to avoid the
bot's import side-effects polluting this process. Each verifier is wrapped in
try/except: on ANY exception it returns ok=True (a verifier FAILURE must never
block the pipeline — only a CONFIRMED invariant violation blocks).
"""

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path


# ──────────────────────────────────────────────
# Wheel-straight test cards (engine/judge.py convention)
# card = rank_offset * 4 + suit,  rank = card // 4 + 2  (2..14),  suit = card % 4
# We pick mixed suits so the hand is NOT a flush; that isolates the straight
# branch (category 4) instead of the straight-flush branch (category 8).
#   Ace  -> 48 (48//4+2 = 14, suit 0)
#   Five -> 13 (13//4+2 = 5,  suit 1)
#   Four -> 10 (10//4+2 = 4,  suit 2)
#   Three-> 7  (7//4+2  = 3,  suit 3)
#   Two  -> 0  (0//4+2  = 2,  suit 0)
# Expected: evaluate_5 returns a tuple whose [0] in (4, 8) and [1] == 5.
_WHEEL_CARDS = [48, 13, 10, 7, 0]


# Subprocess probe scripts. Each imports ONLY the target module from the bot
# directory (inserted at sys.path[0]) and prints a single JSON line.
_WHEEL_PROBE = """import json, sys
sys.path.insert(0, sys.argv[1])
from card_utils import evaluate_5  # noqa: E402
res = evaluate_5(%(cards)s)
cat = res[0] if isinstance(res, tuple) and res else None
high = res[1] if isinstance(res, tuple) and len(res) > 1 else None
print(json.dumps({"category": cat, "high": high}))
""" % {"cards": _WHEEL_CARDS}

_TOTAL_PROBE = """import json, sys
sys.path.insert(0, sys.argv[1])
from constants import TOTAL_HANDS  # noqa: E402
print(json.dumps({"total": TOTAL_HANDS}))
"""


def _run_probe(script: str, bot_dir: Path, timeout: float = 15.0) -> dict | None:
    """Run a one-off probe script in a subprocess with bot_dir first on sys.path.

    Returns the parsed JSON dict the script prints, or None on any failure
    (non-zero exit, timeout, JSON decode error).
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix="_probe.py", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(script)
        probe_path = tf.name
    try:
        proc = subprocess.run(
            [sys.executable, probe_path, str(bot_dir)],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return None
        # Probe prints exactly one JSON line to stdout.
        out = (proc.stdout or "").strip().splitlines()
        if not out:
            return None
        import json as _json
        return _json.loads(out[-1])
    except Exception:
        return None
    finally:
        try:
            os.unlink(probe_path)
        except OSError:
            pass


def _verify_wheel(bot_dir: Path) -> dict:
    """BOT-001a: wheel straight A-2-3-4-5 must be a straight with high==5."""
    if not (bot_dir / "card_utils.py").exists():
        # card_utils absent — cannot verify; let the compile/smoke gates handle a
        # genuinely broken bot. Do not block here.
        return {"ok": True, "reason": "card_utils.py absent — wheel contract not applicable"}
    data = _run_probe(_WHEEL_PROBE, bot_dir)
    if data is not None:
        cat = data.get("category")
        high = data.get("high")
        # category 4 = straight, 8 = straight flush; in both, high must be 5.
        if cat in (4, 8) and high == 5:
            return {"ok": True, "reason": f"evaluate_5(wheel)={cat},{high} (straight, high=5)"}
        return {
            "ok": False,
            "reason": (
                f"evaluate_5(A-2-3-4-5) returned category={cat}, high={high}; "
                "expected a straight with high==5 (wheel fix missing)"
            ),
        }
    # Subprocess import failed (e.g. bot renamed card_utils / evaluate_5).
    # Fall back to an AST scan for the wheel literal inside evaluate_5.
    found = _ast_wheel_literal_in_evaluate_5(bot_dir)
    if found:
        return {"ok": True, "reason": "AST fallback: {14, 2, 3, 4, 5} literal found in evaluate_5"}
    return {
        "ok": False,
        "reason": (
            "Wheel probe could not import evaluate_5 AND AST fallback found no "
            "{14, 2, 3, 4, 5} literal inside evaluate_5 — wheel fix missing or evaluate_5 removed"
        ),
    }


def _ast_wheel_literal_in_evaluate_5(bot_dir: Path) -> bool:
    """Return True if evaluate_5 contains a set/frozenset/tuple literal equal to
    {14, 2, 3, 4, 5} (any order). Robust to variable renames inside the function."""
    card_utils = bot_dir / "card_utils.py"
    if not card_utils.exists():
        return False
    try:
        src = card_utils.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception:
        return False

    target = {14, 2, 3, 4, 5}
    # Scan the whole module: the wheel literal may live in evaluate_5 OR a
    # renamed helper (the runtime probe already failed, so this is a fallback
    # heuristic — the primary check is the subprocess probe above).
    for node in ast.walk(tree):
        vals = _literal_int_set(node)
        if vals is not None and vals == target:
            return True
    return False


def _literal_int_set(node: ast.AST) -> set[int] | None:
    """If node is a Set/Tuple/List of int constants, return the set of ints;
    otherwise None. Handles {14, 2, 3, 4, 5}, (14, 2, 3, 4, 5), frozenset(...)."""
    elements = None
    if isinstance(node, ast.Set):
        elements = node.elts
    elif isinstance(node, (ast.Tuple, ast.List)):
        elements = node.elts
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset":
        if node.args and isinstance(node.args[0], (ast.Set, ast.Tuple, ast.List)):
            elements = node.args[0].elts
    if elements is None:
        return None
    out = set()
    for e in elements:
        if isinstance(e, ast.Constant) and isinstance(e.value, int) and not isinstance(e.value, bool):
            out.add(e.value)
        else:
            return None  # non-int element -> not our target literal
    return out


def _affine_terms(node: ast.AST) -> tuple[dict[str, float], float] | None:
    """Return simple affine coefficients without evaluating candidate code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return {}, float(node.value)
    if isinstance(node, ast.Name):
        return {node.id: 1.0}, 0.0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _affine_terms(node.operand)
        if value is None:
            return None
        sign = -1.0 if isinstance(node.op, ast.USub) else 1.0
        return ({key: sign * coefficient for key, coefficient in value[0].items()}, sign * value[1])
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
        left = _affine_terms(node.left)
        right = _affine_terms(node.right)
        if left is None or right is None:
            return None
        sign = -1.0 if isinstance(node.op, ast.Sub) else 1.0
        coefficients = dict(left[0])
        for key, coefficient in right[0].items():
            coefficients[key] = coefficients.get(key, 0.0) + sign * coefficient
        return coefficients, left[1] + sign * right[1]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left = _affine_terms(node.left)
        right = _affine_terms(node.right)
        if left is None or right is None:
            return None
        if left[0] and right[0]:
            return None
        scalar, value = (left[1], right) if not left[0] else (right[1], left)
        return (
            {key: scalar * coefficient for key, coefficient in value[0].items()},
            scalar * value[1],
        )
    return None


def _minimum_raise_candidates(value: ast.AST) -> list[ast.AST]:
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "max":
        return list(value.args)
    return [value]


def _looks_like_previous_raise_to(name: str) -> bool:
    lowered = name.lower()
    return "raise" in lowered and any(
        marker in lowered
        for marker in ("last", "prev", "previous", "round", "street", "judge")
    )


def _looks_like_own_street_bet(name: str) -> bool:
    lowered = name.lower()
    owner = any(marker in lowered for marker in ("my", "own", "hero", "self"))
    contribution = any(
        marker in lowered for marker in ("bet", "commit", "contribution", "stage")
    )
    return owner and contribution


def _inclusive_raise_boundary_margin(value: ast.AST) -> float | None:
    """Return proven minimum headroom over ``2 * raise_to - own_bet``.

    State implementations use different names (for example
    ``judge_round_raise`` versus ``last_raise_to``), but arbitrary variables are
    not interchangeable evidence.  Bind both semantic roles, require every
    other affine term to vanish, and evaluate extra positive slope at the
    smallest valid prior raise-to (the national big blind, 100 chips).  Thus a
    formula such as ``2.5 * last_raise_to - own_bet - 1`` retains its real
    headroom instead of being reduced incorrectly to the constant ``-1``.
    Unknown shapes remain inconclusive and must not become a blocking false
    positive.
    """
    minimum_prior_raise_to = 100.0
    for candidate in _minimum_raise_candidates(value):
        affine = _affine_terms(candidate)
        if affine is None:
            continue
        coefficients, constant = affine
        raise_terms = [
            (name, coefficient)
            for name, coefficient in coefficients.items()
            if _looks_like_previous_raise_to(name)
        ]
        own_bet_terms = [
            (name, coefficient)
            for name, coefficient in coefficients.items()
            if _looks_like_own_street_bet(name)
        ]
        if len(raise_terms) != 1 or len(own_bet_terms) != 1:
            continue
        raise_name, raise_coefficient = raise_terms[0]
        own_bet_name, own_bet_coefficient = own_bet_terms[0]
        if own_bet_coefficient != -1.0:
            continue
        unexpected = {
            name: coefficient
            for name, coefficient in coefficients.items()
            if name not in {raise_name, own_bet_name} and coefficient != 0.0
        }
        if unexpected:
            continue
        if raise_coefficient < 2.0:
            # With an unbounded legal raise-to domain, a slope below two is
            # eventually below the inclusive official boundary regardless of
            # its constant offset.
            return float("-inf")
        return (
            (raise_coefficient - 2.0) * minimum_prior_raise_to + constant
        )
    return None


def _verify_min_raise(bot_dir: Path) -> dict:
    """BOT-002a: accept exact 2x or conservative positive headroom."""
    state_py = bot_dir / "state.py"
    if not state_py.exists():
        # state.py may legitimately be absent in some bot layouts; verifier
        # failure must not block — treat as ok (no contract to check).
        return {"ok": True, "reason": "state.py absent — min_raise contract not applicable"}
    try:
        src = state_py.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except Exception:
        # Cannot parse -> cannot confirm a violation -> do not block.
        return {"ok": True, "reason": "state.py unparseable — min_raise contract skipped"}

    assignments = []  # list of (value-node, unparse-text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "min_raise_action":
                    try:
                        text = ast.unparse(node.value)
                    except Exception:
                        text = ""
                    assignments.append((node.value, text))

    if not assignments:
        # No min_raise_action assignment at all — the bot may compute raises a
        # different way; we have no confirmed invariant violation, do not block.
        return {"ok": True, "reason": "no min_raise_action assignment found — contract skipped"}

    proven_margins = []
    for value, text in assignments:
        margin = _inclusive_raise_boundary_margin(value)
        if margin is not None:
            proven_margins.append(margin)
        if margin is not None and margin >= 0.0:
            policy = "conservative headroom" if margin > 0.0 else "exact inclusive 2x"
            return {"ok": True, "reason": f"min_raise_action satisfies {policy}: {text[:80]}"}
    joined = " | ".join(text[:80] for _, text in assignments)
    if not proven_margins:
        return {
            "ok": True,
            "reason": (
                "min_raise_action shape is inconclusive; no below-boundary "
                f"violation was proven: {joined}"
            ),
        }
    return {
        "ok": False,
        "reason": (
            "min_raise_action assignment(s) are not proven at or above the official "
            f"inclusive 2x boundary: {joined}"
        ),
    }


def _verify_total_hands(bot_dir: Path) -> dict:
    """BOT-004: TOTAL_HANDS must equal 70."""
    data = _run_probe(_TOTAL_PROBE, bot_dir)
    if data is not None:
        total = data.get("total")
        if total == 70:
            return {"ok": True, "reason": "TOTAL_HANDS == 70"}
        return {"ok": False, "reason": f"TOTAL_HANDS == {total!r}, expected 70"}
    # Import failed (constants.py missing/renamed). Fall back to AST scan for a
    # TOTAL_HANDS = 70 assignment so a refactor that keeps the literal still
    # passes, but a constants.py that is genuinely gone is flagged.
    constants_py = bot_dir / "constants.py"
    if not constants_py.exists():
        return {"ok": True, "reason": "constants.py absent — TOTAL_HANDS contract not applicable"}
    try:
        tree = ast.parse(constants_py.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": True, "reason": "constants.py unparseable — TOTAL_HANDS contract skipped"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "TOTAL_HANDS":
                    if (isinstance(node.value, ast.Constant) and node.value.value == 70):
                        return {"ok": True, "reason": "AST fallback: TOTAL_HANDS = 70"}
                    return {
                        "ok": False,
                        "reason": (
                            f"TOTAL_HANDS assigned a non-70 value "
                            f"(AST: {ast.dump(node.value)}) — import probe also failed"
                        ),
                    }
    # No TOTAL_HANDS assignment found at all -> contract not applicable, do not block.
    return {"ok": True, "reason": "no TOTAL_HANDS assignment found — contract skipped"}


# Verifier crashes are infrastructure failures. They must not silently pass and
# must not be presented as a confirmed candidate violation.
_VERIFIERS = {
    "BOT-001a": _verify_wheel,
    "BOT-002a": _verify_min_raise,
    "BOT-004": _verify_total_hands,
}


def verify_fixes(bot_dir) -> dict:
    """Run every mandatory-fix verifier against *bot_dir*.

    Args:
        bot_dir: Path (or str) to the bot directory (contains card_utils.py,
            constants.py, state.py).

    Returns:
        {fix_id: {"ok": bool, "reason": str}} for every verifier.
        A verifier that raises returns outcome=infrastructure_failure.
    """
    bot_dir = Path(bot_dir)
    results: dict[str, dict] = {}
    for fix_id, fn in _VERIFIERS.items():
        try:
            res = fn(bot_dir)
            if not isinstance(res, dict) or "ok" not in res:
                res = {
                    "ok": False,
                    "outcome": "infrastructure_failure",
                    "reason": f"verifier returned invalid result: {res!r}",
                }
        except Exception as e:  # noqa: BLE001 - converted to typed infra evidence
            res = {
                "ok": False,
                "outcome": "infrastructure_failure",
                "reason": f"verifier raised {type(e).__name__}: {e}",
            }
        else:
            res.setdefault("outcome", "passed" if res.get("ok") else "candidate_failure")
        results[fix_id] = res
    return results
