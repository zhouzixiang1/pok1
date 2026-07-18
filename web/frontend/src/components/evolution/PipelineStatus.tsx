import { useState } from "react";
import type { MasterPlanTask, PipelineCheckpoint, PipelineGateResult } from "../../api/types";
import type { ActiveGeneration, PostPublicationHandoffStatus } from "../../api/control";
import {
  PIPELINE_STAGE_CONTRACT,
  PIPELINE_STAGES,
  PIPELINE_TIMEOUT_LEASES,
  STAGE_LABELS,
  STAGE_TO_MILESTONE,
  isPipelineTimeoutLeaseStage,
  type PipelineStage,
} from "../../constants/pipeline";
import { cn } from "../../lib/utils";
import { canonicalGenerationLabel } from "../../lib/canonicalGenerationIdentity";
import {
  criticAdvisoryComplete,
  criticAdvisoryVerdict,
  pipelineCheckpointIdentityIssues,
  reviewerRetryPending,
} from "../../lib/pipelinePresentation";
import { CheckIcon, CrossIcon } from "./icons";

export function PipelineStepper({ checkpoint }: { checkpoint: PipelineCheckpoint | null }) {
  if (!checkpoint) return null;

  const rawStage = checkpoint.stage ?? "";
  if (isPipelineTimeoutLeaseStage(rawStage)) {
    const lease = PIPELINE_TIMEOUT_LEASES[rawStage];
    return (
      <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
        <p className="font-medium">
          当前租约态：{lease.label} <span className="font-mono text-amber-600 dark:text-amber-400">({rawStage})</span>
        </p>
        <p className="mt-1">{lease.description}</p>
        <p className="mt-1 text-[10px] text-amber-700 dark:text-amber-300">
          唯一恢复工具：<span className="font-mono">{lease.nextTool}</span>。该租约不计入成功流水线进度。
        </p>
      </div>
    );
  }
  if (!PIPELINE_STAGE_CONTRACT.includes(rawStage as PipelineStage)) {
    return (
      <div className="rounded border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
        未知后端流水线阶段：<span className="font-mono">{rawStage || "(missing)"}</span>。进度按不可用处理，未伪装为全未完成。
      </div>
    );
  }
  const stage = rawStage as PipelineStage;
  const isRepair = stage === "repair_planned" || stage === "rework_running";
  // Once repair owns the candidate, review/critic/precommit results belong to
  // the pre-repair bytes.  Keep only the planning prefix green and render the
  // Worker milestone as active until the repaired bytes re-enter the gates.
  const milestone = isRepair ? "workers_done" : STAGE_TO_MILESTONE[stage];
  const currentIdx = PIPELINE_STAGES.indexOf(milestone);
  const isFailure = stage.endsWith("_failed")
    || stage.endsWith("_rejected")
    || stage === "official_inconclusive";

  return (
    <div>
      <div className={cn(
        "mb-1 text-[10px] font-medium",
        isFailure ? "text-red-600 dark:text-red-400" : stage === "official_bootstrap_required" ? "text-amber-600 dark:text-amber-400" : "text-gray-500",
      )}>
        当前阶段：{STAGE_LABELS[stage]} <span className="font-mono text-gray-400">({stage})</span>
      </div>
      <div className="flex items-center gap-0 overflow-x-auto py-2">
      {PIPELINE_STAGES.map((pipelineStage, i) => {
        const done = i < currentIdx;
        const active = i === currentIdx;
        return (
          <div key={pipelineStage} className="flex items-center shrink-0">
            {/* Node */}
            <div className="flex flex-col items-center">
              <div className={cn(
                "relative w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-bold border-2 transition-all duration-300",
                done && "border-success-500 bg-success-500 text-white",
                active && isFailure && "border-error-500 bg-error-500/10 text-error-600",
                active && isRepair && "border-warning-500 bg-warning-500/10 text-warning-600",
                active && !isFailure && !isRepair && "border-brand-500 bg-brand-500/10 text-brand-500",
                !done && !active && "border-gray-300 dark:border-gray-700 text-gray-400",
              )}>
                {done ? <CheckIcon className="w-3 h-3" /> : <span>{i + 1}</span>}
                {active && (
                  <span className={cn(
                    "absolute inset-0 rounded-full border-2 animate-pulse-ring",
                    isFailure ? "border-error-500" : isRepair ? "border-warning-500" : "border-brand-500",
                  )} />
                )}
              </div>
              <span className="mt-1 text-[9px] text-center max-w-[48px] leading-tight text-gray-500 dark:text-gray-400">
                {STAGE_LABELS[pipelineStage]}
              </span>
            </div>
            {/* Connector */}
            {i < PIPELINE_STAGES.length - 1 && (
              <div className={cn(
                "w-4 h-0.5 transition-colors duration-300 mx-0.5",
                i < currentIdx ? "bg-success-500" : "bg-gray-300 dark:bg-gray-700",
              )} />
            )}
          </div>
        );
      })}
      </div>
    </div>
  );
}

export function PipelineStatus({
  checkpoint,
  activeGeneration = null,
  handoff,
  handoffBlocked = false,
  activeBlocked = false,
  activeIssues = [],
  schedulerActive = false,
}: {
  checkpoint: PipelineCheckpoint | null;
  activeGeneration?: ActiveGeneration | null;
  handoff?: PostPublicationHandoffStatus | null;
  handoffBlocked?: boolean;
  activeBlocked?: boolean;
  activeIssues?: string[];
  schedulerActive?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const activeIdentityLabel = activeGeneration
    ? canonicalGenerationLabel(activeGeneration, activeGeneration.next_v)
    : null;
  const activeIdentityText = activeIdentityLabel ?? "双身份投影不可用";

  if (handoff && handoff.status !== "none") {
    const blocked = handoff.status === "blocked" || handoff.blocked || handoffBlocked;
    return (
      <div className="p-3">
        <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">
          发布后交接{handoff.version != null ? ` v${handoff.version}` : ""}
        </h3>
        <p className={cn(
          "text-xs",
          blocked
            ? "text-red-600 dark:text-red-300"
            : handoff.status === "running"
              ? "text-brand-600 dark:text-brand-300"
              : "text-amber-600 dark:text-amber-300",
        )}>
          {blocked
            ? "Archivist 交接被真实性或恢复检查阻断，下一代不会启动。"
            : handoff.status === "running"
              ? "Archivist 正在完成稳定性、归档与清理收据。"
              : "发布已完成，正在等待确定性的 Archivist 恢复。"}
        </p>
        {handoff.source_v != null && (
          <p className="mt-1 text-[10px] text-gray-500">
            source_v=v{handoff.source_v} · revision={handoff.record_revision ?? "—"}
            {handoff.identity_digest ? ` · identity=${handoff.identity_digest.slice(0, 12)}` : ""}
          </p>
        )}
        {handoff.issues.length > 0 && (
          <p className="mt-1 text-[10px] text-red-500">{handoff.issues.join("；")}</p>
        )}
      </div>
    );
  }

  if (activeGeneration && activeBlocked) {
    return (
      <div className="p-3">
        <h3 className="mb-1 text-xs font-semibold uppercase text-red-700 dark:text-red-300">
          流水线 {activeIdentityText} 恢复已阻断
        </h3>
        <p className="text-xs text-red-600 dark:text-red-300">
          后端不会执行当前 checkpoint route；解决权威或恢复诊断前不会推进下一代。
        </p>
        {activeIssues.length > 0 && (
          <p className="mt-1 text-[10px] text-red-500">{activeIssues.join("；")}</p>
        )}
      </div>
    );
  }

  if (!activeGeneration) {
    return (
      <div className="p-3">
        <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">流水线</h3>
        <p className={cn(
          "text-xs",
          schedulerActive ? "text-amber-700 dark:text-amber-300" : "text-gray-400",
        )}>
          {schedulerActive
            ? "外层 generation scheduler 正在持有无 checkpoint 边界；下一动作是系统非 MCP prepare_generation。"
            : "权威状态中没有活跃代次"}
        </p>
      </div>
    );
  }

  if (!checkpoint) {
    return (
      <div className="p-3">
        <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">
          流水线 {activeIdentityText}
          {activeGeneration.source_v != null ? ` · source_v=v${activeGeneration.source_v}` : ""}
        </h3>
        <p className="text-xs text-amber-600 dark:text-amber-300">
          权威活动阶段为 {activeGeneration.stage}，详细 checkpoint 暂不可用。
        </p>
      </div>
    );
  }

  const identityIssues = pipelineCheckpointIdentityIssues(checkpoint, activeGeneration);
  if (identityIssues.length > 0) {
    return (
      <div className="p-3">
        <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">流水线权威不可用</h3>
        <p className="text-xs text-red-600 dark:text-red-300">
          checkpoint 与 control active_generation 的 {identityIssues.join("、")} 不一致；不显示旧流程进度。
        </p>
      </div>
    );
  }

  const plan = checkpoint.master_plan && Array.isArray(checkpoint.master_plan.tasks)
    ? checkpoint.master_plan.tasks
    : [];
  const planProjectionInvalid = checkpoint.master_plan != null && !Array.isArray(checkpoint.master_plan.tasks);
  const repairActive = checkpoint.stage === "repair_planned" || checkpoint.stage === "rework_running";
  const reviewAttempts = checkpoint.review_attempt_journal ?? [];
  const latestReviewAttempt = reviewAttempts.length > 0
    ? reviewAttempts[reviewAttempts.length - 1]
    : undefined;
  const reviewRetryIsPending = reviewerRetryPending(checkpoint);

  return (
    <div className="p-3">
      <button onClick={() => setExpanded(!expanded)} className="w-full text-left flex items-center justify-between mb-2">
        <h3 className="text-xs font-semibold uppercase text-gray-500">
          流水线 {activeIdentityText}
          {activeGeneration.source_v != null ? ` · source_v=v${activeGeneration.source_v}` : ""}
          {activeGeneration.attempt.generation ? ` (尝试 ${activeGeneration.attempt.generation})` : ""}
        </h3>
        <span className="text-[10px] text-gray-400">{expanded ? "▲" : "▼"}</span>
      </button>

      <PipelineStepper checkpoint={checkpoint} />

      {expanded && (
        <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700 space-y-2">
          {repairActive && (
            <div className="rounded border border-warning-300 bg-warning-50 p-2 text-[10px] text-warning-700 dark:border-warning-800 dark:bg-warning-950/20 dark:text-warning-300">
              当前字节正在修复。此前 quality/review/Critic/precommit 结果只描述修复前字节，不显示为当前代码已完成。
            </div>
          )}
          {reviewRetryIsPending && latestReviewAttempt && (
            <div className="rounded border border-amber-300 bg-amber-50 p-2 text-[10px] text-amber-800 dark:border-amber-800 dark:bg-amber-950/20 dark:text-amber-300">
              Reviewer 第 1 次独立判定已拒绝；候选仍停留在 quality_passed，状态机将只重试 Reviewer 第 2 次。Master、Worker 与 70-hand Quality 不会重跑。
              <p className="mt-1 text-gray-500 dark:text-gray-400">
                attempt receipt {latestReviewAttempt.receipt_digest.slice(0, 12)} · candidate {latestReviewAttempt.candidate_artifact_hash.slice(0, 12)}
              </p>
            </div>
          )}
          {checkpoint.direction_audit?.repetition_detected && (
            <div className={cn(
              "p-2 rounded text-[10px] border",
              checkpoint.direction_audit.resolved
                ? "bg-success-50 dark:bg-success-900/20 border-success-200 dark:border-success-800"
                : "bg-warning-50 dark:bg-warning-900/20 border-warning-200 dark:border-warning-800",
            )}>
              {checkpoint.direction_audit.resolved ? (
                <div className="flex items-start gap-1.5">
                  <CheckIcon className="w-3 h-3 text-success-600 shrink-0 mt-0.5" />
                  <div>
                    <span className="font-semibold text-success-700 dark:text-success-400">方向重复已解决</span>
                    {checkpoint.direction_audit.suggested_direction && (
                      <p className="mt-0.5 text-success-600 dark:text-success-400">
                        已切换至: {checkpoint.direction_audit.suggested_direction}
                      </p>
                    )}
                  </div>
                </div>
              ) : (
                <div className="flex items-start gap-1.5">
                  <span className="text-warning-600 shrink-0 mt-0.5">⚠</span>
                  <div>
                    <span className="font-semibold text-warning-700 dark:text-warning-400">检测到方向重复</span>
                    {checkpoint.direction_audit.exhausted_directions.length > 0 && (
                      <p className="mt-0.5 text-warning-600 dark:text-warning-400">
                        已枯竭方向: {checkpoint.direction_audit.exhausted_directions.join("、")}
                      </p>
                    )}
                    {checkpoint.direction_audit.mandatory_constraints && (
                      <p className="mt-0.5 text-warning-600 dark:text-warning-400">
                        强制约束: {checkpoint.direction_audit.mandatory_constraints.slice(0, 150)}
                      </p>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}
          {planProjectionInvalid && (
            <p className="text-[10px] text-red-600">Master Plan 投影缺少 tasks 数组；不从未知结构推断任务。</p>
          )}
          {plan.length > 0 && (
            <div>
              <p className="text-[10px] text-gray-500 mb-1">Master Plan</p>
              {plan.map((task: MasterPlanTask, i: number) => (
                <div key={i} className="text-[10px] text-gray-600 dark:text-gray-400 pl-2 border-l-2 border-brand-300 mb-1">
                  <span className="font-medium">{String(task.role || `Task ${i + 1}`)}</span>
                  {task.target_files ? <span className="text-gray-400 ml-1">→ {Array.isArray(task.target_files) ? (task.target_files as string[]).join(", ") : String(task.target_files)}</span> : null}
                  {task.difficulty ? <span className="ml-1 px-1 rounded bg-gray-100 dark:bg-gray-800 text-gray-500">{String(task.difficulty)}</span> : null}
                </div>
              ))}
            </div>
          )}
          {checkpoint.reviewer_feedback && (
            <div>
              <p className="text-[10px] text-gray-500 mb-1">{repairActive ? "触发修复的 Reviewer 反馈" : "Reviewer 反馈"}</p>
              <p className="text-[10px] text-gray-600 dark:text-gray-400 whitespace-pre-wrap max-h-24 overflow-y-auto">{checkpoint.reviewer_feedback}</p>
            </div>
          )}
          {(() => {
            const gates = checkpoint.gate_results;
            if (!gates || Object.keys(gates).length === 0) return null;
            if (repairActive) return null;
            const gateLabels: Record<string, string> = {
              direction_audit: "方向审核",
              quality: "质量检查",
              review: "代码审核",
              critic: "建议性 Critic",
              precommit_eval: "提交前验证",
            };
            return (
              <div>
                <p className="text-[10px] text-gray-500 mb-1">质量门</p>
                <div className="space-y-1">
                  {Object.entries(gates).map(([key, g]) => {
                    const gate = g as PipelineGateResult;
                    if (key === "critic") {
                      const advisoryComplete = criticAdvisoryComplete(gate);
                      const advisoryVerdict = criticAdvisoryVerdict(gate);
                      return (
                        <div key={key} className="flex items-start gap-1.5 border-l-2 border-violet-300 pl-2 text-[10px]">
                          <span className="shrink-0 text-violet-500">◇</span>
                          <div>
                            <span className="font-medium text-gray-700 dark:text-gray-300">建议性 Critic</span>
                            <span className="ml-1 text-gray-400">{advisoryComplete ? `已完成 · ${advisoryVerdict}` : "执行证据不完整"}</span>
                            {gate.score != null && <span className="ml-1 text-gray-400">分数 {String(gate.score)}</span>}
                            <p className="text-gray-400">仅供后续决策参考，不授予发布资格。</p>
                          </div>
                        </div>
                      );
                    }
                    const passed = gate.passed ?? gate.all_passed ?? gate.approved;
                    const acceptance = gate.national_acceptance;
                    const acceptanceTiming = (
                      acceptance && typeof acceptance === "object"
                      ? acceptance
                      : null
                    );
                    return (
                      <div key={key} className="flex items-start gap-1.5 text-[10px] pl-2 border-l-2 border-brand-300">
                        <span className="shrink-0 mt-px">{passed ? <CheckIcon className="text-success-600" /> : <CrossIcon className="text-error-500" />}</span>
                        <div>
                          <span className="font-medium text-gray-700 dark:text-gray-300">{gateLabels[key] || key}</span>
                          {gate.quality_score != null && <span className="ml-1 text-gray-400">分数 {String(gate.quality_score)}</span>}
                          {gate.score != null && <span className="ml-1 text-gray-400">分数 {String(gate.score)}</span>}
                          {gate.decision_pass_rate != null && <span className="ml-1 text-gray-400">决策 {String(Math.round(gate.decision_pass_rate * 100))}%</span>}
                          {acceptanceTiming && (
                            <p className={acceptanceTiming.timing_ok === true ? "text-gray-400" : "text-error-500"}>
                              原生 70 手计时证据：{acceptanceTiming.timing_ok === true ? "已绑定" : "缺失/漂移"}
                              {typeof acceptanceTiming.native_match_timing_plan_digest === "string" && (
                                <span className="ml-1 font-mono">{acceptanceTiming.native_match_timing_plan_digest.slice(0, 12)}</span>
                              )}
                              {acceptanceTiming.native_match_timeout_phase != null && (
                                <span className="ml-1">timeout={String(acceptanceTiming.native_match_timeout_phase)}</span>
                              )}
                              {acceptanceTiming.native_terminal_abort && (
                                <span className="ml-1">abort={String(acceptanceTiming.native_terminal_abort.code || "unknown")}</span>
                              )}
                            </p>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}
