import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AgentActivityResponse } from "../api/types";
import { useControlStatus } from "../hooks/useControlStatus";
import {
  controlPipelineBlocked,
  controlPipelineIssues,
} from "../api/control";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { PipelineStatus } from "../components/evolution/PipelineStatus";
import { agentActivityView } from "../domain/agentActivityView";
import { pipelineRecoveryRows } from "../domain/failureRecoveryView";
import {
  PIPELINE_TIMEOUT_LEASES,
  isPipelineTimeoutLeaseStage,
} from "../constants/pipeline";

/**
 * Pipeline Map — a non-linear view of the generation state machine.
 *
 * Unlike the linear stepper inside PipelineStatus, this page makes the
 * side-paths explicit: same-stage retry, rewind/repair, timeout lease
 * (timed_out / infra_timed_out), abandon, post-publication handoff, and the
 * background strength fork/join.  Every signal comes from the paired
 * status/health observation or the agent-activity endpoint; the page never
 * re-derives a stage or route from a single field.
 */
export default function PipelineMap() {
  const { status, health, loading, error } = useControlStatus(5_000);
  const [agents, setAgents] = useState<AgentActivityResponse | null>(null);

  useEffect(() => {
    if (!status?.epoch_initialized) {
      setAgents(null);
      return;
    }
    let cancelled = false;
    const refresh = () => {
      api.pipelineAgents().then((value) => {
        if (!cancelled) setAgents(value);
      }).catch((e) => {
        if (!cancelled) setAgents(null);
        console.error("[PipelineMap] agents error:", e);
      });
    };
    refresh();
    const id = setInterval(refresh, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [status?.epoch_initialized]);

  const pipeline = health?.pipeline ?? null;
  const blocked = controlPipelineBlocked(pipeline);
  const issues = controlPipelineIssues(pipeline);
  const activeStage = status?.active_generation?.stage ?? null;
  const isTimeout = activeStage != null && isPipelineTimeoutLeaseStage(activeStage);
  const agentView = agents ? agentActivityView(agents) : null;
  const recoveryRows = pipelineRecoveryRows(pipeline, agentView && agentView.available ? agentView.infraFailure : null);
  const route = pipeline?.route ?? null;
  const handoff = status?.post_publication_handoff ?? null;
  const schedulerBoundary = pipeline?.scheduler_boundary ?? null;

  return (
    <>
      <PageMeta title="流水线地图 — Bot 自进化" description="非线性流水线状态图" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />

      <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1 mb-4">
        <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-2">主线流程</h2>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          线性 stepper 投影当前 milestone；失败/rework/transition 阶段映射到它们正在尝试完成的 milestone，不会被涂成已成功。
        </p>
        {/* Linear stepper from the shared PipelineStatus component.  We pass
            checkpoint=null because the agent-activity projection is not the
            independent checkpoint shape; PipelineStatus then renders the
            authoritative active_generation stage directly. */}
        <PipelineStatus
          checkpoint={null}
          activeGeneration={status?.active_generation ?? null}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Timeout lease panel */}
        <Card>
          <CardHeader title="超时恢复租约" subtitle="不在主线进度内的特殊 checkpoint 状态" />
          <div className="p-4 space-y-2">
            {isTimeout && activeStage ? (
              <TimeoutLeaseCard stage={activeStage} />
            ) : (
              <p className="text-xs text-gray-400">
                当前无超时租约。timed_out 走 abandon 重选；infra_timed_out 走受控 native 预提交恢复。
              </p>
            )}
          </div>
        </Card>

        {/* Route / next action */}
        <Card>
          <CardHeader title="权威 route / 下一动作" subtitle="来自 /api/control/health.pipeline" />
          <div className="p-4 space-y-2 text-xs">
            {blocked ? (
              <Badge variant="error">pipeline 恢复阻断</Badge>
            ) : route ? (
              <>
                <div>
                  <span className="text-gray-500">next_tool：</span>
                  <span className="font-mono text-gray-800 dark:text-gray-200">{route.next_tool ?? "(无)"}</span>
                </div>
                <div>
                  <span className="text-gray-500">intent：</span>
                  <span className="font-mono text-gray-800 dark:text-gray-200">{route.intent}</span>
                </div>
                {route.failure_class && (
                  <div>
                    <span className="text-gray-500">failure_class：</span>
                    <span className="font-mono text-error-600 dark:text-error-400">{route.failure_class}</span>
                  </div>
                )}
                {route.allowed_tools.length > 0 && (
                  <div>
                    <span className="text-gray-500">allowed_tools：</span>
                    <span className="font-mono text-gray-800 dark:text-gray-200">{route.allowed_tools.join(", ")}</span>
                  </div>
                )}
                {route.directive && (
                  <p className="text-gray-500 dark:text-gray-400 italic mt-2">{route.directive}</p>
                )}
              </>
            ) : (
              <p className="text-gray-400">无活跃 generation；外层调度器等待准备。</p>
            )}
            {schedulerBoundary && (
              <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-800">
                <span className="text-gray-500">scheduler_boundary：</span>
                <span className="font-mono text-gray-800 dark:text-gray-200">
                  {schedulerBoundary.state} → {schedulerBoundary.scheduler_action}
                </span>
              </div>
            )}
          </div>
        </Card>

        {/* Recovery / failures */}
        <Card>
          <CardHeader title="恢复 / 失败" subtitle="retry · repair · abandon · 权威冲突" />
          <div className="p-4 space-y-2">
            {issues.length === 0 && recoveryRows.length === 0 ? (
              <p className="text-xs text-success-600 dark:text-success-400">无活跃恢复问题。</p>
            ) : (
              <>
                {issues.length > 0 && (
                  <div className="text-xs">
                    <span className="text-gray-500">pipeline issues：</span>
                    <ul className="ml-4 list-disc text-gray-600 dark:text-gray-300">
                      {issues.map((issue) => (<li key={issue} className="font-mono">{issue}</li>))}
                    </ul>
                  </div>
                )}
                {recoveryRows.map((row) => (
                  <div key={row.key} className="text-xs border-l-2 border-error-300 dark:border-error-700 pl-2">
                    <div className="flex items-center gap-2">
                      <Badge variant={row.disposition === "terminal" ? "error" : row.disposition === "auto_retry" ? "warning" : "neutral"} size="sm">
                        {row.failureClass}
                      </Badge>
                      <span className="text-gray-600 dark:text-gray-300 truncate">{row.detail}</span>
                    </div>
                    <p className="text-gray-500 mt-0.5">{row.dispositionLabel}</p>
                  </div>
                ))}
              </>
            )}
          </div>
        </Card>

        {/* Post-publication handoff */}
        <Card>
          <CardHeader title="发布后 handoff" subtitle="post_publication_handoff_journal" />
          <div className="p-4 space-y-1 text-xs">
            {!handoff || handoff.status === "none" ? (
              <p className="text-gray-400">无活跃 handoff。</p>
            ) : (
              <>
                <div>
                  <span className="text-gray-500">status：</span>
                  <span className="font-mono text-gray-800 dark:text-gray-200">{handoff.status}</span>
                  <Badge variant={handoff.blocked ? "error" : "warning"} size="sm" className="ml-2">
                    {handoff.blocked ? "blocked" : handoff.state ?? ""}
                  </Badge>
                </div>
                <div><span className="text-gray-500">owner_scope：</span><span className="font-mono">{handoff.owner_scope}</span></div>
                <div><span className="text-gray-500">record_revision：</span><span className="font-mono">{handoff.record_revision}</span></div>
                <div><span className="text-gray-500">projection_digest：</span><span className="font-mono">{handoff.projection_digest?.slice(0, 12)}…</span></div>
                {handoff.issues.length > 0 && (
                  <ul className="ml-4 list-disc text-error-600 dark:text-error-400">
                    {handoff.issues.map((issue) => (<li key={issue} className="font-mono">{issue}</li>))}
                  </ul>
                )}
              </>
            )}
          </div>
        </Card>
      </div>
    </>
  );
}

function TimeoutLeaseCard({ stage }: { stage: "timed_out" | "infra_timed_out" }) {
  const lease = PIPELINE_TIMEOUT_LEASES[stage];
  return (
    <div className="rounded-lg border border-error-300 dark:border-error-800 bg-error-50 dark:bg-error-950/30 p-3">
      <div className="flex items-center gap-2 mb-1">
        <Badge variant="error" size="sm">超时租约</Badge>
        <span className="text-sm font-semibold text-error-700 dark:text-error-300">{lease.label}</span>
        <span className="font-mono text-xs text-gray-500">{stage}</span>
      </div>
      <p className="text-xs text-error-700 dark:text-error-300 mb-2">{lease.description}</p>
      <div className="text-xs">
        <span className="text-gray-500">恢复 next_tool：</span>
        <span className="font-mono text-gray-800 dark:text-gray-200">{lease.nextTool}</span>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
        该租约不计入成功流水线进度；不显示为"未知后端阶段"。
      </p>
    </div>
  );
}
