import type { AgentWorkerFailureRow } from "../api/types.js";
import type { ControlPipelineHealth } from "../api/control.js";

export type FailureClass =
  | "worker"
  | "gate"
  | "infrastructure"
  | "terminal_gate"
  | "checkpoint_epoch_incompatible"
  | "recovery_blocked"
  | "unknown";

export type RecoveryDisposition =
  | "auto_retry"
  | "awaiting_lease"
  | "needs_repair"
  | "authority_conflict"
  | "operator_action"
  | "terminal";

export interface FailureRecoveryRow {
  /** Stable key for React lists. */
  key: string;
  failureClass: FailureClass;
  ownerStage: string | null;
  ownerTool: string | null;
  /** Free-form error/reason text the dashboard renders verbatim. */
  detail: string;
  /** Retry count when derivable from worker failure or infra overlay. */
  retry: { attempt: number | null; max: number | null };
  disposition: RecoveryDisposition;
  /** Human-readable Chinese explanation of the disposition. */
  dispositionLabel: string;
}

const INFRA_DISPOSITION_MAX = 3;

function dispositionFromInfra(
  overlay: Record<string, unknown> | null | undefined,
): { disposition: RecoveryDisposition; retry: { attempt: number | null; max: number | null } } {
  if (!overlay) {
    return { disposition: "operator_action", retry: { attempt: null, max: null } };
  }
  const action = typeof overlay.action === "string" ? overlay.action : null;
  const attempt = typeof overlay.attempt === "number" ? overlay.attempt : null;
  const max = typeof overlay.max_attempts === "number" ? overlay.max_attempts : null;
  const exhausted = overlay.exhausted === true;
  if (exhausted || action === "abandon_generation") {
    return { disposition: "terminal", retry: { attempt, max } };
  }
  if (action === "retry_same_tool") {
    return { disposition: "auto_retry", retry: { attempt, max } };
  }
  return { disposition: "operator_action", retry: { attempt, max } };
}

/**
 * Project worker-failure rows from the agent activity endpoint into the
 * Failures & Recovery view.  Each row keeps the backend's own classification;
 * the dashboard never re-derives a failure class from free text.
 */
export function workerFailureRows(
  failures: AgentWorkerFailureRow[],
): FailureRecoveryRow[] {
  return failures.map((row, index) => {
    const failureClass: FailureClass = row.category === "gate" ? "gate" : "worker";
    return {
      key: `worker-${row.worker_id ?? index}-${row.gen ?? "n"}-${index}`,
      failureClass,
      ownerStage: null,
      ownerTool: null,
      detail: row.error ?? "(无错误描述)",
      retry: { attempt: null, max: null },
      disposition: "needs_repair",
      dispositionLabel: "Worker 失败已记录；编排器按受控重试策略处理。",
    };
  });
}

/**
 * Project the health.pipeline recovery overlay + infra_failure + terminal
 * gate outcome into Failures & Recovery rows.  These are the authority-side
 * recovery signals; worker-failure rows are separate.
 */
export function pipelineRecoveryRows(
  pipeline: ControlPipelineHealth | null | undefined,
  infraFailure: Record<string, unknown> | null | undefined,
): FailureRecoveryRow[] {
  const rows: FailureRecoveryRow[] = [];
  if (!pipeline) return rows;

  const recovery = pipeline.recovery;
  if (recovery && Array.isArray(recovery.issues) && recovery.issues.length > 0) {
    rows.push({
      key: "pipeline-recovery",
      failureClass: pipeline.recovery_blocked ? "recovery_blocked" : "unknown",
      ownerStage: pipeline.stage ?? null,
      ownerTool: null,
      detail: recovery.issues.join("; "),
      retry: { attempt: null, max: null },
      disposition: pipeline.recovery_blocked ? "operator_action" : "needs_repair",
      dispositionLabel: pipeline.recovery_blocked
        ? "checkpoint 恢复被阻断；需要操作员介入。"
        : "checkpoint 有恢复问题；编排器按策略处理。",
    });
  }

  if (infraFailure && typeof infraFailure === "object" && Object.keys(infraFailure).length > 0) {
    const { disposition, retry } = dispositionFromInfra(infraFailure);
    const component = typeof infraFailure.component === "string" ? infraFailure.component : "infrastructure";
    const code = typeof infraFailure.code === "string" ? infraFailure.code : "unknown";
    const ownerTool = typeof infraFailure.owner_tool === "string" ? infraFailure.owner_tool : null;
    const resumeStage = typeof infraFailure.resume_stage === "string" ? infraFailure.resume_stage : null;
    rows.push({
      key: `infra-${component}-${code}`,
      failureClass: "infrastructure",
      ownerStage: resumeStage,
      ownerTool,
      detail: `${component} infra 失败：${code}`,
      retry: { attempt: retry.attempt, max: retry.max ?? INFRA_DISPOSITION_MAX },
      disposition,
      dispositionLabel: infraDispositionLabel(disposition, retry.attempt, retry.max ?? INFRA_DISPOSITION_MAX),
    });
  }

  const outcome = pipeline.gate_outcome;
  if (outcome && typeof outcome === "object") {
    const reason = typeof outcome.reason_code === "string" ? outcome.reason_code : "unknown";
    const gateName = typeof outcome.gate_name === "string" ? outcome.gate_name : "unknown";
    rows.push({
      key: `terminal-${gateName}-${outcome.receipt_digest ?? "no-receipt"}`,
      failureClass: "terminal_gate",
      ownerStage: outcome.terminal_stage ?? null,
      ownerTool: gateName,
      detail: `终局 ${gateName} gate 拒绝：${reason}（受控放弃收据已生成）`,
      retry: { attempt: null, max: null },
      disposition: "terminal",
      dispositionLabel: "终局 gate 拒绝；代次只能经权威 abandon 收据结束。",
    });
  }

  if (pipeline.identity_mismatches && pipeline.identity_mismatches.length > 0) {
    rows.push({
      key: "identity-conflict",
      failureClass: "checkpoint_epoch_incompatible",
      ownerStage: pipeline.stage ?? null,
      ownerTool: null,
      detail: `checkpoint 身份字段不一致：${pipeline.identity_mismatches.join(", ")}`,
      retry: { attempt: null, max: null },
      disposition: "authority_conflict",
      dispositionLabel: "权威冲突；不能从 stage 猜测下一工具。",
    });
  }

  return rows;
}

function infraDispositionLabel(
  disposition: RecoveryDisposition,
  attempt: number | null,
  max: number,
): string {
  switch (disposition) {
    case "auto_retry":
      return `基础设施自动重试${attempt != null ? `（第 ${attempt}/${max} 次）` : ""}；候选字节不变。`;
    case "terminal":
      return "基础设施重试已耗尽；代次进入受控放弃流程。";
    case "operator_action":
      return "等待操作员介入或租约恢复。";
    default:
      return "等待恢复。";
  }
}
