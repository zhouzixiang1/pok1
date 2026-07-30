import type { ActiveGeneration } from "../api/control.js";
import type { PipelineCheckpoint, PipelineGateResult } from "../api/types.js";
import { canonicalGenerationIdentityIssues } from "./canonicalGenerationIdentity.js";

export function criticAdvisoryComplete(gate: PipelineGateResult): boolean {
  return gate.approved === true
    && gate.schema_valid === true
    && gate.llm_invoked === true
    && gate.critic_llm_executed === true
    && gate.llm_failed !== true
    && gate.parse_failed !== true;
}

export function criticAdvisoryVerdict(gate: PipelineGateResult): string {
  if (gate.advisory_approved === true) return "建议支持";
  if (gate.advisory_approved === false) return "建议保留意见";
  return "建议结论不可用";
}

export function reviewerRetryPending(checkpoint: PipelineCheckpoint): boolean {
  const attempts = checkpoint.review_attempt_journal ?? [];
  const latest = attempts.length > 0 ? attempts[attempts.length - 1] : undefined;
  const qualityHash = checkpoint.gate_results?.quality?.code_fingerprint;
  return checkpoint.stage === "quality_passed"
    && latest?.attempt === 1
    && latest.approved === false
    && typeof qualityHash === "string"
    && latest.candidate_artifact_hash === qualityHash;
}

/**
 * Stage shown by the read-only Pipeline stepper.
 *
 * When the independent checkpoint poll is null (PipelineMap / partial
 * observation), fall back to ``active_generation.stage`` so the stepper still
 * tracks the authoritative control projection instead of hiding progress.
 */
export function pipelineStepperStage(
  checkpoint: Pick<PipelineCheckpoint, "stage"> | null | undefined,
  activeGeneration: Pick<ActiveGeneration, "stage"> | null | undefined,
): string | null {
  if (checkpoint != null && typeof checkpoint.stage === "string" && checkpoint.stage.length > 0) {
    return checkpoint.stage;
  }
  if (
    activeGeneration != null
    && typeof activeGeneration.stage === "string"
    && activeGeneration.stage.length > 0
  ) {
    return activeGeneration.stage;
  }
  return null;
}

/** Exact fields shared by the independent checkpoint and paired control view. */
export function pipelineCheckpointIdentityIssues(
  checkpoint: PipelineCheckpoint,
  activeGeneration: ActiveGeneration,
): string[] {
  return [
    checkpoint.evaluation_epoch !== "national_tcp_policy_v1" ? "evaluation_epoch" : null,
    checkpoint.next_v !== activeGeneration.next_v ? "next_v" : null,
    checkpoint.next_v !== activeGeneration.canonical_version ? "canonical_version" : null,
    checkpoint.source_v !== activeGeneration.source_v ? "source_v" : null,
    (checkpoint.parent2_v ?? null) !== activeGeneration.parent2_v ? "parent2_v" : null,
    checkpoint.stage !== activeGeneration.stage ? "stage" : null,
    checkpoint.workflow_run_id !== activeGeneration.workflow_run_id ? "workflow_run_id" : null,
    checkpoint.run_id !== activeGeneration.run_id ? "run_id" : null,
    checkpoint.checkpoint_revision !== activeGeneration.checkpoint_revision ? "checkpoint_revision" : null,
    ...canonicalGenerationIdentityIssues(activeGeneration)
      .map((issue) => `canonical_identity.${issue}`),
  ].filter((value): value is string => value !== null);
}
