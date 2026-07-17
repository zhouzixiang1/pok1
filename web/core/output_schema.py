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
MatchMemorySnapshotField = Literal["opponent"]
MatchMemoryPriorRule = Literal["beta_prior_weight_8"]
MatchMemoryConfidenceRule = Literal[
    "global_actions_over_actions_plus_24_and_context_samples_over_samples_plus_8"
]
MatchMemoryConsumer = Literal[
    "policy.get_baseline_decision",
    "policy.iter_decisions",
]
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
MATCH_MEMORY_PRIOR_RULES = tuple(get_args(MatchMemoryPriorRule))
MATCH_MEMORY_CONFIDENCE_RULES = tuple(get_args(MatchMemoryConfidenceRule))
MATCH_MEMORY_CONSUMERS = tuple(get_args(MatchMemoryConsumer))
MATCH_MEMORY_ALLOWED_UPDATE_EVENTS = tuple(get_args(MatchMemoryUpdateEvent))
MATCH_MEMORY_REQUIRED_UPDATE_EVENTS = frozenset({
    "hand_start",
    "opponent_action",
    "settlement",
    "showdown",
})
MATCH_MEMORY_MIN_UPDATE_EVENTS = 4
MATCH_MEMORY_MAX_UPDATE_EVENTS = 6

StateLearningWorkPrimitive = Literal[
    "sample_counted_candidate_batch",
]
StateLearningProfileDimension = Literal[
    "action_profile",
    "terminal_response",
    "showdown_range",
]
StateLearningLineControl = Literal["donk", "delayed_probe"]
StateLearningOracleRef = Literal[
    "docs/official-raise-boundary-oracle-2026-07-11.md",
    "docs/official-terminal-settlement-oracle-2026-07-11.md",
]
STATE_LEARNING_WORK_PRIMITIVES = tuple(get_args(StateLearningWorkPrimitive))
STATE_LEARNING_PROFILE_DIMENSIONS = tuple(get_args(StateLearningProfileDimension))
STATE_LEARNING_LINE_CONTROLS = tuple(get_args(StateLearningLineControl))
STATE_LEARNING_ORACLE_REFS = tuple(get_args(StateLearningOracleRef))
NATIONAL_POLICY_FOCUS_ID = "national_policy_v1_state_learning"
POLICY_CONTEXT_SCHEMA_VERSION = 1
PolicyIntentKind = Literal["pass", "fold", "allin", "raise"]
POLICY_INTENT_KINDS = tuple(get_args(PolicyIntentKind))
POLICY_ENTRYPOINTS = (
    "get_baseline_decision",
    "iter_decisions",
)
POLICY_CONTEXT_TOP_LEVEL_FIELDS = (
    "schema_version",
    "runtime_version",
    "decision_id",
    "cards",
    "hand",
    "betting",
    "history",
    "line",
    "legal",
    "opponent",
    "deadline",
)
STATE_LEARNING_PRIMARY_CHECKS = {
    "sample_counted_candidate_batch": (
        "fast_policy_baseline",
        "incremental_refinement_protocol",
        "budget_scaled_refinement",
    ),
    "action_profile": ("incremental_opponent_model",),
    "terminal_response": ("terminal_response_adaptation",),
    "showdown_range": ("showdown_range_adaptation",),
    "donk": ("donk_line_reachability",),
    "delayed_probe": ("delayed_probe_line_reachability",),
}
MASTER_PROPOSAL_FALSIFIER_TESTS = (
    "fast_policy_baseline",
    "incremental_refinement_protocol",
    "incremental_opponent_model",
    "terminal_response_adaptation",
    "showdown_range_adaptation",
    "donk_line_reachability",
    "delayed_probe_line_reachability",
)
MASTER_PROPOSAL_FALSIFIER_PRIMARY = {
    "fast_policy_baseline": "sample_counted_candidate_batch",
    "incremental_refinement_protocol": "sample_counted_candidate_batch",
    "incremental_opponent_model": "action_profile",
    "terminal_response_adaptation": "terminal_response",
    "showdown_range_adaptation": "showdown_range",
    "donk_line_reachability": "donk",
    "delayed_probe_line_reachability": "delayed_probe",
}
STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS = {
    "sample_counted_candidate_batch": "deadline",
    "action_profile": "opponent.rates",
    "terminal_response": "opponent.terminal_response",
    "showdown_range": "opponent.showdown_range",
    "donk": "line.can_donk",
    "delayed_probe": "line.can_delayed_probe",
}
STATE_LEARNING_INTERVENTION_TARGET_ALIASES = {
    "deadline": ("deadline",),
    "opponent.rates": (
        "opponent.rates",
        "opponent.rates.aggression",
        "opponent.rates.preflop_vpip",
        "opponent.rates.fold_to_raise",
        "opponent.rates.fold_to_allin",
        "action_profile",
        "fold_rate",
        "call_rate",
        "raise_rate",
        "allin_rate",
    ),
    "opponent.terminal_response": (
        "opponent.terminal_response",
        "opponent.terminal_response.fold_to_raise",
        "opponent.terminal_response.fold_to_jam",
        "opponent.terminal_response.river_overcall",
        "terminal_response",
        "call_raise",
        "raise_over_raise",
    ),
    "opponent.showdown_range": (
        "opponent.showdown_range",
        "showdown_range",
        "oppo_hands",
    ),
    "line.can_donk": ("line.can_donk", "can_donk", "donk"),
    "line.can_delayed_probe": (
        "line.can_delayed_probe",
        "can_delayed_probe",
        "delayed_probe",
    ),
}
# Some public-state leaves exist under more than one independently governed
# opponent-model namespace.  A proposal that names only the leaf is ambiguous
# even when it also mentions one target root elsewhere: the executable claim
# must use one of these complete owner-qualified literals.
STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS = {
    "fold_to_raise": (
        "opponent.rates.fold_to_raise",
        "opponent.terminal_response.fold_to_raise",
    ),
}
STATE_LEARNING_PRIMARY_PROMPT_TERMS = {
    "sample_counted_candidate_batch": ("sample_count", "deadline"),
    "action_profile": ("action_profile", "context", "opponent"),
    "terminal_response": ("terminal_response", "confidence"),
    "showdown_range": ("showdown_range", "confidence"),
    "donk": ("can_donk", "positive/control"),
    "delayed_probe": ("can_delayed_probe", "positive/control"),
}

RUNTIME_CONTRACT_WORKER_PROMPT_TERMS = {
    "policy_abi": ("decision_context", "typed intent", "raise_to", "pass"),
    "decision": ("budget", "fallback", "baseline", "deadline"),
    "precompute_artifacts": ("precompute",),
    "match_memory": ("memory", "confidence", "context", "opponent"),
    "official_feedback_refs": ("official",),
}


RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_LAYER = {
    "runtime_architecture": ("policy_abi", "decision"),
    "precompute": ("policy_abi", "precompute_artifacts"),
    "match_memory": ("policy_abi", "match_memory"),
    "opponent_model": ("policy_abi", "match_memory"),
    "native_tcp": ("policy_abi", "decision"),
    "action_intent": ("policy_abi",),
}
RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_FOCUS = {
    NATIONAL_POLICY_FOCUS_ID: ("policy_abi", "state_learning"),
    "incremental_match_model": ("match_memory",),
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
            "Target latency for publishing a policy-derived typed baseline; an always-legal "
            "socket fallback must already exist before candidate policy code starts."
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
    prior_rule: MatchMemoryPriorRule
    confidence_rule: MatchMemoryConfidenceRule
    adaptation_cap: float = Field(gt=0.0, le=1.0)
    consumer: MatchMemoryConsumer

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


class StateLearningRuntimeContract(BaseModel):
    """One mechanically selected policy innovation for a generation.

    The official oracle references and candidate dimension are closed literals,
    not reviewer prose. Exactly one work primitive, opponent-profile dimension,
    or line control is primary in a generation; the other dimensions remain
    shadow evidence unless a passing parent capability would regress.
    """

    model_config = ConfigDict(extra="forbid")

    work_primitive: Optional[StateLearningWorkPrimitive] = None
    profile_dimensions: list[StateLearningProfileDimension] = Field(
        default_factory=list,
        max_length=1,
    )
    line_controls: list[StateLearningLineControl] = Field(
        default_factory=list,
        max_length=1,
    )
    oracle_refs: list[StateLearningOracleRef] = Field(min_length=2, max_length=2)

    @field_validator("profile_dimensions", "line_controls")
    @classmethod
    def _unique_primary_values(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("state-learning primary values must be unique")
        return value

    @field_validator("oracle_refs")
    @classmethod
    def _complete_oracle_pair(cls, value: list[str]) -> list[str]:
        if set(value) != set(STATE_LEARNING_ORACLE_REFS):
            raise ValueError(
                "oracle_refs must contain the exact raise-boundary and "
                "terminal-settlement oracle documents"
            )
        return list(STATE_LEARNING_ORACLE_REFS)

    @model_validator(mode="after")
    def _exactly_one_primary_innovation(self):
        selected = (
            int(self.work_primitive is not None)
            + len(self.profile_dimensions)
            + len(self.line_controls)
        )
        if selected != 1:
            raise ValueError(
                "state_learning must declare exactly one primary innovation across "
                "work_primitive, profile_dimensions, and line_controls"
            )
        return self

    def primary_innovation(self) -> str:
        if self.work_primitive is not None:
            return self.work_primitive
        if self.profile_dimensions:
            return self.profile_dimensions[0]
        return self.line_controls[0]

    def primary_checks(self) -> tuple[str, ...]:
        return STATE_LEARNING_PRIMARY_CHECKS[self.primary_innovation()]


class PolicyABIContract(BaseModel):
    """Closed candidate/system boundary for national raw-TCP decisions."""

    model_config = ConfigDict(extra="forbid")

    module: Literal["policy.py"] = "policy.py"
    context_schema_version: Literal[1] = POLICY_CONTEXT_SCHEMA_VERSION
    context_fields: list[Literal[
        "schema_version",
        "runtime_version",
        "decision_id",
        "cards",
        "hand",
        "betting",
        "history",
        "line",
        "legal",
        "opponent",
        "deadline",
    ]] = Field(
        default_factory=lambda: list(POLICY_CONTEXT_TOP_LEVEL_FIELDS),
        min_length=len(POLICY_CONTEXT_TOP_LEVEL_FIELDS),
        max_length=len(POLICY_CONTEXT_TOP_LEVEL_FIELDS),
    )
    entrypoints: list[Literal[
        "get_baseline_decision",
        "iter_decisions",
    ]] = Field(
        default_factory=lambda: list(POLICY_ENTRYPOINTS),
        min_length=len(POLICY_ENTRYPOINTS),
        max_length=len(POLICY_ENTRYPOINTS),
    )
    intent_kinds: list[PolicyIntentKind] = Field(
        default_factory=lambda: list(POLICY_INTENT_KINDS),
        min_length=len(POLICY_INTENT_KINDS),
        max_length=len(POLICY_INTENT_KINDS),
    )
    raise_field: Literal["raise_to"] = "raise_to"
    pass_mapping: Literal["socket_owner_call_or_check"] = (
        "socket_owner_call_or_check"
    )

    @field_validator("context_fields")
    @classmethod
    def _complete_context_fields(cls, value: list[str]) -> list[str]:
        if tuple(value) != POLICY_CONTEXT_TOP_LEVEL_FIELDS:
            raise ValueError(
                "context_fields must equal the ordered national decision_context v1 ABI"
            )
        return list(POLICY_CONTEXT_TOP_LEVEL_FIELDS)

    @field_validator("entrypoints")
    @classmethod
    def _complete_entrypoints(cls, value: list[str]) -> list[str]:
        if tuple(value) != POLICY_ENTRYPOINTS:
            raise ValueError("entrypoints must equal the ordered policy ABI")
        return list(POLICY_ENTRYPOINTS)

    @field_validator("intent_kinds")
    @classmethod
    def _complete_intent_kinds(cls, value: list[str]) -> list[str]:
        if tuple(value) != POLICY_INTENT_KINDS:
            raise ValueError("intent_kinds must equal pass/fold/allin/raise")
        return list(POLICY_INTENT_KINDS)


class RuntimeContract(BaseModel):
    """Executable contract shared by Master, worker prompt, and quality policy."""

    model_config = ConfigDict(extra="forbid")

    policy_abi: Optional[PolicyABIContract] = None
    decision: Optional[DecisionRuntimeContract] = None
    precompute_artifacts: list[PrecomputeArtifactContract] = Field(default_factory=list, max_length=4)
    match_memory: Optional[MatchMemoryRuntimeContract] = None
    state_learning: Optional[StateLearningRuntimeContract] = None
    reference_pack_id: str = Field(default="", max_length=128)
    official_feedback_refs: list[str] = Field(default_factory=list, max_length=8)
    forbidden_runtime_work: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("reference_pack_id")
    @classmethod
    def _trim_reference_pack_id(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _bind_work_primitive_to_local_reference_card(self):
        """Keep weak planners on a versioned, executable recipe.

        Profile dimensions and line controls already have their own closed
        evidence contracts.  The two work primitives are otherwise broad
        enough that a model can mislabel a dead/static table as an innovation,
        so each must name one source-controlled local reference card.
        """
        state_learning = self.state_learning
        primary = (
            state_learning.primary_innovation()
            if state_learning is not None
            else ""
        )
        if state_learning is not None and state_learning.work_primitive is not None:
            from strategy_reference_pack import validate_reference_selection

            errors = validate_reference_selection(self.reference_pack_id, primary)
            if errors:
                raise ValueError("; ".join(errors))
        elif self.reference_pack_id:
            raise ValueError(
                "reference_pack_id is only valid for a work-primitive "
                "state_learning primary"
            )
        return self


def runtime_contract_worker_prompt_terms(contract: RuntimeContract) -> tuple[str, ...]:
    """Return the literal execution terms a worker prompt must mirror."""
    populated_sections: list[str] = []
    if contract.policy_abi is not None:
        populated_sections.append("policy_abi")
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
    if contract.state_learning is not None:
        terms.extend(
            STATE_LEARNING_PRIMARY_PROMPT_TERMS[
                contract.state_learning.primary_innovation()
            ]
        )
        if contract.state_learning.work_primitive is not None:
            from strategy_reference_pack import get_reference_card

            card = get_reference_card(contract.reference_pack_id)
            if card is not None:
                terms.extend(card.required_worker_terms)
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
            "- each task writable scope is exactly [\"policy.py\"]; candidate helper "
            "modules/assets are forbidden. Every system runtime artifact owner_file must "
            "also appear in target_files/files_allowed (writable) or "
            "read_only_dependencies (context only; never writable)."
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
            f'prior_rule="{MATCH_MEMORY_PRIOR_RULES[0]}"; '
            f'confidence_rule="{MATCH_MEMORY_CONFIDENCE_RULES[0]}"; '
            f"consumer must be one of {list(MATCH_MEMORY_CONSUMERS)}; "
            f"update_events must contain {MATCH_MEMORY_MIN_UPDATE_EVENTS}.."
            f"{MATCH_MEMORY_MAX_UPDATE_EVENTS} unique values chosen only from "
            f"[{allowed_events}], and must include [{required_events}]."
        ),
        (
            "- state_learning: declare exactly one primary innovation across "
            f"work_primitive={list(STATE_LEARNING_WORK_PRIMITIVES)}, "
            f"profile_dimensions={list(STATE_LEARNING_PROFILE_DIMENSIONS)}, or "
            f"line_controls={list(STATE_LEARNING_LINE_CONTROLS)}; oracle_refs must "
            f"equal {list(STATE_LEARNING_ORACLE_REFS)}."
        ),
        (
            "- a state_learning work primitive must declare reference_pack_id "
            "from the source-controlled local strategy cards; profile dimensions "
            "and line controls must leave reference_pack_id empty. Foundation-only "
            "tables never satisfy this requirement by themselves."
        ),
        (
            "- policy_abi is closed: module=policy.py; entrypoints="
            f"{list(POLICY_ENTRYPOINTS)}; context schema version="
            f"{POLICY_CONTEXT_SCHEMA_VERSION}; ordered fields="
            f"{list(POLICY_CONTEXT_TOP_LEVEL_FIELDS)}; intents="
            f"{list(POLICY_INTENT_KINDS)} with raise.raise_to. The socket owner alone "
            "maps pass to call/check and serializes raw TCP."
        ),
    ]
    for primary, terms in STATE_LEARNING_PRIMARY_PROMPT_TERMS.items():
        lines.append(
            f"- state_learning primary {primary!r} requires worker_prompt terms: "
            + ", ".join(f'\"{term}\"' for term in terms)
            + "."
        )
        lines.append(
            f"- state_learning primary {primary!r} requires checks_required: "
            + ", ".join(
                f'\"{check}\"' for check in STATE_LEARNING_PRIMARY_CHECKS[primary]
            )
            + "."
        )
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
    skill_layer: str = Field(min_length=1, description="Primary strategy/protocol layer: preflop_range, texture, spr, blocker, line_template, protocol, native_tcp, telemetry, etc.")
    files_allowed: list[str] = Field(default_factory=list)
    read_only_dependencies: list[str] = Field(
        default_factory=list,
        description=(
            "Runtime-contract owner files the worker may inspect but must not edit; "
            "this field never expands the write boundary."
        ),
    )
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
        focus_id = self.architecture_focus_id.strip()
        writable_scope = {
            str(item).replace("\\", "/").rsplit("/", 1)[-1]
            for item in [*self.target_files, *self.files_allowed]
            if str(item).strip()
        }
        if writable_scope != {"policy.py"}:
            raise ValueError(
                "national_tcp_policy_v1 writable scope must be exactly ['policy.py']; "
                f"got {sorted(writable_scope)}. System files, helpers, and assets are read-only/forbidden."
            )

        required_sections = runtime_contract_required_sections(
            self.skill_layer,
            focus_id,
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

        invalid_precompute_owners = sorted({
            item.owner_file
            for item in contract.precompute_artifacts
            if item.owner_file != "precompute.py"
        })
        if invalid_precompute_owners:
            raise ValueError(
                "precompute artifacts must be existing system-owned precompute.py "
                f"objects, got owners {invalid_precompute_owners}"
            )
        if (
            contract.match_memory is not None
            and contract.match_memory.owner_file != "national_bot.py"
        ):
            raise ValueError(
                "match memory is reducer-owned in system national_bot.py; "
                f"got {contract.match_memory.owner_file!r}"
            )

        state_learning = contract.state_learning
        if state_learning is not None:
            missing_checks = sorted(
                set(state_learning.primary_checks()).difference(self.checks_required)
            )
            if missing_checks:
                raise ValueError(
                    "state_learning primary innovation requires checks_required "
                    f"to include {missing_checks}"
                )
            if (
                state_learning.work_primitive == "sample_counted_candidate_batch"
                and contract.decision is None
            ):
                raise ValueError(
                    "sample_counted_candidate_batch requires a decision contract"
                )
            if state_learning.work_primitive is not None:
                from strategy_reference_pack import validate_reference_task

                reference_errors = validate_reference_task(
                    contract.reference_pack_id,
                    state_learning.primary_innovation(),
                    target_files=[*self.target_files, *self.files_allowed],
                    worker_prompt=self.worker_prompt,
                )
                if reference_errors:
                    raise ValueError("; ".join(reference_errors))

        read_only_scope = {
            str(item).replace("\\", "/").rsplit("/", 1)[-1]
            for item in self.read_only_dependencies
            if str(item).strip()
        }
        overlap = sorted(writable_scope.intersection(read_only_scope))
        if overlap:
            raise ValueError(
                f"read_only_dependencies overlap writable target_files/files_allowed: {overlap}"
            )
        owners: list[str] = []
        if contract.match_memory is not None:
            owners.append(contract.match_memory.owner_file)
        owners.extend(item.owner_file for item in contract.precompute_artifacts)
        missing_owners = sorted({
            owner
            for owner in owners
            if owner not in writable_scope and owner not in read_only_scope
        })
        if missing_owners:
            raise ValueError(
                f"runtime_contract owner file(s) {missing_owners} are outside "
                "the declared writable/read-only scope: "
                f"writable={sorted(writable_scope)}, read_only={sorted(read_only_scope)}"
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
    measurement_plan: str = Field(min_length=20)
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

        focus_tasks = [
            task
            for task in self.tasks
            if task.architecture_focus_id.strip()
            == NATIONAL_POLICY_FOCUS_ID
        ]
        primary_tasks = [
            task
            for task in self.tasks
            if task.runtime_contract is not None
            and task.runtime_contract.state_learning is not None
        ]
        if len(primary_tasks) > 1:
            raise ValueError(
                "exactly one state_learning primary is allowed across the entire generation"
            )
        if focus_tasks and len(primary_tasks) != 1:
            raise ValueError(
                f"{NATIONAL_POLICY_FOCUS_ID} requires exactly one "
                "state_learning primary across the entire generation"
            )
        if focus_tasks and primary_tasks[0] not in focus_tasks:
            raise ValueError(
                "the generation-level state_learning primary must belong to the "
                f"{NATIONAL_POLICY_FOCUS_ID} focus task"
            )
        return self


class ReviewResult(BaseModel):
    approved: bool
    feedback: str = ""
    quality_score: int = Field(ge=1, le=10)
    change_summary: str = ""
    risk_areas: list[str] = []


class Evidence(BaseModel):
    h2h_weaknesses: list[str] = []
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


class StagnationResult(BaseModel):
    is_stagnant: bool
    confidence: str = "medium"
    recommendation: str = "continue"
    branch_from: Optional[str] = None
    reason: str = ""


class CombinedAnalystResult(BaseModel):
    is_stagnant: bool = False
    confidence: Literal["low", "medium", "high"] = "low"
    trend: Literal["improving", "stagnant", "declining", "unknown"] = Field(
        default="stagnant",
        description="improving, stagnant, declining, or unknown",
    )
    diversity_needed: bool = False
    diversity_reason: Optional[str] = None
    recommendation: Literal[
        "continue", "crossover", "branch", "branch_from", "force_exploration"
    ] = "continue"
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
    evidence_alignment: str = "aligned"  # aligned, misaligned, unrelated
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

    @field_validator("files_to_take_from_a", "files_to_take_from_b")
    @classmethod
    def _policy_only_crossover_files(cls, value: list[str]) -> list[str]:
        normalized = [str(item).strip() for item in value]
        if any(item != "policy.py" for item in normalized):
            raise ValueError(
                "national_tcp_policy_v1 crossover may select only policy.py"
            )
        return list(dict.fromkeys(normalized))


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
    "combined_analyst": CombinedAnalystResult,
    # Audit agents
    "master_plan_auditor": MasterPlanAuditResult,
    "worker_cot_checker": WorkerCoTCheckResult,
    "degeneration_diagnosis": DegenerationDiagnosis,
    "crossover_compatibility": CrossoverCompatibilityResult,
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
