import type { PipelineCheckpoint } from "./types.js";

type JsonObject = Record<string, unknown>;

const isObject = (value: unknown): value is JsonObject => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);

const isPositiveInteger = (value: unknown): value is number => (
  typeof value === "number" && Number.isSafeInteger(value) && value > 0
);

const isDigest = (value: unknown): value is string => (
  typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
);

const validReviewAttemptJournal = (
  value: unknown,
  workflowRunId: unknown,
): boolean => {
  if (value === undefined) return true;
  if (!Array.isArray(value)) return false;
  return value.every((row) => (
    isObject(row)
    && row.schema_version === 1
    && row.kind === "pipeline-review-verdict-attempt-v1"
    && row.workflow_run_id === workflowRunId
    && (row.attempt === 1 || row.attempt === 2)
    && row.authority_slot === (row.attempt === 1 ? "review" : "review:retry")
    && typeof row.approved === "boolean"
    && isPositiveInteger(row.input_checkpoint_revision)
    && isDigest(row.cycle_digest)
    && isDigest(row.candidate_artifact_hash)
    && isDigest(row.quality_gate_digest)
    && isDigest(row.receipt_digest)
  ));
};

/**
 * Fail closed at the independent checkpoint-API boundary.
 *
 * The dashboard pairs this object with the checkpoint identity already frozen
 * into `/api/control/health`.  Accepting a missing revision here would allow an
 * old same-stage response to look current after a CAS update.
 */
export function expectPipelineCheckpoint(value: unknown): PipelineCheckpoint | null {
  if (value === null) return null;
  if (
    !isObject(value)
    || value.checkpoint_schema_version !== 2
    || value.evaluation_epoch !== "national_tcp_policy_v1"
    || !isPositiveInteger(value.checkpoint_revision)
    || !isPositiveInteger(value.next_v)
    || !(
      value.source_v === null
      || (isPositiveInteger(value.source_v) && value.source_v < value.next_v)
    )
    || typeof value.stage !== "string"
    || value.stage.trim().length === 0
    || typeof value.workflow_run_id !== "string"
    || value.workflow_run_id.trim().length === 0
    || typeof value.run_id !== "string"
    || value.run_id.trim().length === 0
    || !validReviewAttemptJournal(
      value.review_attempt_journal,
      value.workflow_run_id,
    )
  ) {
    throw new Error("pipeline checkpoint is structurally incomplete");
  }
  return value as unknown as PipelineCheckpoint;
}
