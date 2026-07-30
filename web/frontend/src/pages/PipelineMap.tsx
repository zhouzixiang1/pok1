import { useControlStatusValue } from "../context/DataProvider";
import { useBoundAgentActivity } from "../hooks/useBoundAgentActivity";
import { usePipelineCheckpoint } from "../hooks/usePipelineCheckpoint";
import {
  controlPipelineBlocked,
  controlPipelineIssues,
  draftGenerations,
} from "../api/control";
import PageMeta from "../components/common/PageMeta";
import { EvolutionPageHeader } from "../components/evolution/EvolutionPageHeader";
import { PhaseAProjectionStrip } from "../components/evolution/PhaseAProjectionStrip";
import { HandoffEightStep } from "../components/evolution/HandoffEightStep";
import { PipelineDiagnostics } from "../components/evolution/PipelineDiagnostics";
import { PipelineStatus } from "../components/evolution/PipelineStatus";
import { OperatorSituation } from "../components/evolution/OperatorSituation";
import {
  EvolutionSection,
  EvolutionStatusBadge,
  EvolutionSurface,
} from "../components/evolution/ui";
import { agentActivityView } from "../domain/agentActivityView";
import { operatorSituationView } from "../domain/operatorSituationView";
import { pipelineRecoveryRows } from "../domain/failureRecoveryView";
import {
  PIPELINE_TIMEOUT_LEASES,
  isPipelineTimeoutLeaseStage,
} from "../constants/pipeline";

/**
 * Pipeline Map — sole full generation stepper + handoff eight-step + diagnostics.
 */
export default function PipelineMap() {
  const { status, health, loading, error } = useControlStatusValue();
  const { checkpoint } = usePipelineCheckpoint(5_000);
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
  const recoveryRows = pipelineRecoveryRows(
    pipeline,
    agentView && agentView.available ? agentView.infraFailure : null,
  );
  const route = pipeline?.route ?? null;
  const handoff = status?.post_publication_handoff ?? null;
  const situation = operatorSituationView(status, health);

  return (
    <div className="space-y-4">
      <PageMeta title="本代进度 — Bot 自进化" description="当前 Bot 从研发到发布的真实进度" />
      <EvolutionPageHeader
        title="本代进度"
        subtitle="唯一完整 generation stepper + 发布后交接八步"
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

      <EvolutionSurface>
        <EvolutionSection
          title="本代从研发到发布的进度"
          subtitle="只把真正通过的步骤标为完成；重试与认证等待不伪装成功"
        />
        <div className="mt-3">
          <PipelineStatus
            checkpoint={checkpoint}
            activeGeneration={status?.active_generation ?? null}
            drafts={draftGenerations(status)}
            pipelineMode={status?.pipeline_mode ?? null}
            route={route}
            handoff={handoff}
          />
        </div>
      </EvolutionSurface>

      <HandoffEightStep handoff={handoff} />

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <PipelineDiagnostics status={status} health={health} />

        <EvolutionSurface padding="sm">
          <EvolutionSection
            title="超时后的安全恢复"
            subtitle="只有真的超时时才出现；不计为成功进度"
          />
          <div className="mt-3 space-y-2">
            {isTimeout && activeStage ? (
              <TimeoutLeaseCard stage={activeStage} />
            ) : (
              <p className="text-xs text-gray-400">
                当前没有超时恢复任务。代次整体超时会受控结束本次尝试；基础设施超时只从原生预发布评测入口恢复。
              </p>
            )}
          </div>
        </EvolutionSurface>

        <EvolutionSurface padding="sm">
          <EvolutionSection
            title="当前异常如何处理"
            subtitle="自动重试、受控修复、结束本次尝试或人工介入"
          />
          <div className="mt-3 space-y-2">
            {issues.length === 0 && recoveryRows.length === 0 ? (
              <p className="text-xs text-success-600 dark:text-success-400">当前没有阻断流程的异常。</p>
            ) : (
              <>
                {issues.length > 0 && (
                  <ul className="ml-4 list-disc text-xs text-gray-600 dark:text-gray-300">
                    {issues.map((issue) => (
                      <li key={issue} className="font-mono">{issue}</li>
                    ))}
                  </ul>
                )}
                {recoveryRows.map((row) => (
                  <div
                    key={row.key}
                    className="border-l-2 border-error-300 pl-2 text-xs dark:border-error-700"
                  >
                    <div className="flex items-center gap-2">
                      <EvolutionStatusBadge
                        tone={
                          row.disposition === "terminal"
                            ? "error"
                            : row.disposition === "auto_retry"
                              ? "warn"
                              : "neutral"
                        }
                      >
                        {row.failureClass}
                      </EvolutionStatusBadge>
                      <span className="truncate text-gray-600 dark:text-gray-300">{row.detail}</span>
                    </div>
                    <p className="mt-0.5 text-gray-500">{row.dispositionLabel}</p>
                  </div>
                ))}
              </>
            )}
            {blocked && (
              <EvolutionStatusBadge tone="error">下一动作被安全阻断</EvolutionStatusBadge>
            )}
          </div>
        </EvolutionSurface>
      </div>
    </div>
  );
}

function TimeoutLeaseCard({ stage }: { stage: "timed_out" | "infra_timed_out" }) {
  const lease = PIPELINE_TIMEOUT_LEASES[stage];
  return (
    <div className="rounded-md border border-error-300 bg-error-50 p-3 dark:border-error-800 dark:bg-error-950/30">
      <div className="mb-1 flex items-center gap-2">
        <EvolutionStatusBadge tone="error">超时恢复</EvolutionStatusBadge>
        <span className="text-sm font-semibold text-error-700 dark:text-error-300">{lease.label}</span>
        <span className="font-mono text-xs text-gray-500">{stage}</span>
      </div>
      <p className="mb-2 text-xs text-error-700 dark:text-error-300">{lease.description}</p>
      <p className="text-xs text-gray-500">
        该状态不计入成功进度；页面会等待后端指定的安全恢复入口，而不是猜测下一步。
      </p>
      <div className="mt-1 font-mono text-xs text-gray-500">
        stage: {stage} · next_tool: {lease.nextTool}
      </div>
    </div>
  );
}
