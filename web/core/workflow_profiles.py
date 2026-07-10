"""Conservative workflow profiles for the evolution pipeline."""

from __future__ import annotations

import os

from pipeline_schema import WorkflowProfile
from skill_library import describe_skill_layers


_PROFILES = {
    "default": WorkflowProfile(
        profile_id="default",
        description="Balanced evolution profile with national acceptance enabled.",
    ),
    "national_primary": WorkflowProfile(
        profile_id="national_primary",
        description=(
            "Legacy adapter-backed national regression profile. It cannot produce "
            "or commit formal national-bot versions."
        ),
        evaluation_protocol="national",
        rating_protocol="national",
        national_execution_mode="adapter",
        national_acceptance_hands=70,
        national_acceptance_hard=True,
        national_acceptance_timeout_sec=420,
        national_precommit_hands=70,
        national_precommit_matches=1,
        national_rating_hands=70,
        national_rating_matches=1,
        eval_wait_min_games=24,
        eval_wait_rd_threshold=110.0,
        eval_wait_rd_min_games=12,
        focus_skill_layers=["protocol", "adapter", "action_sanitizer", "opponent_model"],
    ),
    "national_native": WorkflowProfile(
        profile_id="national_native",
        description="Make generated bots TCP-native national clients; adapter is legacy regression only.",
        evaluation_protocol="national",
        rating_protocol="national",
        national_execution_mode="native_tcp",
        national_acceptance_hands=70,
        national_acceptance_hard=True,
        national_acceptance_timeout_sec=600,
        national_precommit_hands=70,
        national_precommit_matches=1,
        national_rating_hands=70,
        national_rating_matches=1,
        eval_wait_min_games=24,
        eval_wait_rd_threshold=110.0,
        eval_wait_rd_min_games=12,
        focus_skill_layers=[
            "protocol",
            "native_tcp",
            "action_sanitizer",
            "runtime_architecture",
            "precompute",
            "match_memory",
            "opponent_model",
        ],
    ),
    "national_strict": WorkflowProfile(
        profile_id="national_strict",
        description="Prioritize national TCP legality and adapter transparency.",
        evaluation_protocol="local_json",
        national_acceptance_hands=20,
        national_acceptance_hard=True,
        focus_skill_layers=["protocol", "adapter", "action_sanitizer"],
    ),
    "postflop_skill": WorkflowProfile(
        profile_id="postflop_skill",
        description="Focus worker effort on postflop texture, SPR, blockers, and line templates.",
        focus_skill_layers=["texture", "spr", "blocker", "line_template"],
    ),
    "preflop_range": WorkflowProfile(
        profile_id="preflop_range",
        description="Focus worker effort on preflop range and blind-vs-blind spots.",
        focus_skill_layers=["preflop_range", "bb_vs_limp", "bb_vs_open"],
    ),
    "exploration_diversity": WorkflowProfile(
        profile_id="exploration_diversity",
        description="Prefer novel behavior niches while keeping hard protocol gates.",
        hidden_scenarios_enabled=True,
        focus_skill_layers=["novelty", "map_elites"],
    ),
}


def get_workflow_profile(profile_id: str | None = None) -> WorkflowProfile:
    selected = profile_id or os.environ.get("POK_WORKFLOW_PROFILE") or "national_native"
    return _PROFILES.get(selected, _PROFILES["default"])


def profile_summary(profile: WorkflowProfile | None = None) -> str:
    p = profile or get_workflow_profile()
    layers = ", ".join(p.focus_skill_layers) if p.focus_skill_layers else "balanced"
    return (
        f"Workflow profile: {p.profile_id}\n"
        f"- {p.description}\n"
        f"- evaluation_protocol={p.evaluation_protocol}\n"
        f"- rating_protocol={p.rating_protocol}\n"
        f"- national_execution_mode={p.national_execution_mode}\n"
        f"- max_workers={p.max_workers}\n"
        f"- national_acceptance_hands={p.national_acceptance_hands}, "
        f"hard={p.national_acceptance_hard}, "
        f"timeout={p.national_acceptance_timeout_sec}s\n"
        f"- national_precommit_hands={p.national_precommit_hands}, "
        f"matches={p.national_precommit_matches}\n"
        f"- national_rating_hands={p.national_rating_hands}, "
        f"matches={p.national_rating_matches}\n"
        f"- eval_wait_min_games={p.eval_wait_min_games}, "
        f"rd_threshold={p.eval_wait_rd_threshold:g}, "
        f"rd_min_games={p.eval_wait_rd_min_games}\n"
        f"- focus_skill_layers={layers}\n"
        f"\nSkill layer contract:\n{describe_skill_layers(p.focus_skill_layers or None)}"
    )
