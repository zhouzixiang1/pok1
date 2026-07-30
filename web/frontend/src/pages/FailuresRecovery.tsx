import { useControlStatusValue } from "../context/DataProvider";
import { useBoundAgentActivity } from "../hooks/useBoundAgentActivity";
import PageMeta from "../components/common/PageMeta";
import { EmptyState } from "../components/shared";
import { EvolutionPageHeader } from "../components/evolution/EvolutionPageHeader";
import { PhaseAProjectionStrip } from "../components/evolution/PhaseAProjectionStrip";
import { OperatorSituation } from "../components/evolution/OperatorSituation";
import {
  EvolutionSection,
  EvolutionStatusBadge,
  EvolutionSurface,
} from "../components/evolution/ui";
import type { EvolutionStatusTone } from "../components/evolution/ui";
import { agentActivityView } from "../domain/agentActivityView";
import {
  workerFailureRows,
  pipelineRecoveryRows,
  type FailureRecoveryRow,
  type RecoveryDisposition,
} from "../domain/failureRecoveryView";
import { operatorSituationView } from "../domain/operatorSituationView";
import { cn } from "../lib/utils";

const DISPOSITION_TONE: Record<RecoveryDisposition, EvolutionStatusTone> = {
  auto_retry: "warn",
  awaiting_lease: "neutral",
  needs_repair: "warn",
  authority_conflict: "error",
  operator_action: "error",
  historical: "neutral",
  terminal: "error",
};

const DISPOSITION_LABEL: Record<RecoveryDisposition, string> = {
  auto_retry: "正在重试",
  awaiting_lease: "等待租约",
  needs_repair: "需要修复",
  authority_conflict: "权威冲突",
  operator_action: "需操作员",
  historical: "历史记录",
  terminal: "终局",
};

/**
 * Failures & Recovery — every failure class and its disposition, paired so an
 * operator can tell "正在重试" from "等待租约", "需要修复", "权威冲突", and
 * terminal/abandon.  Never renders a single opaque spinner that hides these.
 */
export default function FailuresRecovery() {
  const { status, health, loading, error } = useControlStatusValue();
  const { agents } = useBoundAgentActivity(
    status?.active_generation,
    status?.epoch_initialized === true,
  );

  const view = agents ? agentActivityView(agents, health?.pipeline?.route) : null;
  const workerRows: FailureRecoveryRow[] = view && view.available
    ? workerFailureRows(view.workerFailures)
    : [];
  const recoveryRows = pipelineRecoveryRows(
    health?.pipeline ?? null,
    view && view.available ? view.infraFailure : null,
  );
  const allRows = [...recoveryRows, ...workerRows];
  const lastStageAge = health?.pipeline && typeof (health.pipeline as Record<string, unknown>).last_stage_age_sec === "number"
    ? (health.pipeline as Record<string, unknown>).last_stage_age_sec as number
    : null;
  const situation = operatorSituationView(status, health);

  return (
    <div className="space-y-4">
      <PageMeta title="异常与恢复 — Bot 自进化" description="出了什么问题、系统怎么处理、是否要人工介入" />
      <EvolutionPageHeader
        title="异常与恢复"
        subtitle="自动重试、租约等待、受控结束与人工介入分轨"
        status={status}
        health={health}
        loading={loading}
        error={error}
        variant="compact"
      />
      <PhaseAProjectionStrip
        status={status}
        manualRequired={situation?.manualRequired === true}
      />
      <OperatorSituation status={status} health={health} />

      {!status?.epoch_initialized ? (
        <EmptyState message="严格进化尚未初始化；当前没有可验证的异常与恢复记录。" />
      ) : (
        <>
          <EvolutionSurface padding="sm">
            <EvolutionSection
              title="异常处理概览"
              subtitle="自动重试、等待安全恢复、受控结束或需要人工，一眼区分"
            />
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
              <Overview label="当前内部阶段" value={status.active_generation?.stage ?? "—"} mono />
              <Overview label="本阶段持续时间" value={lastStageAge != null ? `${Math.round(lastStageAge)} 秒` : "—"} />
              <Overview label="本工作流累计历史失败记录" value={String(view && view.available ? view.reworkCounts.worker_failure : 0)} />
              <Overview label="原生预评测返工次数" value={String(view && view.available ? view.reworkCounts.precommit : 0)} />
            </div>
          </EvolutionSurface>

          <EvolutionSurface padding="sm">
            <EvolutionSection title="异常明细与处理结果" subtitle="先看处理方式；内部 class、stage 和 tool 保留供审计" />
            <div className="mt-3 space-y-2">
              {allRows.length === 0 ? (
                <p className="text-xs text-success-600 dark:text-success-400">当前没有阻断流程的异常。历史失败不会在这里伪装成仍在发生。</p>
              ) : (
                allRows.map((row) => (
                  <div key={row.key} className="rounded-md border border-gray-100 p-2 dark:border-gray-800">
                    <div className="flex flex-wrap items-center gap-2">
                      <EvolutionStatusBadge tone="neutral">{row.failureClass}</EvolutionStatusBadge>
                      <EvolutionStatusBadge tone={DISPOSITION_TONE[row.disposition]}>
                        {DISPOSITION_LABEL[row.disposition]}
                      </EvolutionStatusBadge>
                      {row.ownerStage && <span className="font-mono text-xs text-gray-500">内部阶段：{row.ownerStage}</span>}
                      {row.ownerTool && <span className="font-mono text-xs text-gray-500">负责工具：{row.ownerTool}</span>}
                      {row.retry.attempt != null && (
                        <span className="font-mono text-xs text-gray-500">
                          重试：{row.retry.attempt}{row.retry.max != null ? `/${row.retry.max}` : ""}
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-xs text-gray-700 dark:text-gray-200">{row.detail}</p>
                    <p className={cn(
                      "mt-1 text-xs",
                      row.disposition === "terminal" || row.disposition === "authority_conflict" || row.disposition === "operator_action"
                        ? "text-error-600 dark:text-error-400"
                        : "text-gray-500 dark:text-gray-400",
                    )}>{row.dispositionLabel}</p>
                  </div>
                ))
              )}
            </div>
          </EvolutionSurface>

          {view && view.available && view.reviewerFeedback && (
            <EvolutionSurface padding="sm">
              <EvolutionSection
                title="独立代码审核反馈"
                subtitle="审核结论是绑定门禁；这段文字只解释结论，不能单独证明通过"
              />
              <div className="mt-3">
                <p className="max-h-40 overflow-y-auto whitespace-pre-wrap text-xs text-gray-600 dark:text-gray-300">
                  {view.reviewerFeedback}
                </p>
              </div>
            </EvolutionSurface>
          )}
        </>
      )}
    </div>
  );
}

function Overview({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between border-b border-gray-50 py-0.5 dark:border-gray-900">
      <span className="text-gray-500 dark:text-gray-400">{label}</span>
      <span className={cn("text-gray-800 dark:text-gray-200", mono && "font-mono")}>{value}</span>
    </div>
  );
}
