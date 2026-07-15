import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "web" / "core" / "prompts"


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _native_worker_prompt() -> str:
    return _prompt("worker_prompt.md").replace(
        "{execution_profile_contract}",
        _prompt("worker_profile_national_native.md"),
    )


def test_core_prompts_include_full_national_legality_rules():
    prompt_names = [
        "worker_profile_national_native.md",
        "master_prompt.md",
        "reviewer_prompt.md",
        "crossover_prompt.md",
    ]
    combined = "\n".join(_prompt(name) for name in prompt_names)

    required_phrases = [
        "sever/国赛平台",
        "raise-to-total",
        "Exact `prev * 2` is legal",
        "conservative",
        "postflop first action cannot be",
        "check is illegal",
        "Preflop BB cannot",
        "all remaining chips",
        "consecutive all-ins are illegal",
    ]
    for phrase in required_phrases:
        assert phrase in combined

    forbidden_legality_claims = [
        "strictly greater than 2x",
        "strictly >2x",
        "minimum valid re-raise after raise X is X*2+1",
    ]
    for claim in forbidden_legality_claims:
        assert claim not in combined


def test_active_generation_prompts_use_national_bot_namespace():
    prompt_names = [
        "combined_analyst.md",
        "orchestrator.md",
        "master_plan_audit.md",
        "crossover_prompt.md",
        "reviewer_prompt.md",
        "critic_prompt.md",
    ]
    combined = "\n".join(_prompt(name) for name in prompt_names)

    forbidden_patterns = [
        r"bots/claude_v",
        r"\bclaude_vN\b",
        r'"claude_v',
        r"`bot-v",
        r"\sbot-v",
        r"(?<!national-)bot-vN",
        r"(?<!national-)bot-v\{",
    ]
    for pattern in forbidden_patterns:
        assert not re.search(pattern, combined), pattern

    required_fragments = [
        "bots/national_v",
        "national_vN",
        "national-bot-v",
        "national_bot.py",
        "policy.py",
    ]
    for fragment in required_fragments:
        assert fragment in combined
    assert "sever/bot_adapter.py" not in combined


def test_retired_live_analyst_prompts_are_not_exposed_as_active_roles():
    from server.routes.prompts import ALLOWED_PROMPTS

    assert "combined_analyst" in ALLOWED_PROMPTS
    assert "initial" not in ALLOWED_PROMPTS
    assert "match_analyst" not in ALLOWED_PROMPTS
    assert "performance_analyst" not in ALLOWED_PROMPTS
    assert "stagnation_analyzer" not in ALLOWED_PROMPTS


def test_auxiliary_prompts_block_national_protocol_misleading_plans():
    assert "National rules safety" in _prompt("master_plan_audit.md")
    assert "national protocol legality assumptions" in _prompt("crossover_compatibility.md")
    assert not (PROMPTS / "dynamic_test_generator.md").exists()

    active = _prompt("master_prompt.md") + _prompt("reviewer_prompt.md")
    assert "Postflop first action" in active
    assert "check/check" in active


def test_tuner_prompt_contract_matches_planning_hard_gate():
    master_prompt = _prompt("master_prompt.md")
    worker_prompt = _prompt("worker_prompt.md")

    assert '`target_files` must be exactly `["policy.py"]`' in master_prompt
    assert "existing named numeric" in master_prompt
    assert "Any other file, new functions, classes, imports, or control flow" in master_prompt

    assert "must target `policy.py` only" in worker_prompt
    assert "Any other file, new functions/classes/imports/control flow" in worker_prompt
    assert "constants.py only" not in worker_prompt


def test_weak_model_exploration_uses_frozen_structural_evidence_not_retired_qd():
    from skill_library import SKILL_LAYERS
    from workflow_profiles import get_workflow_profile

    novelty = SKILL_LAYERS["novelty"]
    profile = get_workflow_profile("exploration_diversity")
    master = _prompt("master_prompt.md")
    worker = _prompt("worker_prompt.md")

    assert novelty.poker_skill_ref == "Frozen structural proposal contract"
    assert novelty.required_spot_fields == (
        "proposal_id",
        "structural_call_chain",
        "falsifier",
        "consumer_trace_digest",
    )
    assert "niche_id" not in novelty.required_spot_fields
    assert "children_count" not in novelty.gate_metrics
    assert "frozen, falsifiable" in profile.description
    assert "A Tuner-only proposal is invalid" in master
    assert "BAD primary plan" in master
    assert "subordinate calibration" in worker


def test_prompts_do_not_inject_retired_replay_experience_surface():
    master_prompt = _prompt("master_prompt.md")
    worker_prompt = _prompt("worker_prompt.md")

    # The storage kernel may retain identity-bound replay records internally,
    # but active planning has no Markdown/background experience injection.
    assert "mutable `web/core/results/*`" in master_prompt
    assert "the other checkout" in " ".join(master_prompt.split())
    assert "battle_lessons.jsonl" not in master_prompt
    assert "battle_evidence.jsonl" not in master_prompt
    assert "Battle Experience" not in master_prompt
    assert "battle_lesson_*" not in worker_prompt
    assert "<battle_evidence_contract>" not in worker_prompt


def test_master_and_skill_registry_have_no_retired_exploitability_sidecar():
    from skill_library import SKILL_LAYERS

    master_prompt = _prompt("master_prompt.md")
    opponent_model = SKILL_LAYERS["opponent_model"]

    assert "exploitability_weaknesses" not in master_prompt
    assert "Exploitability Weaknesses" not in master_prompt
    assert "exploitability_delta" not in opponent_model.gate_metrics
    assert opponent_model.gate_metrics == (
        "h2h_delta",
        "opponent_profile_consumed",
    )


def test_prompts_require_national_runtime_architecture_contracts():
    master_prompt = _prompt("master_prompt.md")
    worker_prompt = _native_worker_prompt()
    reviewer_prompt = _prompt("reviewer_prompt.md")
    critic_prompt = _prompt("critic_prompt.md")

    assert "hard_deadline_ms" in master_prompt
    assert "baseline_target_ms" in master_prompt
    assert "refinement_budget_ms" in master_prompt
    assert "Official EXE Compliance Feedback" in master_prompt
    assert "{official_feedback}" in master_prompt
    assert "National Runtime Architecture Feedback" in master_prompt
    assert "{runtime_feedback}" in master_prompt
    assert "`runtime_contract` object" in master_prompt
    assert '"runtime_contract":' in master_prompt
    assert "{master_plan_executable_contract}" in master_prompt
    assert "{strategy_reference_packet}" in master_prompt
    assert "reference_pack_id" in master_prompt
    assert "same-shape/different-value" in master_prompt
    assert "same constants and literal types used" in master_prompt
    assert "system-owned, read-only pure import-time data" in worker_prompt
    assert "task's injected typed strategy-reference card" in worker_prompt
    assert "persistent," in worker_prompt and "worker owns policy imports" in worker_prompt
    assert "# Runtime Contract" in worker_prompt
    assert "Runtime architecture check" in reviewer_prompt
    assert "incremental" in reviewer_prompt
    assert "baseline_passed_check" in reviewer_prompt
    assert "sparse snapshot" in reviewer_prompt
    assert "get_baseline_decision" in worker_prompt
    assert "A missed deadline terminates the complete process group/tree" in worker_prompt
    assert "Official Windows EXE artifacts are compliance evidence only" in critic_prompt
    assert "Never use EXE" in critic_prompt


def test_native_prompts_require_authoritative_terminal_repair_and_memory():
    master = _prompt("master_prompt.md")
    worker = _native_worker_prompt()
    reviewer = _prompt("reviewer_prompt.md")

    for prompt in (master, worker, reviewer):
        assert "street-closing" in prompt
        assert "exactly once" in prompt
        assert "decision_context" in prompt
        assert "req['hand_runtime']" not in prompt
        assert "req['opponent_runtime']" not in prompt
        assert "showdown_range" in prompt
        assert "terminal" in prompt and "fold" in prompt and "call" in prompt

    required_context_fields = (
        "preflop_aggressor",
        "preflop_spot",
        "position",
        "can_donk",
        "can_delayed_probe",
        "street_open",
        "spr",
        "pot_odds",
    )
    for field in required_context_fields:
        assert field in master
        assert field in worker
    for field in ("position", "can_donk", "can_delayed_probe", "showdown_range"):
        assert field in reviewer


def test_native_prompts_require_transcript_reachability_and_control_pairs():
    master = _prompt("master_prompt.md")
    worker = _native_worker_prompt()
    reviewer = _prompt("reviewer_prompt.md")

    assert "producer -> policy consumer -> socket-validated typed intent -> telemetry" in master
    assert "producer -> consumer -> socket-validated typed intent -> telemetry" in reviewer
    for prompt in (master, reviewer):
        assert "firing tuple" in prompt
        assert "one-predicate control" in prompt
        assert "difference" in prompt

    for prompt in (master, worker, reviewer):
        assert "hero BB" in prompt
        assert "SB raise" in prompt
        assert "official `call`" in prompt or "official wire `call`" in prompt
        assert "opponent_checked_back" in prompt
        assert "check/check" in prompt


def test_native_prompts_reject_fake_refinement_and_threshold_only_innovation():
    master = _prompt("master_prompt.md")
    worker = _native_worker_prompt()
    reviewer = _prompt("reviewer_prompt.md")

    for prompt in (master, reviewer):
        assert "strictly under 250 ms" in prompt
        assert "sample_count" in prompt
        assert "original baseline" in prompt or "input baseline" in prompt
        assert "fixed-seed" in prompt
        assert "improved action" in prompt or "action improves" in prompt

    assert "at least eight trusted refinement steps" in worker
    assert "at least 5 ms" in worker
    assert "typed intent" in worker or "typed-intent" in worker

    assert "one attributable structural hypothesis" in master
    assert "threshold-only" in master
    assert "unreachable refinement facade" in master


def test_native_prompts_preserve_both_20260711_official_oracles():
    master = _prompt("master_prompt.md")
    worker = _prompt("worker_profile_national_native.md")
    reviewer = _prompt("reviewer_prompt.md")

    for prompt in (master, worker, reviewer):
        normalized = " ".join(prompt.split())
        assert "Exact" in normalized and "2x" in normalized
        assert "1..70" in normalized
        assert "1..69" in normalized or "69 TCP settlement pairs" in normalized
        assert "STATE:0..69" in normalized
        assert (
            "69 settlements alone" in normalized
            or "69 TCP settlement pairs are sufficient only" in normalized
            or "completion then requires starts 1..70, settlements 1..69" in normalized
        )
        assert "hand-70 `earnChips`" in normalized
        assert "official-full-v5" in normalized

    assert "H2H" in master
    for prompt in (worker, reviewer):
        assert "Glicko" in prompt and "H2H" in prompt

    assert "Official EXE Compliance Feedback (compliance-only, not strength)" in master
    assert "National Runtime Architecture Feedback (planning signal, not legality)" in master


def test_all_pipeline_roles_share_normal_and_first_strict_certification_boundary():
    prompts = {
        "master": _prompt("master_prompt.md"),
        "worker": _prompt("worker_profile_national_native.md"),
        "reviewer": _prompt("reviewer_prompt.md"),
        "critic": _prompt("critic_prompt.md"),
        "orchestrator": _prompt("orchestrator.md"),
    }

    for role, prompt in prompts.items():
        normalized = " ".join(prompt.split())
        assert "official-full-v5" in normalized, role
        assert "five" in normalized and "70-hand self-play" in normalized, role
        assert "three" in normalized and "eligible" in normalized, role
        assert "v143" in normalized and "operator-only" in normalized, role
        assert "first_strict_control_v1" in normalized, role
        assert "v144+" in normalized, role


def test_eqr_means_equity_realization_not_expected_quantity_of_risk():
    critic = _prompt("critic_prompt.md")
    master = _prompt("master_prompt.md")

    assert "Equity Realization (EQR)" in master
    assert "Equity Realization (EQR)" in critic
    assert "expected-quantity-of-risk" not in critic


def test_prompts_never_promote_web_arena_to_official_or_strength_authority():
    orchestrator = _prompt("orchestrator.md")
    worker = _prompt("worker_profile_national_native.md")
    reviewer = _prompt("reviewer_prompt.md")
    critic = _prompt("critic_prompt.md")
    official_analyst = _prompt("official_platform_analysis.md")

    assert "Arena completion" in orchestrator
    assert "Only a valid content-bound certificate" in orchestrator
    assert "The Web Arena is diagnostic only" in worker
    assert "Arena and official chip totals never" in worker
    assert "Arena THP" in reviewer
    assert "non-strength, local diagnostic evidence" in critic
    assert "deterministic EXE verdict" in official_analyst


def test_worker_execution_profiles_do_not_mix_native_and_botzone_verification():
    common = _prompt("worker_prompt.md")
    native = _native_worker_prompt()

    assert common.count("{execution_profile_contract}") == 1
    assert not (PROMPTS / "worker_profile_legacy_adapter.md").exists()
    assert "delimiter-free raw TCP byte stream" in native
    assert "five-file submission ABI" in " ".join(native.split())
    assert "The complete candidate decision surface is `policy.py`" in native
    assert "Do not reconstruct these values from raw TCP text" in native
    assert "require_current_stream_decoder=True" in native
    assert "require_current_decision_runtime=True" in native
    assert "smoke_tester.py" not in native
    assert "emit exactly one JSON" not in native


def test_role_prompts_match_the_runtime_read_capability_guard():
    worker = _native_worker_prompt()
    master = _prompt("master_prompt.md")
    reviewer = _prompt("reviewer_prompt.md")
    crossover = _prompt("crossover_prompt.md")

    assert "python -m py_compile {candidate_path}/policy.py" in worker
    assert "diff -rq bots/national_v" not in worker
    assert "python -B -c" not in worker
    assert "Only the `Read` tool is available" in master
    assert "python -c" not in master
    assert "python -c" not in reviewer
    assert "git log" not in reviewer
    assert "python -c" not in crossover
    assert "national_v{version}/*.py" not in crossover
    assert "bots/national_v{version}/policy.py" not in crossover
    assert "exact system-injected lease target" in crossover
    assert "py_compile" not in crossover
    assert "system quality gate owns compilation" in crossover


def test_active_llm_prompt_templates_expose_only_strict_raw_tcp_vocabulary():
    """Weak models must not be taught retired protocol/control-plane concepts."""

    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(PROMPTS.glob("*.md"))
    )
    retired_patterns = (
        r"\bbotzone\b",
        r"\bbot[_ -]?adapter\b",
        r"\badapter\b",
        r"\bnewline\b",
        r"\bmap[-_ ]?elites?\b",
        r"\bqd archive\b",
        r"\bniches?\b",
        r"\belite parents?\b",
        r"\bbattle[_ -]?scheduler\b",
        r"\bcertification queue\b",
        r"\bofficial(?: exe)? queue\b",
        r"\blegacy (?:exe|transport|protocol|subprocess)\b",
        r"\bretired protocol\b",
        r"\brequest/response\b",
        r"\binteger actions?\b",
        r"\bjson entrypoint\b",
    )
    for pattern in retired_patterns:
        assert not re.search(pattern, combined, flags=re.IGNORECASE), pattern

    required = (
        "delimiter-free",
        "fragmented",
        "coalesced",
        "typed policy-intent ABI",
        "official-full-v5",
    )
    for phrase in required:
        assert phrase in combined


def test_official_repair_worker_prompt_keeps_the_same_strict_abi():
    import tool_planning

    task = tool_planning._official_repair_tasks(
        {
            "next_v": 143,
            "source_v": 143,
            "stage": "official_full_failed",
            "gate_results": {
                "official_full": {
                    "issues": ["policy.py: bounded decision exception"],
                },
            },
        },
        "Repair the bounded policy exception.",
    )[0]
    prompt = task["worker_prompt"]
    assert "five-file strict artifact" in prompt
    assert "system-owned TCP entrypoint byte-identical" in prompt
    assert "bot_adapter" not in prompt.lower()


def test_master_and_crossover_prompts_do_not_embed_generation_case_history():
    for name in ("master_prompt.md", "crossover_prompt.md"):
        prompt = _prompt(name)
        assert "Known Mandatory Fixes" not in prompt
        # v143/v144+ describe the permanent first-strict certification
        # boundary, not an injected historical strategy case.
        history_free = prompt.replace("v143", "").replace("v144+", "")
        assert not re.search(r"\bv\d{2,}\b", history_free), name
        assert not re.search(r"\bL\d{3,}\b", prompt), name
        assert "Deterministic Invariants" in prompt


def test_readonly_review_prompts_ban_temp_redirect_probes():
    for name in ("reviewer_prompt.md", "master_prompt.md", "master_plan_audit.md"):
        text = _prompt(name)
        assert "read-only" in text
        assert "temp files" in text or "temporary files" in text
        assert "write redirects" in text or "redirects" in text
        assert any(marker in text for marker in (
            "diff -u",
            "direct read-only commands",
            "Only the `Read` tool is available",
            "direct statically bounded reads",
            "no filesystem tools",
        ))

    critic = _prompt("critic_prompt.md")
    assert "read-only advisory gate" in critic
    assert "only tool is Read" in critic
    assert "Bash, Git, Python subprocesses" in critic
    assert "complete evidence boundary" in critic


def test_master_prompts_prioritize_h2h_snapshot_over_spotlight_samples():
    master_prompt = _prompt("master_prompt.md")
    audit_prompt = _prompt("master_plan_audit.md")
    critic_prompt = _prompt("critic_prompt.md")

    assert "The stable H2H snapshot is authoritative" in master_prompt
    assert "Replay Spotlight and supplied match-history excerpts" in master_prompt
    assert "must not override" in master_prompt
    assert "`games`, `a_wins`, `b_wins`, and `win_rate`" in master_prompt
    assert "canonical_citation" in master_prompt
    assert "Never read `web/core/results/head_to_head.json`" in master_prompt

    assert "Reject plans that use replay spotlight" in audit_prompt
    assert "short-window samples" in audit_prompt
    assert "snapshot row key and exact `games`, `a_wins`, `b_wins`, and `win_rate`" in audit_prompt

    assert "Replay spotlight is hand-level evidence only" in critic_prompt
    assert "prefer H2H for matchup claims" in critic_prompt


def test_prompts_require_reachable_embedded_selftests():
    master_prompt = _prompt("master_prompt.md")
    worker_prompt = _prompt("worker_prompt.md")

    assert 'if __name__ == "__main__"' in master_prompt
    assert 'if __name__ == "__main__"' in worker_prompt
    assert "_self_test_*" in master_prompt
    assert "_self_test_*" in worker_prompt
    assert "Never leave a new top-level `_self_test_*`" in worker_prompt
    assert "Never leave new standalone `_self_test_*` functions uncalled" in master_prompt


def test_retired_strategy_side_roles_have_no_active_prompt_or_callable():
    tool_gates = (ROOT / "web" / "core" / "tool_gates.py").read_text(encoding="utf-8")
    audit_agents = (ROOT / "web" / "core" / "audit_agents.py").read_text(encoding="utf-8")

    for prompt_name in (
        "regression_guardian.md",
        "precommit_semantic.md",
        "critic_calibration.md",
    ):
        assert not (PROMPTS / prompt_name).exists()
    for symbol in (
        "_run_regression_guardian",
        "_run_precommit_semantic",
        "_run_critic_calibration",
    ):
        assert symbol not in tool_gates
        assert symbol not in audit_agents


def test_active_prompts_use_call_to_pass_after_postflop_check():
    master = _prompt("master_prompt.md")
    worker = _prompt("worker_profile_national_native.md")
    crossover = _prompt("crossover_prompt.md")
    worker_normalized = " ".join(worker.split())

    assert "second postflop pass after a check is wire `call`" in master
    assert "after the first postflop action a pass is `call`" in worker_normalized
    assert "after a postflop check the second pass is call, not check" in crossover
