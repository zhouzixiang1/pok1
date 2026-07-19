import type {
  OfficialCertificationJob,
  OfficialCertificationJobsProjection,
} from "./types.js";
import type { ActiveGeneration } from "./control.js";

type JsonObject = Record<string, unknown>;

export const OFFICIAL_CERTIFICATION_STAGES = [
  "official_bootstrap_required",
  "official_certifying",
  "official_failed",
  "official_inconclusive",
] as const;

const OFFICIAL_CERTIFICATION_STAGE_SET: ReadonlySet<string> = new Set(
  OFFICIAL_CERTIFICATION_STAGES,
);

export function isOfficialCertificationStage(stage: string | null | undefined): boolean {
  return typeof stage === "string" && OFFICIAL_CERTIFICATION_STAGE_SET.has(stage);
}

export function isNormalOfficialCertificationStage(stage: string | null | undefined): boolean {
  return stage !== "official_bootstrap_required" && isOfficialCertificationStage(stage);
}
const isObject = (value: unknown): value is JsonObject => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);
const isInteger = (value: unknown): value is number => (
  typeof value === "number" && Number.isSafeInteger(value)
);

function isJob(value: unknown): value is OfficialCertificationJob {
  if (!isObject(value)) return false;
  if (
    typeof value.job_id !== "string"
    || !/^[0-9a-f]{64}$/.test(value.job_id)
    || !["created", "queued", "starting", "running", "finalizing", "cancel_requested", "completed", "failed", "cancelled"].includes(String(value.state))
    || typeof value.workflow_run_id !== "string"
    || value.workflow_run_id.length === 0
    || !isInteger(value.candidate_version)
    || value.evaluation_epoch !== "national_tcp_policy_v1"
    || value.epoch_initialized !== true
    || value.formal_policy_id !== "official-full-v5"
    || value.formal_mode !== "full"
    || !["pipeline_attached_full_v5_job", "operator_bootstrap_full_v5_job"].includes(String(value.formal_authority))
  ) return false;
  if (value.progress !== undefined) {
    const progress = value.progress;
    if (
      !isObject(progress)
      || ![progress.suite_attempt, progress.rounds_requested, progress.rounds_completed, progress.rounds_passed].every(isInteger)
      || !Array.isArray(progress.rounds)
    ) return false;
  }
  return true;
}

export function expectOfficialCertificationJobs(
  value: unknown,
): OfficialCertificationJobsProjection {
  if (
    !isObject(value)
    || value.schema_version !== 1
    || value.evaluation_epoch !== "national_tcp_policy_v1"
    || typeof value.epoch_state !== "string"
    || typeof value.epoch_initialized !== "boolean"
    || value.formal_policy_id !== "official-full-v5"
    || value.formal_mode !== "full"
    || !isInteger(value.pending)
    || !isInteger(value.running)
    || !Array.isArray(value.jobs)
    || !value.jobs.every(isJob)
    || !(value.workflow_run_id === null || typeof value.workflow_run_id === "string")
    || !(value.candidate_version === null || isInteger(value.candidate_version))
    || !(value.next_v === null || isInteger(value.next_v))
    || !(value.source_v === null || isInteger(value.source_v))
    || !(value.parent2_v === null || isInteger(value.parent2_v))
    || !(value.checkpoint_stage === null || typeof value.checkpoint_stage === "string")
    || !(value.checkpoint_revision === null || isInteger(value.checkpoint_revision))
    || !(value.run_id === null || typeof value.run_id === "string")
    || !(value.operator_transition === undefined || value.operator_transition === null || isObject(value.operator_transition))
  ) {
    throw new Error("official certification jobs projection is structurally invalid");
  }
  const hasContext = typeof value.workflow_run_id === "string";
  const workflowRunId = value.workflow_run_id;
  if (
    hasContext
      ? (
          typeof workflowRunId !== "string"
          || workflowRunId.length === 0
          || !isInteger(value.candidate_version)
          || value.candidate_version !== value.next_v
          || !isInteger(value.next_v)
          || !(value.source_v === null || isInteger(value.source_v))
          || !(value.parent2_v === null || isInteger(value.parent2_v))
          || typeof value.checkpoint_stage !== "string"
          || value.checkpoint_stage.length === 0
          || !isInteger(value.checkpoint_revision)
          || value.checkpoint_revision <= 0
          || typeof value.run_id !== "string"
          || value.run_id.length === 0
        )
      : [value.candidate_version, value.next_v, value.source_v, value.parent2_v, value.checkpoint_stage, value.checkpoint_revision, value.run_id].some((field) => field !== null)
  ) {
    throw new Error("official certification jobs projection checkpoint identity is invalid");
  }
  return value as unknown as OfficialCertificationJobsProjection;
}

export function officialJobsBindingIssues(
  projection: OfficialCertificationJobsProjection,
  generation: ActiveGeneration | null | undefined,
): string[] {
  if (!generation) return ["active_generation"];
  const issues: string[] = [];
  if (projection.workflow_run_id !== generation.workflow_run_id) issues.push("workflow_run_id");
  if (projection.candidate_version !== generation.next_v) issues.push("candidate_version");
  if (projection.next_v !== generation.next_v) issues.push("next_v");
  if (projection.source_v !== generation.source_v) issues.push("source_v");
  if (projection.parent2_v !== generation.parent2_v) issues.push("parent2_v");
  if (projection.checkpoint_stage !== generation.stage) issues.push("checkpoint_stage");
  if (projection.checkpoint_revision !== generation.checkpoint_revision) issues.push("checkpoint_revision");
  if (projection.run_id !== generation.run_id) issues.push("run_id");
  if (projection.jobs.some((job) => job.workflow_run_id !== generation.workflow_run_id)) {
    issues.push("job_workflow_run_id");
  }
  if (projection.jobs.some((job) => job.candidate_version !== generation.next_v)) {
    issues.push("job_candidate_version");
  }
  return issues;
}
