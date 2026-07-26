"""Master Architect error types and advisory-analysis sentinels.

These are cross-layer control-flow signals shared between the Master runtime
(``agent_master.py``), the strict authority workflow, ``tool_planning``, and the
schema-validation code in ``agent_master_validation.py``.  They are deliberately
isolated from the validation module so callers can import them without pulling
in the full schema/parser dependency graph, and so the validation module name
does not mask error/sentinel ownership.

All symbols are re-exported by ``agent_master.py`` for backward compatibility.
"""

# Advisory-analysis sentinels.
# Explicit marker returned when an analyst LLM crashed on an infrastructure
# error (NOT a business judgement).  Consumers must distinguish this from empty
# text so a missing analysis is never read as a negative business signal.
LLM_INFRA_SENTINEL = "[LLM_INFRA_ERROR: analysis unavailable]"
LLM_INFRA_SENTINEL_MSG = (
    "⚠ Analysis unavailable: the LLM analyst crashed with an infrastructure "
    "error (NOT a business judgement). Treat conclusions in this section as "
    "missing rather than negative — the daemon data still exists, only the "
    "LLM interpretation failed."
)


class MasterInfrastructureError(RuntimeError):
    """The Master role produced no plan because its LLM transport failed."""

    def __init__(self, source_v, next_v, prompt_digest, issue):
        self.source_v = source_v
        self.next_v = next_v
        self.prompt_digest = prompt_digest
        self.issue = str(issue)[:500]
        super().__init__(self.issue)


class MasterEnsembleInfrastructureParked(MasterInfrastructureError):
    """One journaled Scout/Ballot is missing; preserve all accepted siblings."""

    def __init__(
        self,
        source_v,
        next_v,
        prompt_digest,
        issue,
        *,
        slot,
        retry_state,
    ):
        super().__init__(source_v, next_v, prompt_digest, issue)
        self.slot = str(slot)
        self.role_attempt = int((retry_state or {}).get("role_attempt") or 1)
        self.accepted_slots = tuple((retry_state or {}).get("accepted_slots") or ())
        self.pending_slots = tuple((retry_state or {}).get("pending_slots") or ())
        self.authority_run_id = str((retry_state or {}).get("run_id") or "")
        self.retry_after_sec = min(
            60.0,
            max(5.0, 5.0 * (2 ** min(self.role_attempt - 1, 4))),
        )
        self.needs_attention = self.role_attempt >= 3


class MasterAuthorityError(RuntimeError):
    """Deterministic checkpoint/evidence authority blocks provider dispatch."""

    def __init__(self, source_v, next_v, prompt_digest, errors):
        self.source_v = source_v
        self.next_v = next_v
        self.prompt_digest = prompt_digest
        self.errors = tuple(
            str(item)[:500]
            for item in (
                errors if isinstance(errors, (list, tuple)) else [errors]
            )
            if str(item)
        ) or ("master_authority_invalid",)
        self.issue = ";".join(self.errors)[:500]
        super().__init__(self.issue)
