import type { ControlHealth, ControlStatus, PipelineRoute } from "../../api/control";
import { controlPipelineBlocked, controlPipelineIssues } from "../../api/control";
import { EvolutionSection, EvolutionStatusBadge, EvolutionSurface } from "./ui";
import { cn } from "../../lib/utils";

interface PipelineDiagnosticsProps {
  status: ControlStatus | null;
  health: ControlHealth | null;
  className?: string;
}

/**
 * Pipeline diagnostics panel: route, owner_scope, daemon effective pairs,
 * pairs_drift, recovery issues. Read-only — mutations stay on Control.
 */
export function PipelineDiagnostics({
  status,
  health,
  className,
}: PipelineDiagnosticsProps) {
  const pipeline = health?.pipeline ?? null;
  const blocked = controlPipelineBlocked(pipeline);
  const issues = controlPipelineIssues(pipeline);
  const route: PipelineRoute | null = pipeline?.route ?? null;
  const daemon = health?.daemon;
  const handoff = status?.post_publication_handoff;

  return (
    <EvolutionSurface className={cn("space-y-3", className)} padding="sm">
      <EvolutionSection
        title="流水线诊断"
        subtitle="下一动作 / 任务归属 / 后台实际对数 — 不从阶段猜测下一工具"
      />
      <div className="flex flex-wrap gap-2 text-xs">
        <EvolutionStatusBadge tone={blocked ? "error" : "ok"}>
          {blocked ? "下一动作阻断" : "下一动作可用"}
        </EvolutionStatusBadge>
        {route?.next_tool && (
          <span className="font-mono text-gray-600 dark:text-gray-300">
            下一动作={route.next_tool}
          </span>
        )}
        {handoff && handoff.status !== "none" && (
          <EvolutionStatusBadge
            tone={handoff.owner_scope === "foreign_process" ? "error" : "info"}
          >
            交接归属={handoff.owner_scope}
          </EvolutionStatusBadge>
        )}
        {daemon && (
          <EvolutionStatusBadge tone={daemon.pairs_drift ? "warn" : "neutral"}>
            后台对数：配置 {daemon.configured_pairs ?? "?"}
            {" · "}实际 {daemon.effective_pairs ?? "?"}
            {daemon.pairs_drift ? " · 不一致" : ""}
          </EvolutionStatusBadge>
        )}
      </div>
      {route?.directive && (
        <p className="rounded-md bg-gray-50 p-2 text-xs text-gray-700 dark:bg-white/[0.03] dark:text-gray-200">
          {route.directive}
        </p>
      )}
      {issues.length > 0 && (
        <ul className="space-y-0.5 text-[11px] text-error-600 dark:text-error-400">
          {issues.slice(0, 12).map((issue) => (
            <li key={issue}>· {issue}</li>
          ))}
        </ul>
      )}
      {!route && !blocked && (
        <p className="text-xs text-gray-400">当前无配对 route（可能处于干净 scheduler 边界）。</p>
      )}
    </EvolutionSurface>
  );
}
