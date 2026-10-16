"""Precommit rework-round default calibration regressions (2026-10-16).

Same-seed rework rounds are zero-gain: a rework round reuses the
checkpoint-frozen precommit plan (tool_eval stored_plan; the seed nonce is
generated only when no plan exists), and measured same-seed replays
produced identical score lines (v558: four rounds all 6W-6L; v560: two
rounds identical).  The default ceiling is therefore 3 -> 1 on the sole
live reader's ``_tw`` parent (tool_planning_worker) — the
evolution_infra/evolution_core definitions stay byte-frozen because those
files sit inside the rating-daemon SEMANTIC_PATHS identity set.  The
POK_MAX_PRECOMMIT_REWORK_ROUNDS env override must keep working.
"""

import importlib

import evolution_infra
import tool_planning_worker as _tw


def test_precommit_rework_rounds_default_is_one_on_the_live_reader():
    assert _tw.MAX_PRECOMMIT_REWORK_ROUNDS == 1


def test_carrier_files_stay_byte_frozen():
    # evolution_infra keeps the historical default; only the reader-side
    # rebind carries the calibration.
    assert evolution_infra.MAX_PRECOMMIT_REWORK_ROUNDS == 3


def test_precommit_rework_rounds_env_override_still_applies(monkeypatch):
    monkeypatch.setenv("POK_MAX_PRECOMMIT_REWORK_ROUNDS", "3")
    try:
        reloaded = importlib.reload(_tw)
        assert reloaded.MAX_PRECOMMIT_REWORK_ROUNDS == 3
    finally:
        monkeypatch.delenv("POK_MAX_PRECOMMIT_REWORK_ROUNDS")
        importlib.reload(_tw)
    assert _tw.MAX_PRECOMMIT_REWORK_ROUNDS == 1
