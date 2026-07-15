import type { ActiveGeneration } from "../api/control.js";
import type { PipelineCheckpoint, PipelineGateResult } from "../api/types.js";

export function criticAdvisoryComplete(gate: PipelineGateResult): boolean {
  return gate.schema_valid === true
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

/** Exact fields shared by the independent checkpoint and paired control view. */
export function pipelineCheckpointIdentityIssues(
  checkpoint: PipelineCheckpoint,
  activeGeneration: ActiveGeneration,
): string[] {
  return [
    checkpoint.evaluation_epoch !== "national_tcp_policy_v1" ? "evaluation_epoch" : null,
    checkpoint.next_v !== activeGeneration.next_v ? "next_v" : null,
    checkpoint.source_v !== activeGeneration.source_v ? "source_v" : null,
    checkpoint.stage !== activeGeneration.stage ? "stage" : null,
    checkpoint.workflow_run_id !== activeGeneration.workflow_run_id ? "workflow_run_id" : null,
    checkpoint.run_id !== activeGeneration.run_id ? "run_id" : null,
    checkpoint.checkpoint_revision !== activeGeneration.checkpoint_revision ? "checkpoint_revision" : null,
  ].filter((value): value is string => value !== null);
}
