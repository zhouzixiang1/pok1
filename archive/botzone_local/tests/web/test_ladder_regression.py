"""Regression tests for the ladder ↔ mirror_battle interface contract.

Background
----------
A bug (fixed in fix/ladder-mirror-unpack) made `engine/ladder.py` completely
unrunnable: `mirror_battle()` returns a 5-tuple `(match_wins, draws, n_played,
all_logs, net_chips_list)` but ladder unpacked only 4 values, AND the serial
path passed an unsupported `debug_bots` kwarg. Both raised at runtime.

Root cause: `battle.py` added a 5th return value (`net_chips_list`, for AIVAT /
chip-delta analysis) but ladder — a manual tool NOT used by the evolution
pipeline — never tracked the signature change, so it silently broke.

These tests lock the contract so a future `mirror_battle` return-value or
kwarg change cannot silently break ladder again.

Two layers:
- Contract tests (fast, static, no bot subprocess): assert the source-level
  contract holds. CI always runs these — milliseconds, zero external deps.
- Integration test (slow, real subprocess): runs mirror_battle end-to-end and
  unpacks the result exactly as ladder does. Marked `requires_active_bot` +
  `slow`; skipped by default (CI) via pytest.ini `addopts = -m "not slow"`.
"""
import ast
import inspect
import os
import re
import sys

# Resolve engine/ onto sys.path (same pattern as test_logic_aivat.py).
WEB_CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(WEB_CORE)
ENGINE_DIR = os.path.join(PROJECT_ROOT, "engine")
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

import pytest

from battle import mirror_battle

LADDER_PATH = os.path.join(ENGINE_DIR, "ladder.py")
BATTLE_PATH = os.path.join(ENGINE_DIR, "battle.py")

# mirror_battle's documented return arity — the contract ladder depends on.
EXPECTED_RETURN_ARITY = 5
# kwargs ladder is NOT allowed to pass (only battle() accepts debug_bots).
FORBIDDEN_KWARGS = {"debug_bots"}

# Baseline bots (git-tracked) used by the integration test.
BASELINE_BOT1 = os.path.join(PROJECT_ROOT, "bots", "bot1", "main.py")
BASELINE_BOT2 = os.path.join(PROJECT_ROOT, "bots", "bot2", "main.py")


def _read(path):
    with open(path) as fh:
        return fh.read()


def _mirror_battle_return_arity():
    """Count values in mirror_battle's `return ...` statement via AST.

    Walks the AST to find the Return node inside `mirror_battle` (not the
    generator wrapper) and counts top-level tuple elements. Returns None if
    the return shape cannot be statically determined.
    """
    tree = ast.parse(_read(BATTLE_PATH), filename=BATTLE_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "mirror_battle":
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Return)
                    and isinstance(sub.value, ast.Tuple)):
                return len(sub.value.elts)
    return None


def _count_unpack_targets_in_line(line):
    """Count comma-separated assignment targets in an unpack assignment.

    Handles both real source (`a, b, c = ...`) and the inlined subprocess
    script string (`"...    a, b, c = battle_func(...)\n"`) that ladder builds.
    Returns the number of LHS targets, or None if the line isn't an unpack.
    """
    # Strip the python-string quoting that wraps the subprocess script body.
    line = line.strip().strip('"').strip()
    eq = line.find("=")
    if eq == -1:
        return None
    lhs = line[:eq]
    if "," not in lhs:
        return None
    # Count top-level commas (no nested tuples in these unpacks).
    return lhs.count(",") + 1


# ── Contract layer: static source-level guarantees (CI always runs) ────────

class TestMirrorBattleContract:
    """Lock the mirror_battle ↔ ladder interface contract.

    These are intentionally STATIC checks (AST + inspect, no subprocess) so
    they run in milliseconds and never depend on bots existing. They catch
    the exact failure mode of the original bug: a return-value or kwarg
    change in battle.py that ladder fails to track.
    """

    def test_returns_expected_arity(self):
        """mirror_battle must return exactly 5 values (4 broke ladder)."""
        arity = _mirror_battle_return_arity()
        assert arity is not None, (
            "无法静态确定 mirror_battle 的返回元组形状 — 检查 battle.py 的 return 语句"
        )
        assert arity == EXPECTED_RETURN_ARITY, (
            f"mirror_battle 现在返回 {arity} 个值,但 ladder 按 {EXPECTED_RETURN_ARITY} 解包。"
            f"如果 return 值数量变了,必须同步更新 ladder.py 的两处解包 + 本常量。"
        )

    def test_no_forbidden_kwargs(self):
        """mirror_battle must NOT accept debug_bots (that's battle()'s param).

        ladder's serial path once passed debug_bots=None here → TypeError.
        """
        sig = inspect.signature(mirror_battle)
        params = set(sig.parameters)
        leaked = params & FORBIDDEN_KWARGS
        assert not leaked, (
            f"mirror_battle 不应接受 {leaked} 参数(那是 battle() 的)。"
            "若签名确实变了,更新 FORBIDDEN_KWARGS 并复核 ladder 调用。"
        )


class TestLadderUnpackContract:
    """Every mirror_battle call site in ladder.py must unpack the full tuple.

    Guards both the serial path and the inlined subprocess script string.
    If mirror_battle's arity changes and someone updates only one call site,
    this catches the other.
    """

    def test_all_unpacks_match_arity(self):
        """Each `... = battle_func(...)` in ladder unpacks EXPECTED_RETURN_ARITY targets."""
        source = _read(LADDER_PATH)
        mismatches = []
        for lineno, line in enumerate(source.splitlines(), start=1):
            if "battle_func(" not in line or "=" not in line:
                continue
            n_targets = _count_unpack_targets_in_line(line)
            if n_targets is None:
                continue
            if n_targets != EXPECTED_RETURN_ARITY:
                mismatches.append(
                    f"L{lineno}: 解包 {n_targets} 个值,但 mirror_battle 返回 "
                    f"{EXPECTED_RETURN_ARITY} 个 → {line.strip()}"
                )
        assert not mismatches, (
            "ladder.py 存在与 mirror_battle 返回值数量不匹配的解包:\n  "
            + "\n  ".join(mismatches)
        )


# ── Integration layer: real mirror_battle, unpacked as ladder does ─────────

@pytest.mark.slow
@pytest.mark.requires_active_bot
@pytest.mark.timeout(120)  # real bot subprocesses need ~40s; override the 30s default
class TestLadderIntegration:
    """End-to-end: run a real mirror_battle and unpack exactly as ladder does.

    Skipped when baseline bots are absent (CI without the repo's bots/) or
    when slow tests are deselected (default: `addopts = -m "not slow"`).
    Run locally with: `pytest web/tests/test_ladder_regression.py -m slow`
    """

    def test_mirror_battle_unpacks_without_error(self):
        """Running mirror_battle + ladder-style unpack must not raise.

        This is the runtime confirmation of the contract tests above: it
        proves the 5-tuple shape holds at execution time, not just in source.
        Uses n_games=2 and save_log=False (zero file side-effects).

        Note: mirror_battle spawns _PersistentBot subprocesses whose Popen
        objects emit a benign BrokenPipeError from their GC finalizer on
        close. This is a subprocess-module quirk, not a test or battle.py
        bug (close() itself is try/except-guarded). Suppressed via the
        filterwarnings line in pytest.ini.
        """
        if not (os.path.isfile(BASELINE_BOT1) and os.path.isfile(BASELINE_BOT2)):
            pytest.skip("基准 bot1/bot2 不存在 — 无法运行集成测试")

        # Unpack exactly as ladder's serial path does (post-fix).
        match_wins, draws, n_played, all_logs, net_chips_list = mirror_battle(
            BASELINE_BOT1, BASELINE_BOT2, n_games=2,
            verbose=False, save_log=False,
        )

        # Shape sanity (the values themselves are nondeterministic — mirror
        # battles swap hole cards, so we only assert structure, not outcome).
        assert len(match_wins) == 2, "match_wins 应为 [bot0_wins, bot1_wins]"
        assert isinstance(net_chips_list, list), (
            "第 5 个返回值应为 net_chips_list(每局净筹码列表)"
        )
