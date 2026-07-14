"""Smoke test for the M5b offline ReBeL-like runner.

This test verifies that the runner module imports correctly and that its
helper functions produce valid types. A full end-to-end run takes several
minutes due to CFR computation and is verified separately.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bots.research_native_lab.common_contracts.actions import Action, ActionKind
from bots.research_native_lab.common_contracts.national_state import NationalGameState
from bots.research_native_lab.rebel_decisionholdem.rebel_like.hunl_pbs import (
    HUNLReachFactorPublicBeliefState,
)
from bots.research_native_lab.rebel_decisionholdem.rebel_like.m5b_data import (
    make_prelabel_plan,
    prelabel_plan_digest,
    build_split_manifest,
)
from bots.research_native_lab.rebel_decisionholdem.rebel_like.m5b_search import (
    DepthLimitedCFRAvg,
    TerminalRolloutLeaf,
    UniformPolicy,
)
from bots.research_native_lab.rebel_decisionholdem.tools.run_m5b_offline_rebel_loop import (
    SCHEMA,
)


CONFIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "m5b_offline_rebel_loop.json"
)


def test_runner_schema_and_config():
    """The runner schema is defined and the config file exists and parses."""
    assert SCHEMA == "route-a1-m5b-offline-runner-v1"
    assert CONFIG_PATH.exists()
    config = json.loads(CONFIG_PATH.read_text())
    assert config["schema"] == "route-a1-m5b-offline-rebel-loop-config-v1"
    assert "network" in config
    assert "solver" in config
    assert "training" in config


def test_runner_generates_valid_plans_and_split():
    """Multiple PBS roots produce valid PreLabelPlans and a split manifest."""
    def d(payload):
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    uniform = UniformPolicy()
    solver = DepthLimitedCFRAvg(
        iterations=2,
        deals_per_iteration=1,
        public_action_depth=1,
        warm_policy=uniform,
        rollout_leaf=TerminalRolloutLeaf(uniform, rollouts=1),
        seed=42,
    )

    plans = []
    # Alternate small blind positions to create distinct public family IDs
    for i in range(10):
        state = NationalGameState.new_hand(i + 1, small_blind=i % 2)
        pbs = HUNLReachFactorPublicBeliefState.from_state(state)
        plan = make_prelabel_plan(
            state,
            pbs,
            trajectory_id=d({"t": i}),
            rollout_group_id=d({"r": i}),
            source_copy_group_id=d({"s": i}),
            source_checkpoint_digest=d({"c": 0}),
            decision_index=i,
            seed_group=i,
        )
        plans.append(plan)

    plan_digest = prelabel_plan_digest(plans)
    manifest = build_split_manifest(
        plans,
        expected_prelabel_plan_digest=plan_digest,
        split_seed=42,
        basis_points={"train": 7000, "validation": 1500, "test": 1500},
        minimum_components={"train": 0, "validation": 0, "test": 0},
    )

    assert "manifest_sha256" in manifest
    assert "sample_splits" in manifest
    for plan in plans:
        assert plan.sample_id in manifest["sample_splits"]
    # With 10 distinct public families, at least 2 splits should be populated
    splits_used = {manifest["sample_splits"][p.sample_id] for p in plans}
    assert len(splits_used) >= 2
