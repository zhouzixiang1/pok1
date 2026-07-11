"""Pydantic models for validating structured LLM output from each pipeline agent."""

from typing import Literal, Optional, get_args
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from skill_library import valid_skill_layers


MASTER_PLAN_MIN_TASKS = 1
MASTER_PLAN_MAX_TASKS = 3
WORKER_TASK_MIN_TARGET_FILES = 1
WORKER_TASK_MAX_TARGET_FILES = 3
WORKER_PROMPT_MIN_CHARS = 20
WORKER_PROMPT_MAX_CHARS = 12_000

PrecomputeBuildPhase = Literal["module_import"]
PrecomputeFallback = Literal["legal_baseline"]
PRECOMPUTE_BUILD_PHASES = tuple(get_args(PrecomputeBuildPhase))
PRECOMPUTE_FALLBACKS = tuple(get_args(PrecomputeFallback))
PRECOMPUTE_MAX_BUILD_MS = 2_500
PRECOMPUTE_MAX_ENTRIES = 65_536
PRECOMPUTE_MAX_BYTES = 8 * 1024 * 1024
PRECOMPUTE_KEY_SHAPE_PATTERN = (
    r"^(int|str|tuple\[(int|str|bool)(,(int|str|bool)){0,4}\])$"
)
PRECOMPUTE_CONSUMER_PATTERN = (
    r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$"
)

MatchMemoryResetBoundary = Literal["tcp_connection"]
MatchMemorySnapshotField = Literal["opponent_runtime"]
MatchMemoryUpdateEvent = Literal[
    "hand_start",
    "street_start",
    "hero_action",
    "opponent_action",
    "settlement",
    "showdown",
]
MATCH_MEMORY_RESET_BOUNDARIES = tuple(get_args(MatchMemoryResetBoundary))
MATCH_MEMORY_SNAPSHOT_FIELDS = tuple(get_args(MatchMemorySnapshotField))
MATCH_MEMORY_ALLOWED_UPDATE_EVENTS = tuple(get_args(MatchMemoryUpdateEvent))
MATCH_MEMORY_REQUIRED_UPDATE_EVENTS = frozenset({
    "hand_start",
    "opponent_action",
    "settlement",
    "showdown",
})
MATCH_MEMORY_MIN_UPDATE_EVENTS = 4
MATCH_MEMORY_MAX_UPDATE_EVENTS = 6

RUNTIME_CONTRACT_WORKER_PROMPT_TERMS = {
    "decision": ("budget", "fallback", "baseline", "deadline"),
    "precompute_artifacts": ("precompute",),
    "match_memory": ("memory", "confidence", "opponent_runtime"),
    "official_feedback_refs": ("official",),
}


RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_LAYER = {
    "runtime_architecture": ("decision",),
    "precompute": ("precompute_artifacts",),
    "match_memory": ("match_memory",),
    "opponent_model": ("match_memory",),
    "native_tcp": ("decision",),
}
RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_FOCUS = {
    "incremental_match_model": ("match_memory",),
    "reusable_precompute": ("precompute_artifacts",),
    "deadline_refinement": ("decision",),
    "bounded_runtime_enumeration": ("precompute_artifacts",),
    "decision_path_purity": ("decision",),
}
RUNTIME_CONTRACT_REQUIRED_LAYERS = frozenset(
    RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_LAYER
)


def runtime_contract_required_layers() -> set[str]:
    return set(RUNTIME_CONTRACT_REQUIRED_LAYERS)


def runtime_contract_required_sections(
    skill_layer: str,
    architecture_focus_id: str = "",
) -> tuple[str, ...]:
    """Return required RuntimeContract sections for a task's layer and focus."""
    sections = [
        *RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_LAYER.get(skill_layer, ()),
        *RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_FOCUS.get(
            architecture_focus_id,
            (),
        ),
    ]
    return tuple(dict.fromkeys(sections))


def runtime_contract_is_required(
    skill_layer: str,
    architecture_focus_id: str = "",
) -> bool:
    """Return whether a task must carry a RuntimeContract at all."""
    return (
        skill_layer in RUNTIME_CONTRACT_REQUIRED_LAYERS
        or bool(architecture_focus_id.strip())
    )


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
    build_phase: PrecomputeBuildPhase
    max_build_ms: int = Field(ge=1, le=PRECOMPUTE_MAX_BUILD_MS)
    max_entries: int = Field(ge=1, le=PRECOMPUTE_MAX_ENTRIES)
    max_bytes: int = Field(ge=1, le=PRECOMPUTE_MAX_BYTES)
    key_shape: str = Field(pattern=PRECOMPUTE_KEY_SHAPE_PATTERN)
    consumer: str = Field(pattern=PRECOMPUTE_CONSUMER_PATTERN)
    fallback: PrecomputeFallback

    @field_validator("name", "owner_file", "key_shape", "consumer", "fallback")
    @classmethod
    def _trim_artifact_text(cls, value: str) -> str:
        return value.strip()


class MatchMemoryRuntimeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tracker_class: str = Field(min_length=3)
    owner_file: str = Field(pattern=r"^[^/\\]+\.py$")
    reset_boundary: MatchMemoryResetBoundary
    update_events: list[MatchMemoryUpdateEvent] = Field(
        min_length=MATCH_MEMORY_MIN_UPDATE_EVENTS,
        max_length=MATCH_MEMORY_MAX_UPDATE_EVENTS,
    )
    snapshot_field: MatchMemorySnapshotField
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
        if not MATCH_MEMORY_REQUIRED_UPDATE_EVENTS.issubset(value):
            raise ValueError(
                "update_events must include "
                f"{sorted(MATCH_MEMORY_REQUIRED_UPDATE_EVENTS)}"
            )
        return value


class RuntimeContract(BaseModel):
    """Executable contract shared by Master, worker prompt, and quality policy."""

    model_config = ConfigDict(extra="forbid")

    decision: Optional[DecisionRuntimeContract] = None
    precompute_artifacts: list[PrecomputeArtifactContract] = Field(default_factory=list, max_length=4)
    match_memory: Optional[MatchMemoryRuntimeContract] = None
    official_feedback_refs: list[str] = Field(default_factory=list, max_length=8)
    forbidden_runtime_work: list[str] = Field(default_factory=list, max_length=8)


def runtime_contract_worker_prompt_terms(contract: RuntimeContract) -> tuple[str, ...]:
    """Return the literal execution terms a worker prompt must mirror."""
    populated_sections: list[str] = []
    if contract.decision is not None:
        populated_sections.append("decision")
    if contract.precompute_artifacts:
        populated_sections.append("precompute_artifacts")
    if contract.match_memory is not None:
        populated_sections.append("match_memory")
    if contract.official_feedback_refs:
        populated_sections.append("official_feedback_refs")

    terms: list[str] = []
    for section in populated_sections:
        terms.extend(RUNTIME_CONTRACT_WORKER_PROMPT_TERMS[section])
    return tuple(dict.fromkeys(terms))


def runtime_contract_missing_sections(
    contract: RuntimeContract,
    required_sections: tuple[str, ...],
) -> tuple[str, ...]:
    """Return required RuntimeContract sections that are not populated."""
    missing: list[str] = []
    for section in required_sections:
        value = getattr(contract, section)
        if value is None or value == []:
            missing.append(section)
    return tuple(missing)


def master_plan_executable_contract_text() -> str:
    """Render the exact Master limits from the schema's single sources of truth."""
    allowed_events = ", ".join(
        f'"{event}"' for event in MATCH_MEMORY_ALLOWED_UPDATE_EVENTS
    )
    required_events = ", ".join(
        f'"{event}"' for event in sorted(MATCH_MEMORY_REQUIRED_UPDATE_EVENTS)
    )
    lines = [
        "System-owned executable Master-plan contract (generated from output_schema.py; authoritative):",
        (
            f"- tasks: {MASTER_PLAN_MIN_TASKS}..{MASTER_PLAN_MAX_TASKS} items; "
            "worker_id values must be unique."
        ),
        (
            f"- each task.target_files: {WORKER_TASK_MIN_TARGET_FILES}.."
            f"{WORKER_TASK_MAX_TARGET_FILES} files (never more than "
            f"{WORKER_TASK_MAX_TARGET_FILES}); every runtime artifact owner_file must "
            "also appear in target_files or files_allowed."
        ),
        (
            f"- each task.worker_prompt: {WORKER_PROMPT_MIN_CHARS}.."
            f"{WORKER_PROMPT_MAX_CHARS} characters."
        ),
        (
            "- each precompute_artifacts item: "
            f'build_phase="{PRECOMPUTE_BUILD_PHASES[0]}"; '
            f"max_build_ms=1..{PRECOMPUTE_MAX_BUILD_MS}; "
            f"max_entries=1..{PRECOMPUTE_MAX_ENTRIES}; "
            f"max_bytes=1..{PRECOMPUTE_MAX_BYTES}; "
            f"key_shape must match {PRECOMPUTE_KEY_SHAPE_PATTERN!r}; "
            f"consumer must match {PRECOMPUTE_CONSUMER_PATTERN!r}; "
            f'fallback="{PRECOMPUTE_FALLBACKS[0]}".'
        ),
        (
            "- match_memory: "
            f'reset_boundary="{MATCH_MEMORY_RESET_BOUNDARIES[0]}"; '
            f'snapshot_field="{MATCH_MEMORY_SNAPSHOT_FIELDS[0]}"; '
            f"update_events must contain {MATCH_MEMORY_MIN_UPDATE_EVENTS}.."
            f"{MATCH_MEMORY_MAX_UPDATE_EVENTS} unique values chosen only from "
            f"[{allowed_events}], and must include [{required_events}]."
        ),
    ]
    for section, terms in RUNTIME_CONTRACT_WORKER_PROMPT_TERMS.items():
        rendered_terms = ", ".join(f'"{term}"' for term in terms)
        lines.append(
            f"- when runtime_contract.{section} is populated, worker_prompt must "
            f"literally contain: {rendered_terms}."
        )
    for focus_id, sections in RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_FOCUS.items():
        lines.append(
            f"- architecture_focus_id=\"{focus_id}\" requires runtime_contract "
            f"section(s): {', '.join(sections)}."
        )
    return "\n".join(lines)


class WorkerTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: int = Field(ge=1, le=3)
    role: str = Field(description="Algorithmic Logic Architect, Hyperparameter Tuner, or Opponent Modeler")
    target_files: list[str] = Field(
        min_length=WORKER_TASK_MIN_TARGET_FILES,
        max_length=WORKER_TASK_MAX_TARGET_FILES,
    )
    difficulty: str = "medium"
    skill_layer: str = Field(min_length=1, description="Primary strategy/protocol layer: preflop_range, texture, spr, blocker, line_template, protocol, adapter, native_tcp, telemetry, etc.")
    files_allowed: list[str] = Field(default_factory=list)
    prohibited_files: list[str] = Field(default_factory=list)
    expected_diff_shape: str = ""
    behavior_hypothesis: str = ""
    checks_required: list[str] = Field(default_factory=list)
    merge_policy: str = "disjoint_target_files"
    architecture_focus_id: str = ""
    worker_prompt: str = Field(
        min_length=WORKER_PROMPT_MIN_CHARS,
        max_length=WORKER_PROMPT_MAX_CHARS,
        description="Detailed instructions for this worker",
    )
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
        required_sections = runtime_contract_required_sections(
            self.skill_layer,
            self.architecture_focus_id.strip(),
        )
        if not runtime_contract_is_required(
            self.skill_layer,
            self.architecture_focus_id,
        ):
            return self

        contract = self.runtime_contract
        if contract is None:
            raise ValueError(
                "runtime_contract is required when "
                f"skill_layer={self.skill_layer!r} or "
                f"architecture_focus_id={self.architecture_focus_id!r}"
            )

        missing = runtime_contract_missing_sections(contract, required_sections)
        if missing:
            raise ValueError(
                "runtime_contract for "
                f"skill_layer={self.skill_layer!r}, "
                f"architecture_focus_id={self.architecture_focus_id!r} is missing "
                f"{', '.join(missing)}"
            )

        declared_scope = {
            str(item).replace("\\", "/").rsplit("/", 1)[-1]
            for item in [*self.target_files, *self.files_allowed]
            if str(item).strip()
        }
        owners: list[str] = []
        if contract.match_memory is not None:
            owners.append(contract.match_memory.owner_file)
        owners.extend(item.owner_file for item in contract.precompute_artifacts)
        missing_owners = sorted({owner for owner in owners if owner not in declared_scope})
        if missing_owners:
            raise ValueError(
                f"runtime_contract owner file(s) {missing_owners} are outside "
                f"target_files/files_allowed={sorted(declared_scope)}"
            )

        prompt_lower = self.worker_prompt.lower()
        required_terms = runtime_contract_worker_prompt_terms(contract)
        missing_terms = [term for term in required_terms if term not in prompt_lower]
        if missing_terms:
            raise ValueError(
                "runtime_contract is declared but worker_prompt does not mention "
                f"required execution term(s) {missing_terms}"
            )
        return self


class MasterPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: str = Field(min_length=10)
    targeted_failure: str = Field(min_length=5)
    expected_behavior_change: str = ""
    do_not_touch: list[str] = Field(default_factory=list)
    measurement_plan: str = ""
    tasks: list[WorkerTask] = Field(
        min_length=MASTER_PLAN_MIN_TASKS,
        max_length=MASTER_PLAN_MAX_TASKS,
    )

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
