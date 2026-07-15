import type { PipelineCheckpoint } from "./types.js";

type JsonObject = Record<string, unknown>;

const isObject = (value: unknown): value is JsonObject => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);

const isPositiveInteger = (value: unknown): value is number => (
  typeof value === "number" && Number.isSafeInteger(value) && value > 0
);

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
  ) {
    throw new Error("pipeline checkpoint is structurally incomplete");
  }
  return value as unknown as PipelineCheckpoint;
}
