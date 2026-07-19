import { useControlStatus } from "../hooks/useControlStatus";
import { useBoundAgentActivity } from "../hooks/useBoundAgentActivity";
import {
  controlPipelineBlocked,
  controlPipelineIssues,
} from "../api/control";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { PipelineStatus } from "../components/evolution/PipelineStatus";
import { OperatorSituation } from "../components/evolution/OperatorSituation";
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
  const { agents } = useBoundAgentActivity(
    status?.active_generation,
    status?.epoch_initialized === true,
  );

  const pipeline = health?.pipeline ?? null;
  const blocked = controlPipelineBlocked(pipeline);
  const issues = controlPipelineIssues(pipeline);
  const activeStage = status?.active_generation?.stage ?? null;
  const isTimeout = activeStage != null && isPipelineTimeoutLeaseStage(activeStage);
  const agentView = agents ? agentActivityView(agents, pipeline?.route) : null;
  const recoveryRows = pipelineRecoveryRows(pipeline, agentView && agentView.available ? agentView.infraFailure : null);
  const route = pipeline?.route ?? null;
  const handoff = status?.post_publication_handoff ?? null;
  const schedulerBoundary = pipeline?.scheduler_boundary ?? null;

  return (
    <>
      <PageMeta title="本代进度 — Bot 自进化" description="当前 Bot 从研发到发布的真实进度" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />
      <OperatorSituation status={status} health={health} className="mb-4" />

      <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1 mb-4">
        <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200 mb-2">本代从研发到发布的进度</h2>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          只把真正通过的步骤标为完成。重试、修复和认证等待会停在正在处理的步骤，不会伪装成成功。
        </p>
        {/* Linear stepper from the shared PipelineStatus component.  We pass
            checkpoint=null because the agent-activity projection is not the
            independent checkpoint shape; PipelineStatus then renders the
            authoritative active_generation stage directly. */}
        <PipelineStatus
          checkpoint={null}
          activeGeneration={status?.active_generation ?? null}
          route={route}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Timeout lease panel */}
        <Card>
          <CardHeader title="超时后的安全恢复" subtitle="只有真的超时时才出现；不计为成功进度" />
          <div className="p-4 space-y-2">
            {isTimeout && activeStage ? (
              <TimeoutLeaseCard stage={activeStage} />
            ) : (
              <p className="text-xs text-gray-400">
                当前没有超时恢复任务。代次整体超时会受控结束本次尝试；基础设施超时只从原生预发布评测入口恢复。
              </p>
            )}
          </div>
        </Card>

        {/* Route / next action */}
        <Card>
          <CardHeader title="系统准备做什么" subtitle="由状态机给出的唯一下一动作" />
          <div className="p-4 space-y-2 text-xs">
            {blocked ? (
              <Badge variant="error">下一动作被安全阻断</Badge>
            ) : route ? (
              <>
                {route.directive && (
                  <p className="rounded-lg bg-gray-50 p-2 text-gray-700 dark:bg-white/[0.03] dark:text-gray-200">{route.directive}</p>
                )}
                <details className="text-gray-500 dark:text-gray-400">
                  <summary className="cursor-pointer font-medium">查看原始 route</summary>
                  <div className="mt-2 space-y-1 font-mono">
                    <div>next_tool: {route.next_tool ?? "none"}</div>
                    <div>intent: {route.intent}</div>
                    {route.failure_class && <div>failure_class: {route.failure_class}</div>}
                    <div>allowed_tools: {route.allowed_tools.join(", ") || "none"}</div>
                  </div>
                </details>
              </>
            ) : (
              <p className="text-gray-400">当前没有活跃代次；等待外层调度器准备下一代。</p>
            )}
            {schedulerBoundary && (
              <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-800">
                <span className="text-gray-500">下一代准备权：</span>
                <span className="font-mono text-gray-800 dark:text-gray-200">
                  {schedulerBoundary.state} → {schedulerBoundary.scheduler_action}
                </span>
              </div>
            )}
          </div>
        </Card>

        {/* Recovery / failures */}
        <Card>
          <CardHeader title="当前异常如何处理" subtitle="自动重试、受控修复、结束本次尝试或人工介入" />
          <div className="p-4 space-y-2">
            {issues.length === 0 && recoveryRows.length === 0 ? (
              <p className="text-xs text-success-600 dark:text-success-400">当前没有阻断流程的异常。</p>
            ) : (
              <>
                {issues.length > 0 && (
                  <div className="text-xs">
                    <span className="text-gray-500">权威诊断：</span>
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
          <CardHeader title="发布后的收尾" subtitle="提交、tag 完成后归档并把准备权交给下一代" />
          <div className="p-4 space-y-1 text-xs">
            {!handoff || handoff.status === "none" ? (
              <p className="text-gray-400">当前没有发布后收尾任务。</p>
            ) : (
              <>
                <div>
                  <span className="text-gray-500">收尾状态：</span>
                  <span className="font-mono text-gray-800 dark:text-gray-200">{handoff.status}</span>
                  <Badge variant={handoff.blocked ? "error" : "warning"} size="sm" className="ml-2">
                    {handoff.blocked ? "blocked" : handoff.state ?? ""}
                  </Badge>
                </div>
                <details className="text-gray-500 dark:text-gray-400">
                  <summary className="cursor-pointer font-medium">查看收尾身份</summary>
                  <div className="mt-2 space-y-1 font-mono">
                    <div>owner_scope: {handoff.owner_scope}</div>
                    <div>record_revision: {handoff.record_revision}</div>
                    <div>projection_digest: {handoff.projection_digest?.slice(0, 12)}…</div>
                  </div>
                </details>
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
        <Badge variant="error" size="sm">超时恢复</Badge>
        <span className="text-sm font-semibold text-error-700 dark:text-error-300">{lease.label}</span>
        <span className="font-mono text-xs text-gray-500">{stage}</span>
      </div>
      <p className="text-xs text-error-700 dark:text-error-300 mb-2">{lease.description}</p>
      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
        该状态不计入成功进度；页面会等待后端指定的安全恢复入口，而不是猜测下一步。
      </p>
      <details className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        <summary className="cursor-pointer font-medium">查看恢复入口</summary>
        <div className="mt-1 font-mono">stage: {stage} · next_tool: {lease.nextTool}</div>
      </details>
    </div>
  );
}
