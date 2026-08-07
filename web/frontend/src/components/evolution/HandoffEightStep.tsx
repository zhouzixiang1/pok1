import type { PostPublicationHandoffStatus, HandoffStepProjection } from "../../api/control";
import {
  EvolutionSection,
  EvolutionStatusBadge,
  EvolutionStepperTrack,
  EvolutionSurface,
  type StepperStep,
} from "./ui";
import { cn } from "../../lib/utils";

/** Chinese labels for REQUIRED_STEPS (plan F4). */
export const HANDOFF_STEP_LABELS: Record<string, string> = {
  stability_observation: "稳定性观察",
  reap_signal: "回收信号",
  priority_eval: "优先评测",
  archive_rotation: "归档轮转",
  log_cleanup: "日志清理",
  pool_reap: "池回收",
  cycle_annotation: "周期标注",
  housekeeping: "管家收尾",
};

const HANDOFF_STEP_ORDER = [
  "stability_observation",
  "reap_signal",
  "priority_eval",
  "archive_rotation",
  "log_cleanup",
  "pool_reap",
  "cycle_annotation",
  "housekeeping",
] as const;

function toStepperStatus(
  step: HandoffStepProjection,
  handoffStatus: PostPublicationHandoffStatus["status"],
): StepperStep["status"] {
  if (handoffStatus === "blocked" && step.status !== "completed") return "blocked";
  if (step.status === "completed") return "completed";
  if (step.status === "running" || step.status === "planned") return "running";
  return "pending";
}

interface HandoffEightStepProps {
  handoff: PostPublicationHandoffStatus | null | undefined;
  className?: string;
}

/**
 * Post-publication handoff eight-step animated track.
 * Consumes only the whitelisted steps[] projection — never plan/receipt bodies.
 */
export function HandoffEightStep({ handoff, className }: HandoffEightStepProps) {
  if (!handoff || handoff.status === "none") {
    return (
      <EvolutionSurface className={cn(className)} padding="sm">
        <EvolutionSection
          title="发布后交接"
          subtitle="当前无活动 handoff；八步轨道在 pending/running/blocked 时出现"
        />
      </EvolutionSurface>
    );
  }

  const projected = handoff.steps?.length
    ? handoff.steps
    : HANDOFF_STEP_ORDER.map((id, i) => ({
        id,
        ordinal: i + 1,
        status: "pending" as const,
        plan_digest: null,
        receipt_digest: null,
        updated_at: null,
      }));

  const steps: StepperStep[] = projected.map((step) => ({
    id: step.id,
    label: HANDOFF_STEP_LABELS[step.id] ?? step.id,
    status: toStepperStatus(step, handoff.status),
    detail: step.plan_digest
      ? `plan ${step.plan_digest.slice(0, 8)}…`
      : undefined,
  }));

  return (
    <EvolutionSurface className={cn("space-y-3", className)} padding="sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <EvolutionSection
          title="发布后交接八步"
          subtitle={`v${handoff.version ?? "?"} · rev ${handoff.record_revision ?? "—"} · 已完成 ${handoff.completed_count ?? 0}/8`}
        />
        <EvolutionStatusBadge
          tone={
            handoff.status === "blocked"
              ? "error"
              : handoff.status === "running"
                ? "info"
                : "park"
          }
          pulse={handoff.status === "running"}
        >
          {handoff.status === "pending" ? "等待中"
            : handoff.status === "running" ? "进行中"
            : handoff.status === "blocked" ? "被阻断"
            : handoff.status}
          {handoff.current_step
            ? ` · ${HANDOFF_STEP_LABELS[handoff.current_step] ?? handoff.current_step}`
            : ""}
        </EvolutionStatusBadge>
      </div>
      <EvolutionStepperTrack steps={steps} />
      {handoff.issues.length > 0 && (
        <ul className="space-y-0.5 text-[11px] text-error-600 dark:text-error-400">
          {handoff.issues.slice(0, 8).map((issue) => (
            <li key={issue}>· {issue}</li>
          ))}
        </ul>
      )}
    </EvolutionSurface>
  );
}
