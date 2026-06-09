"""Pydantic models for validating structured LLM output from each pipeline agent."""

from typing import Optional
from pydantic import BaseModel, Field


class WorkerTask(BaseModel):
    worker_id: int = Field(ge=1, le=3)
    role: str = Field(description="Algorithmic Logic Architect, Hyperparameter Tuner, or Opponent Modeler")
    target_files: list[str] = Field(min_length=1)
    difficulty: str = "medium"
    worker_prompt: str = Field(min_length=20, description="Detailed instructions for this worker")


class MasterPlan(BaseModel):
    analysis: str = Field(min_length=10)
    targeted_failure: str = Field(min_length=5)
    expected_behavior_change: str = ""
    do_not_touch: list[str] = []
    measurement_plan: str = ""
    branch_from: Optional[str] = None
    tasks: list[WorkerTask] = Field(min_length=1, max_length=3)


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
    rationale: str = ""


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
