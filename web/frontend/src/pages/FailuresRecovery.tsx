import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AgentActivityResponse } from "../api/types";
import { useControlStatus } from "../hooks/useControlStatus";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader, EmptyState } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { agentActivityView } from "../domain/agentActivityView";
import {
  workerFailureRows,
  pipelineRecoveryRows,
  type FailureRecoveryRow,
  type RecoveryDisposition,
} from "../domain/failureRecoveryView";
import { cn } from "../lib/utils";

const DISPOSITION_VARIANT: Record<RecoveryDisposition, "success" | "warning" | "error" | "neutral" | "info"> = {
  auto_retry: "warning",
  awaiting_lease: "neutral",
  needs_repair: "warning",
  authority_conflict: "error",
  operator_action: "error",
  terminal: "error",
};

const DISPOSITION_LABEL: Record<RecoveryDisposition, string> = {
  auto_retry: "正在重试",
  awaiting_lease: "等待租约",
  needs_repair: "需要修复",
  authority_conflict: "权威冲突",
  operator_action: "需操作员",
  terminal: "终局",
};

/**
 * Failures & Recovery — every failure class and its disposition, paired so an
 * operator can tell "正在重试" from "等待租约", "需要修复", "权威冲突", and
 * terminal/abandon.  Never renders a single opaque spinner that hides these.
 */
export default function FailuresRecovery() {
  const { status, health, loading, error } = useControlStatus(5_000);
  const [agents, setAgents] = useState<AgentActivityResponse | null>(null);

  useEffect(() => {
    if (!status?.epoch_initialized) { setAgents(null); return; }
    let cancelled = false;
    const refresh = () => api.pipelineAgents().then((v) => { if (!cancelled) setAgents(v); }).catch((e) => {
      if (!cancelled) setAgents(null);
      console.error("[FailuresRecovery] agents error:", e);
    });
    refresh();
    const id = setInterval(refresh, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [status?.epoch_initialized]);

  const view = agents ? agentActivityView(agents) : null;
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

  return (
    <>
      <PageMeta title="失败与恢复 — Bot 自进化" description="失败分类与恢复动作" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />

      {!status?.epoch_initialized ? (
        <EmptyState message="epoch 未初始化；失败与恢复投影不可用。" />
      ) : (
        <div className="space-y-4">
          <Card>
            <CardHeader
              title="恢复状态概览"
              subtitle="区分正在重试 / 等待租约 / 需要修复 / 权威冲突 / 终局"
            />
            <div className="p-3 grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
              <Overview label="当前 stage" value={status.active_generation?.stage ?? "—"} mono />
              <Overview label="stage 持续" value={lastStageAge != null ? `${Math.round(lastStageAge)}s` : "—"} mono />
              <Overview label="worker 失败累计" value={String(view && view.available ? view.reworkCounts.worker_failure : 0)} />
              <Overview label="precommit rework" value={String(view && view.available ? view.reworkCounts.precommit : 0)} />
            </div>
          </Card>

          <Card>
            <CardHeader title="失败与恢复明细" subtitle="root cause · class · retry · disposition" />
            <div className="p-3 space-y-2">
              {allRows.length === 0 ? (
                <p className="text-xs text-success-600 dark:text-success-400">当前无活跃失败或恢复问题。</p>
              ) : (
                allRows.map((row) => (
                  <div key={row.key} className="rounded border border-gray-100 dark:border-gray-800 p-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="neutral" size="sm">{row.failureClass}</Badge>
                      <Badge variant={DISPOSITION_VARIANT[row.disposition]} size="sm">
                        {DISPOSITION_LABEL[row.disposition]}
                      </Badge>
                      {row.ownerStage && <span className="font-mono text-xs text-gray-500">stage：{row.ownerStage}</span>}
                      {row.ownerTool && <span className="font-mono text-xs text-gray-500">tool：{row.ownerTool}</span>}
                      {row.retry.attempt != null && (
                        <span className="font-mono text-xs text-gray-500">
                          retry：{row.retry.attempt}{row.retry.max != null ? `/${row.retry.max}` : ""}
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-gray-700 dark:text-gray-200 mt-1">{row.detail}</p>
                    <p className={cn(
                      "text-xs mt-1",
                      row.disposition === "terminal" || row.disposition === "authority_conflict" || row.disposition === "operator_action"
                        ? "text-error-600 dark:text-error-400"
                        : "text-gray-500 dark:text-gray-400",
                    )}>{row.dispositionLabel}</p>
                  </div>
                ))
              )}
            </div>
          </Card>

          {view && view.available && view.reviewerFeedback && (
            <Card>
              <CardHeader title="Reviewer 反馈" subtitle="advisory 上下文，非发布资格" />
              <div className="p-3">
                <p className="text-xs text-gray-600 dark:text-gray-300 whitespace-pre-wrap max-h-40 overflow-y-auto">
                  {view.reviewerFeedback}
                </p>
              </div>
            </Card>
          )}
        </div>
      )}
    </>
  );
}

function Overview({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between border-b border-gray-50 dark:border-gray-900 py-0.5">
      <span className="text-gray-500 dark:text-gray-400">{label}</span>
      <span className={cn("text-gray-800 dark:text-gray-200", mono && "font-mono")}>{value}</span>
    </div>
  );
}
