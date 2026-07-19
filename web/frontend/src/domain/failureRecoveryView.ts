import type { AgentWorkerFailureRow } from "../api/types.js";
import type { ControlPipelineHealth } from "../api/control.js";

export type FailureClass =
  | "worker"
  | "gate"
  | "infrastructure"
  | "timeout_lease"
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
  | "historical"
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
    const failureClass: FailureClass = row.category === "gate"
      ? "gate"
      : row.category === "worker"
        ? "worker"
        : "unknown";
    return {
      key: `worker-${row.worker_id ?? index}-${row.gen ?? "n"}-${index}`,
      failureClass,
      ownerStage: null,
      ownerTool: null,
      detail: row.error ?? "(无错误描述)",
      retry: { attempt: null, max: null },
      disposition: "historical",
      dispositionLabel: "这是当前 workflow 的历史失败记录，不表示故障仍在发生；当前动作只以 health route 为准。",
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

  if (pipeline.stage === "timed_out" || pipeline.stage === "infra_timed_out") {
    const expectedTool = pipeline.stage === "timed_out"
      ? "abandon_generation"
      : "run_precommit_eval";
    const route = pipeline.route;
    const routeBound = route?.stage === pipeline.stage
      && route.next_v === pipeline.next_v
      && route.source_v === pipeline.source_v
      && route.parent2_v === pipeline.parent2_v
      && route.next_tool === expectedTool;
    rows.push({
      key: `timeout-lease-${pipeline.stage}`,
      failureClass: "timeout_lease",
      ownerStage: pipeline.stage,
      ownerTool: routeBound ? expectedTool : null,
      detail: pipeline.stage === "timed_out"
        ? "代次超时租约已落盘；它只能由当前 owner 生成 canonical abandon 收据。"
        : "基础设施超时租约已落盘；候选字节不变，只能从原生预提交入口恢复。",
      retry: { attempt: null, max: null },
      disposition: "awaiting_lease",
      dispositionLabel: routeBound
        ? `等待当前租约 owner 执行 ${expectedTool}；不会静默等待或重放任意旧阶段。`
        : "租约存在，但配对 route 未证明唯一恢复工具；页面拒绝猜测，由权威诊断阻断。",
    });
  }

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
        ? "持久状态无法安全恢复；需要操作员按权威诊断处理。"
        : "持久状态存在可恢复问题；编排器会按当前 route 处理。",
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
      detail: `${component} 基础设施异常：${code}`,
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
      detail: `${gateName} 门终局拒绝本次尝试：${reason}（受控结束收据已生成）`,
      retry: { attempt: null, max: null },
      disposition: "terminal",
      dispositionLabel: "本次 workflow 不能继续；状态机只能依据该收据结束它，再创建独立 successor。",
    });
  }

  if (pipeline.identity_mismatches && pipeline.identity_mismatches.length > 0) {
    rows.push({
      key: "identity-conflict",
      failureClass: "checkpoint_epoch_incompatible",
      ownerStage: pipeline.stage ?? null,
      ownerTool: null,
      detail: `持久状态与控制投影身份不一致：${pipeline.identity_mismatches.join(", ")}`,
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
      return "等待操作员按权威诊断介入。";
    case "awaiting_lease":
      return "等待已绑定 owner 的租约恢复动作。";
    default:
      return "等待恢复。";
  }
}
