"""Pydantic models for validating structured LLM output from each pipeline agent."""

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skill_library import valid_skill_layers


RUNTIME_CONTRACT_REQUIRED_LAYERS = frozenset({
    "runtime_architecture",
    "precompute",
    "match_memory",
    "opponent_model",
    "native_tcp",
})


def runtime_contract_required_layers() -> set[str]:
    return set(RUNTIME_CONTRACT_REQUIRED_LAYERS)


class DecisionRuntimeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clock: Literal["time.monotonic", "time.perf_counter"]
    hard_deadline_ms: int = Field(
        ge=1,
        le=55_000,
        description=(
            "Socket-owned hard return deadline. The 55 second ceiling reserves at least "
            "five seconds of the official 60 second turn for logging and wire handling."
        ),
    )
    baseline_target_ms: int = Field(
        ge=1,
        le=5_000,
        description=(
            "Target latency for publishing a strategy-derived legal baseline; an always-legal "
            "socket fallback must already exist before strategy code starts."
        ),
    )
    refinement_budget_ms: int = Field(
        ge=1,
        le=54_500,
        description=(
            "Elapsed-time budget from decision start for optional bounded refinement, ending "
            "strictly before the socket hard deadline."
        ),
    )
    baseline_path: str = Field(min_length=5, description="Fast legal action path computed before refinement.")
    fallback_action: str = Field(min_length=3, description="Legal pending-action fallback on deadline/error.")
    refinement_bound: str = Field(min_length=5, description="Concrete loop/sample/search cap after baseline.")
    max_samples: Optional[int] = Field(default=None, ge=1, le=100_000)

    @field_validator("baseline_path", "fallback_action", "refinement_bound")
    @classmethod
    def _trim_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _ordered_decision_budgets(self):
        if self.baseline_target_ms >= self.refinement_budget_ms:
            raise ValueError("baseline_target_ms must be below refinement_budget_ms")
        if self.refinement_budget_ms >= self.hard_deadline_ms:
            raise ValueError("refinement_budget_ms must be below hard_deadline_ms")
        return self


class PrecomputeArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2)
    owner_file: str = Field(pattern=r"^[^/\\]+\.py$")
    build_phase: Literal["module_import"]
    max_build_ms: int = Field(ge=1, le=2_500)
    max_entries: int = Field(ge=1, le=65_536)
    max_bytes: int = Field(ge=1, le=8 * 1024 * 1024)
    key_shape: str = Field(
        pattern=r"^(int|str|tuple\[(int|str|bool)(,(int|str|bool)){0,4}\])$"
    )
    consumer: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")
    fallback: Literal["legal_baseline"]

    @field_validator("name", "owner_file", "key_shape", "consumer", "fallback")
    @classmethod
    def _trim_artifact_text(cls, value: str) -> str:
        return value.strip()


class MatchMemoryRuntimeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracker_class: str = Field(min_length=3)
    owner_file: str = Field(pattern=r"^[^/\\]+\.py$")
    reset_boundary: Literal["tcp_connection"]
    update_events: list[Literal[
        "hand_start",
        "street_start",
        "hero_action",
        "opponent_action",
        "settlement",
        "showdown",
    ]] = Field(min_length=4, max_length=6)
    snapshot_field: Literal["opponent_runtime"]
    max_recent_hands: int = Field(ge=0, le=70)
    prior_rule: str = Field(min_length=5)
    confidence_rule: str = Field(min_length=5)
    adaptation_cap: float = Field(gt=0.0, le=1.0)
    consumer: str = Field(min_length=3)

    @field_validator("tracker_class", "owner_file", "prior_rule", "confidence_rule", "consumer")
    @classmethod
    def _trim_memory_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("update_events")
    @classmethod
    def _unique_update_events(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("update_events must be unique")
        required = {"hand_start", "opponent_action", "settlement", "showdown"}
        if not required.issubset(value):
            raise ValueError(f"update_events must include {sorted(required)}")
        return value


class RuntimeContract(BaseModel):
    """Executable contract shared by Master, worker prompt, and quality policy."""

    model_config = ConfigDict(extra="forbid")

    decision: Optional[DecisionRuntimeContract] = None
    precompute_artifacts: list[PrecomputeArtifactContract] = Field(default_factory=list, max_length=4)
    match_memory: Optional[MatchMemoryRuntimeContract] = None
    official_feedback_refs: list[str] = Field(default_factory=list, max_length=8)
    forbidden_runtime_work: list[str] = Field(default_factory=list, max_length=8)


class WorkerTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: int = Field(ge=1, le=3)
    role: str = Field(description="Algorithmic Logic Architect, Hyperparameter Tuner, or Opponent Modeler")
    target_files: list[str] = Field(min_length=1)
    difficulty: str = "medium"
    skill_layer: str = Field(min_length=1, description="Primary strategy/protocol layer: preflop_range, texture, spr, blocker, line_template, protocol, adapter, native_tcp, telemetry, etc.")
    files_allowed: list[str] = Field(default_factory=list)
    prohibited_files: list[str] = Field(default_factory=list)
    expected_diff_shape: str = ""
    behavior_hypothesis: str = ""
    checks_required: list[str] = Field(default_factory=list)
    merge_policy: str = "disjoint_target_files"
    architecture_focus_id: str = ""
    worker_prompt: str = Field(min_length=20, description="Detailed instructions for this worker")
    runtime_contract: Optional[RuntimeContract] = Field(
        default=None,
        description="Required for runtime/precompute/match-memory/native TCP tasks.",
    )

    @field_validator("skill_layer")
    @classmethod
    def _known_skill_layer(cls, value: str) -> str:
        layer = value.strip()
        if layer not in valid_skill_layers():
            raise ValueError(
                f"Unknown skill_layer {value!r}; expected one of {sorted(valid_skill_layers())}"
            )
        return layer

    @model_validator(mode="after")
    def _runtime_contract_matches_layer(self):
        if self.skill_layer not in RUNTIME_CONTRACT_REQUIRED_LAYERS:
            return self

        contract = self.runtime_contract
        if contract is None:
            raise ValueError(
                f"runtime_contract is required when skill_layer={self.skill_layer!r}"
            )

        missing: list[str] = []
        if self.skill_layer in {"runtime_architecture", "native_tcp"}:
            if contract.decision is None:
                missing.append("decision")
        if self.skill_layer == "precompute" and not contract.precompute_artifacts:
            missing.append("precompute_artifacts")
        if self.skill_layer in {"match_memory", "opponent_model"} and contract.match_memory is None:
            missing.append("match_memory")
        if missing:
            raise ValueError(
                f"runtime_contract for skill_layer={self.skill_layer!r} is missing "
                f"{', '.join(missing)}"
            )
        return self


class MasterPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: str = Field(min_length=10)
    targeted_failure: str = Field(min_length=5)
    expected_behavior_change: str = ""
    do_not_touch: list[str] = Field(default_factory=list)
    measurement_plan: str = ""
    tasks: list[WorkerTask] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def _unique_worker_ids(self):
        """Enforce each worker task targets a distinct worker_id.

        Without this, the Master can emit two tasks for the same worker_id,
        collapsing parallelism and producing ambiguous worker dispatch. role is
        intentionally left as a free-form str (architect/tuner/other substrings
        all permitted) to stay compatible with existing plan history.
        """
        seen = set()
        for t in self.tasks:
            if t.worker_id in seen:
                raise ValueError(f"Duplicate worker_id {t.worker_id} in tasks; each worker must have a unique id")
            seen.add(t.worker_id)
        return self


class ReviewResult(BaseModel):
    approved: bool
    feedback: str = ""
    quality_score: int = Field(ge=1, le=10)
    change_summary: str = ""
    risk_areas: list[str] = []


class Evidence(BaseModel):
    h2h_weaknesses: list[str] = []
    experience_pool_refs: list[str] = []
    diff_refs: list[str] = []


class CriticResult(BaseModel):
    score: int = Field(ge=1, le=10)
    approved: bool
    strategic_assessment: str = ""
    evidence: Evidence = Field(default_factory=Evidence)
    feedback: str = ""
    local_optima_warning: bool = False
    local_optima_reason: Optional[str] = None


class DirectionEntry(BaseModel):
    version: int
    direction: str
    outcome: str = ""


class DirectionAuditResult(BaseModel):
    last_directions: list[DirectionEntry] = []
    repetition_detected: bool
    repetition_count: int = 0
    exhausted_directions: list[str] = []
    mandatory_constraints: Optional[str] = None
    suggested_direction: Optional[str] = None
    confidence: str = "medium"


class ArchivistResult(BaseModel):
    generation_assessment: str = Field(description="improvement, neutral, regression, or mixed")
    archive_notes: str = ""
    experience_updates: list[str] = Field(default_factory=list, max_length=2)
    strategic_advice: str = ""


class StagnationResult(BaseModel):
    is_stagnant: bool
    confidence: str = "medium"
    recommendation: str = "continue"
    branch_from: Optional[str] = None
    reason: str = ""


class PerformanceResult(BaseModel):
    trend: str = Field(description="improving, stagnant, or declining")
    verified_improvements: list[str] = []
    persistent_weaknesses: list[str] = []
    diversity_needed: bool = False
    diversity_reason: Optional[str] = None
    suggestion: str = ""


class CombinedAnalystResult(BaseModel):
    is_stagnant: bool = False
    confidence: str = "medium"
    trend: str = Field(default="stagnant", description="improving, stagnant, or declining")
    diversity_needed: bool = False
    diversity_reason: Optional[str] = None
    recommendation: str = "continue"
    branch_from: Optional[str] = None
    verified_improvements: list[str] = []
    persistent_weaknesses: list[str] = []
    reason: str = ""
    suggestion: Optional[str] = None
    recommended_source: str = ""
    source_rationale: str = ""
    causal_analysis: Optional[str] = None


# ──────────────────────────────────────────────
# Audit Agent Schemas (Phase 0)
# ──────────────────────────────────────────────

class MasterPlanAuditResult(BaseModel):
    """P0-1: Post-Master plan verification audit."""
    plan_coherent: bool = True
    contradiction_found: bool = False
    contradictions: list[str] = []
    experience_alignment: str = "aligned"  # aligned, misaligned, unrelated
    direction_novelty: str = "novel"       # novel, incremental, repetitive
    overall_pass: bool = True
    feedback: str = ""
    retry_recommended: bool = False


class WorkerCoTCheckResult(BaseModel):
    """P0-2: Worker Chain-of-Thought consistency check."""
    worker_id: int = 0
    cot_consistent: bool = True
    discrepancies: list[str] = []
    logical_contradictions: list[str] = []
    boundary_violations: list[str] = []
    focus_areas: list[str] = []  # Injected into reviewer if issues found


class DynamicTestScenario(BaseModel):
    """P0-3: Single LLM-generated test scenario."""
    id: str
    description: str
    input: dict
    expected_actions: list[str] = []
    forbidden_actions: list[str] = []
    zero_action: Optional[str] = None
    legal_actions: list[str] = []
    raise_min: Optional[int] = None
    raise_max: Optional[int] = None
    allin_requires_minus2: bool = False
    national_legal_expected: bool = False
    rationale: str = ""

    @field_validator("input")
    @classmethod
    def _input_is_single_request(cls, value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("input must be a single request dict")
        if "requests" in value or "responses" in value:
            raise ValueError("input must be a single request dict, not a full bot payload")
        return value


class DynamicTestSuite(BaseModel):
    """P0-3: Collection of LLM-generated test scenarios."""
    scenarios: list[DynamicTestScenario] = Field(max_length=10)


class PrecommitSemanticResult(BaseModel):
    """P0-4: Semantic interpretation of precommit eval battle results."""
    win_pattern_analysis: str = ""
    top_opponent_assessment: str = ""
    regression_semantics: str = "safe"  # clear_regression, marginal, safe, improvement
    recommended_action: str = "proceed"  # proceed, caution, block
    confidence: str = "medium"
    data_quality: dict[str, object] = Field(default_factory=dict)
    block_evidence: list[str] = Field(default_factory=list, max_length=5)


class DegenerationDiagnosis(BaseModel):
    """P1-1: Continuous degeneration root cause diagnosis."""
    is_degenerating: bool = False
    root_causes: list[str] = []
    commit_evidence: list[str] = []
    strategy_drift_evidence: list[str] = []
    recommendation: str = "continue"  # continue, crossover, branch_from, force_exploration
    urgent_intervention: bool = False


class CrossoverCompatibilityResult(BaseModel):
    """P1-3: Crossover parent compatibility audit."""
    compatible: bool = True
    compatibility_score: int = Field(ge=1, le=10)
    conflict_areas: list[str] = []
    suggested_merge_approach: str = ""
    files_to_take_from_a: list[str] = []
    files_to_take_from_b: list[str] = []


class ExperiencePoolAuditResult(BaseModel):
    """P1-4: Experience pool quality audit."""
    stale_entries: list[str] = []
    contradictions: list[str] = []
    relevance_issues: list[str] = []
    recommended_removals: list[str] = []
    recommended_additions: list[str] = []
    overall_health: str = "healthy"  # healthy, needs_cleanup, stale


# ──────────────────────────────────────────────
# Schema Registry
# ──────────────────────────────────────────────

# Map agent names to their Pydantic models
AGENT_SCHEMAS = {
    "master": MasterPlan,
    "reviewer": ReviewResult,
    "critic": CriticResult,
    "direction_auditor": DirectionAuditResult,
    "archivist": ArchivistResult,
    "stagnation_analyst": StagnationResult,
    "performance_analyst": PerformanceResult,
    "combined_analyst": CombinedAnalystResult,
    # Audit agents
    "master_plan_auditor": MasterPlanAuditResult,
    "worker_cot_checker": WorkerCoTCheckResult,
    "dynamic_test_generator": DynamicTestSuite,
    "precommit_semantic": PrecommitSemanticResult,
    "degeneration_diagnosis": DegenerationDiagnosis,
    "crossover_compatibility": CrossoverCompatibilityResult,
    "experience_pool_audit": ExperiencePoolAuditResult,
}


def validate_agent_output(agent_name: str, data: dict) -> tuple[dict, list[str]]:
    """Validate agent output against its Pydantic schema.

    Returns (validated_data, errors). On validation failure, returns
    (original_data, error_messages) so the caller can retry with context.
    """
    schema_cls = AGENT_SCHEMAS.get(agent_name)
    if schema_cls is None:
        return data, []

    try:
        model = schema_cls.model_validate(data)
        return model.model_dump(), []
    except Exception as e:
        errors = []
        if hasattr(e, 'errors'):
            for err in e.errors():
                loc = '.'.join(str(x) for x in err['loc'])
                errors.append(f"{loc}: {err['msg']}")
        else:
            errors.append(str(e))
        return data, errors
