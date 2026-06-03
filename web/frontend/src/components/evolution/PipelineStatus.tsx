import { useState } from "react";
import type { PipelineCheckpoint } from "../../api/types";
import { PIPELINE_STAGES, STAGE_LABELS } from "../../constants/pipeline";
import { cn } from "../../lib/utils";
import { CheckIcon, CrossIcon } from "./icons";

export function PipelineStepper({ checkpoint }: { checkpoint: PipelineCheckpoint | null }) {
  if (!checkpoint) return null;

  const currentIdx = PIPELINE_STAGES.indexOf(checkpoint.stage as typeof PIPELINE_STAGES[number]);

  return (
    <div className="flex items-center gap-0 overflow-x-auto py-2">
      {PIPELINE_STAGES.map((stage, i) => {
        const done = i < currentIdx;
        const active = i === currentIdx;
        return (
          <div key={stage} className="flex items-center shrink-0">
            {/* Node */}
            <div className="flex flex-col items-center">
              <div className={cn(
                "relative w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-bold border-2 transition-all duration-300",
                done && "border-success-500 bg-success-500 text-white",
                active && "border-brand-500 bg-brand-500/10 text-brand-500",
                !done && !active && "border-gray-300 dark:border-gray-700 text-gray-400",
              )}>
                {done ? <CheckIcon className="w-3 h-3" /> : <span>{i + 1}</span>}
                {active && (
                  <span className="absolute inset-0 rounded-full border-2 border-brand-500 animate-pulse-ring" />
                )}
              </div>
              <span className="mt-1 text-[9px] text-center max-w-[48px] leading-tight text-gray-500 dark:text-gray-400">
                {STAGE_LABELS[stage]}
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
  );
}

export function PipelineStatus({ checkpoint }: { checkpoint: PipelineCheckpoint | null }) {
  const [expanded, setExpanded] = useState(false);

  if (!checkpoint) {
    return (
      <div className="p-3">
        <h3 className="mb-1 text-xs font-semibold uppercase text-gray-500">流水线</h3>
        <p className="text-xs text-gray-400">无活跃代次</p>
      </div>
    );
  }

  const plan = Array.isArray(checkpoint.master_plan) ? checkpoint.master_plan : [];

  return (
    <div className="p-3">
      <button onClick={() => setExpanded(!expanded)} className="w-full text-left flex items-center justify-between mb-2">
        <h3 className="text-xs font-semibold uppercase text-gray-500">
          流水线 v{checkpoint.next_v} ← v{checkpoint.source_v}
          {checkpoint.generation_attempt ? ` (尝试 ${checkpoint.generation_attempt})` : ""}
        </h3>
        <span className="text-[10px] text-gray-400">{expanded ? "▲" : "▼"}</span>
      </button>

      <PipelineStepper checkpoint={checkpoint} />

      {expanded && (
        <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-700 space-y-2">
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
          {plan.length > 0 && (
            <div>
              <p className="text-[10px] text-gray-500 mb-1">Master Plan</p>
              {plan.map((task: Record<string, unknown>, i: number) => (
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
              <p className="text-[10px] text-gray-500 mb-1">Reviewer 反馈</p>
              <p className="text-[10px] text-gray-600 dark:text-gray-400 whitespace-pre-wrap max-h-24 overflow-y-auto">{checkpoint.reviewer_feedback}</p>
            </div>
          )}
          {(() => {
            const gates = checkpoint.gate_results as Record<string, Record<string, unknown>> | undefined;
            if (!gates || Object.keys(gates).length === 0) return null;
            const gateLabels: Record<string, string> = {
              direction_audit: "方向审核",
              quality: "质量检查",
              review: "代码审核",
              critic: "策略审核",
              precommit_eval: "提交前验证",
            };
            return (
              <div>
                <p className="text-[10px] text-gray-500 mb-1">质量门</p>
                <div className="space-y-1">
                  {Object.entries(gates).map(([key, g]) => {
                    const passed = g.passed ?? g.all_passed ?? g.approved;
                    return (
                      <div key={key} className="flex items-start gap-1.5 text-[10px] pl-2 border-l-2 border-brand-300">
                        <span className="shrink-0 mt-px">{passed ? <CheckIcon className="text-success-600" /> : <CrossIcon className="text-error-500" />}</span>
                        <div>
                          <span className="font-medium text-gray-700 dark:text-gray-300">{gateLabels[key] || key}</span>
                          {g.quality_score != null && <span className="ml-1 text-gray-400">分数 {String(g.quality_score)}</span>}
                          {g.score != null && <span className="ml-1 text-gray-400">分数 {String(g.score)}</span>}
                          {g.decision_pass_rate != null && <span className="ml-1 text-gray-400">决策 {String(Math.round((g.decision_pass_rate as number) * 100))}%</span>}
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
