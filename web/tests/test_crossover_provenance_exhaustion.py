"""Crossover provenance-exhaustion distinct outcome (2026-10-20).

v572 (v566×v11) burned all crossover LLM attempts on the identical provenance
mistake — the model rewrote Parent A's ``_decision_from_equity`` as an adapted
variant instead of AST-exact-copying a Parent B component — because the retry
prompt carried no Parent-B component inventory. This module pins:

1. ``_crossover_provenance_retry_feedback`` lists Parent B's importable
   top-level components (def/class @line, AST-derived, bounded) together with
   the verbatim-copy instruction and the observed adapted-variant failure mode.
2. ``CrossoverAttemptExhaustion`` stays FALSY (byte-compatible with the
   historical bare ``False``) while classifying all-provenance exhaustion.
3. Scheduler/saturator pair-avoidance recognizes BOTH abandon reason tokens
   (``crossover_llm_exhausted`` and ``crossover_provenance_exhausted``).
"""

import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


# ---------------------------------------------------------------------------
# 1. Provenance retry feedback: Parent-B component inventory
# ---------------------------------------------------------------------------
_PARENT_B_POLICY = '''\
"""Parent B policy."""

THRESHOLD = 0.62


def _opponent_posterior(ctx):
    return ctx.posterior


class RangeModel:
    pass


async def _async_probe(ctx):
    return ctx.probe


def decide(ctx):
    # nested defs are NOT importable top-level components
    def _inner():
        return None
    return _opponent_posterior(ctx)
'''


def _write_parent_b(tmp_path, source=_PARENT_B_POLICY):
    frozen = tmp_path / "frozen-parent-b"
    frozen.mkdir()
    (frozen / "policy.py").write_text(source, encoding="utf-8")
    return frozen


def test_provenance_retry_feedback_keeps_rejection_header_and_issues(tmp_path):
    import agent_review

    frozen = _write_parent_b(tmp_path)
    issues = [{"path": "policy.py", "reason": "independent_logic_not_parent_b_component"}]

    feedback = agent_review._crossover_provenance_retry_feedback(issues, frozen)

    assert feedback.startswith(
        "\n\n# Previous Attempt Rejected By Crossover Provenance Contract\n"
    )
    assert "independent_logic_not_parent_b_component" in feedback
    assert "pure recombination" in feedback


def test_provenance_retry_feedback_lists_parent_b_top_level_components(tmp_path):
    import agent_review

    frozen = _write_parent_b(tmp_path)

    feedback = agent_review._crossover_provenance_retry_feedback(
        [{"reason": "x"}], frozen
    )

    assert "# Parent-B Importable Components" in feedback
    assert "frozen snapshot, authoritative" in feedback
    # Names render with their def/class kind and source line, in source order.
    assert "- _opponent_posterior (def @6)" in feedback
    assert "- RangeModel (class @10)" in feedback
    assert "- _async_probe (def @14)" in feedback
    # Nested defs are not importable top-level components.
    assert "_inner" not in feedback
    # The verbatim-copy instruction is explicit.
    assert "VERBATIM" in feedback
    assert "do not adapt, rename, or merge" in feedback
    # The production failure mode is named so the model does not repeat it.
    assert "ADAPTED variant of a Parent-A function" in feedback


def test_provenance_retry_feedback_inventory_is_bounded(tmp_path):
    import agent_review

    source = "".join(
        f"def _component_{index}(ctx):\n    return ctx\n\n" for index in range(300)
    )
    frozen = _write_parent_b(tmp_path, source=source)

    inventory = agent_review._render_parent_b_component_inventory(
        agent_review._parent_b_component_inventory(frozen)
    )

    assert len(inventory) <= agent_review._PARENT_B_INVENTORY_MAX_CHARS
    listed = [line for line in inventory.splitlines() if line.startswith("- ")]
    assert len(listed) <= agent_review._PARENT_B_INVENTORY_MAX_ENTRIES
    assert f"... (+{300 - len(listed)} more)" in inventory


def test_provenance_retry_feedback_omits_inventory_when_snapshot_unreadable(tmp_path):
    import agent_review

    # Missing snapshot dir and unparseable policy.py both fail open to the
    # historical header-only feedback (advisory text never blocks the loop).
    missing = tmp_path / "does-not-exist"
    header_only = agent_review._crossover_provenance_retry_feedback(
        [{"reason": "x"}], missing
    )
    assert "# Parent-B Importable Components" not in header_only

    broken = tmp_path / "broken-parent-b"
    broken.mkdir()
    (broken / "policy.py").write_text("def ((", encoding="utf-8")
    assert (
        "# Parent-B Importable Components"
        not in agent_review._crossover_provenance_retry_feedback(
            [{"reason": "x"}], broken
        )
    )


# ---------------------------------------------------------------------------
# 2. Structured falsy exhaustion result
# ---------------------------------------------------------------------------
def test_exhaustion_result_is_falsy_and_classifies_provenance():
    import agent_review

    all_provenance = agent_review.CrossoverAttemptExhaustion(["provenance"] * 5)
    assert bool(all_provenance) is False
    assert all_provenance.exhaustion == "provenance"
    assert all_provenance.attempt_failure_classes == ["provenance"] * 5

    mixed = agent_review.CrossoverAttemptExhaustion(
        ["provenance", "provider_exception"]
    )
    assert bool(mixed) is False
    assert mixed.exhaustion == "mixed"

    empty = agent_review.CrossoverAttemptExhaustion([])
    assert bool(empty) is False
    # No recorded attempt class can never certify "provenance".
    assert empty.exhaustion == "mixed"


def test_exhaustion_max_crossover_retries_is_five():
    import agent_review
    import evolution_infra

    # (b) 2026-10-20: 3 -> 5, two extra attempts for the model to act on the
    # newly injected Parent-B inventory.  The raise is a module-level rebind
    # in agent_review ONLY: evolution_infra.py is a byte-frozen SEMANTIC_PATHS
    # rating-identity carrier (its sha256 feeds the frozen
    # evaluation_data_manifest base identity) and must keep the legacy
    # constant — editing it wedges every frozen generation snapshot at the
    # handoff identity rebuild (v579, 2026-10-20).
    assert agent_review.MAX_CROSSOVER_RETRIES == 5
    assert evolution_infra.MAX_CROSSOVER_RETRIES == 3


# ---------------------------------------------------------------------------
# 3. Scheduler / saturator pair-avoidance recognizes both reason tokens
# ---------------------------------------------------------------------------
def _abandon_rows(reason):
    return [
        {
            "version": 572,
            "checkpoint_stage": "selected",
            "source_v": 566,
            "reason": reason,
            "checkpoint_envelope": {"parent2_v": 11},
        }
    ]


def test_last_abandon_block_steers_both_crossover_exhaustion_tokens(monkeypatch):
    import evolution_infra
    import generation_scheduler as gs

    for reason in (
        "crossover_llm_exhausted:v566xv11",
        "crossover_provenance_exhausted:v566xv11",
    ):
        monkeypatch.setattr(
            evolution_infra,
            "load_abandoned_version_receipts",
            lambda reason=reason: _abandon_rows(reason),
        )
        block = gs._last_abandon_block()
        assert block.startswith("LAST ABANDON RECEIPTS"), reason
        assert "Pick a different parent pair" in block, reason
        assert "do not retry the same pair unchanged" in block, reason


def test_saturator_attribution_prompt_maps_both_crossover_tokens():
    import llm_saturator

    for reason in (
        "crossover_llm_exhausted:v566xv11",
        "crossover_provenance_exhausted:v566xv11",
    ):
        # The attribution job is the LAST rotation entry whenever an abandon
        # receipt is present; scan for its session id instead of a magic
        # number (the 2026-10-28 packet widening moved any hardcoded index
        # off the attribution slot).
        session_id = next(
            i
            for i in range(50)
            if llm_saturator.saturator_job_for(i, abandon_reason=reason)["name"]
            == "abandon_attribution"
        )
        job = llm_saturator.saturator_job_for(session_id, abandon_reason=reason)
        assert job["name"] == "abandon_attribution", reason
        prompt = str(job["prompt"])
        assert reason in prompt
        assert (
            "crossover_provenance_exhausted → parent pair or "
            "verbatim-component port" in prompt
        )
        assert "crossover_llm_exhausted → parent pair or merge" in prompt
