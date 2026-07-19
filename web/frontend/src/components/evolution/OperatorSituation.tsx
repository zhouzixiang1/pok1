import type { ControlHealth, ControlStatus } from "../../api/control";
import { operatorSituationView, type OperatorSituationTone } from "../../domain/operatorSituationView";
import { Badge } from "../shared/Badge";
import { Card } from "../shared/Card";

const BADGE_VARIANT: Record<OperatorSituationTone, "success" | "info" | "warning" | "error" | "neutral"> = {
  success: "success",
  info: "info",
  warning: "warning",
  error: "error",
  neutral: "neutral",
};

const BORDER_CLASS: Record<OperatorSituationTone, string> = {
  success: "border-success-300 dark:border-success-800",
  info: "border-brand-300 dark:border-brand-800",
  warning: "border-warning-300 dark:border-warning-800",
  error: "border-error-300 dark:border-error-800",
  neutral: "border-gray-200 dark:border-border-subtle",
};

/** Shared operator-language projection. Raw stage/route stay in a details row. */
export function OperatorSituation({
  status,
  health,
  className = "",
}: {
  status: ControlStatus | null | undefined;
  health: ControlHealth | null | undefined;
  className?: string;
}) {
  const view = operatorSituationView(status, health);
  return (
    <Card className={`${BORDER_CLASS[view.tone]} ${className}`} padding="p-0">
      <div className="p-4">
        <div className="flex flex-wrap items-start gap-2">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wide text-gray-400">现在要知道的事</p>
            <h2 className="mt-0.5 text-base font-semibold text-gray-900 dark:text-white">{view.headline}</h2>
          </div>
          <Badge variant={BADGE_VARIANT[view.tone]} className="ml-auto" pulse={view.tone === "info" && status?.running === true}>
            {view.manualLabel}
          </Badge>
        </div>

        <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-3">
          <SituationField label="当前发生什么" value={view.what} />
          <SituationField label="为什么" value={view.why} />
          <SituationField label="下一步" value={view.next} />
        </div>

        <div className={`mt-3 rounded-lg border px-3 py-2 text-xs ${
          view.manualRequired
            ? "border-warning-200 bg-warning-50 text-warning-800 dark:border-warning-900 dark:bg-warning-950/30 dark:text-warning-200"
            : "border-success-200 bg-success-50 text-success-800 dark:border-success-900 dark:bg-success-950/20 dark:text-success-200"
        }`}>
          <span className="font-semibold">{view.manualRequired ? "人工处理：需要。" : "人工处理：不需要。"}</span>{" "}
          {view.manualDetail}
        </div>

        {view.continuityNote && (
          <p className="mt-2 rounded-lg border border-brand-100 bg-brand-50/50 px-3 py-2 text-xs text-brand-700 dark:border-brand-900 dark:bg-brand-950/20 dark:text-brand-300">
            <span className="font-semibold">继任说明：</span>{view.continuityNote}
          </p>
        )}

        <details className="mt-3 text-xs text-gray-500 dark:text-gray-400">
          <summary className="cursor-pointer select-none font-medium hover:text-gray-700 dark:hover:text-gray-200">
            技术身份与原始路由
          </summary>
          <div className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 md:grid-cols-2">
            {view.technical.map((row) => (
              <div key={row.label} className="flex min-w-0 justify-between gap-3 border-b border-gray-100 py-1 dark:border-gray-800">
                <span className="shrink-0">{row.label}</span>
                <span className="truncate text-right font-mono text-gray-700 dark:text-gray-300" title={row.value}>{row.value}</span>
              </div>
            ))}
          </div>
        </details>
      </div>
    </Card>
  );
}

function SituationField({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-gray-50 px-3 py-2 dark:bg-white/[0.03]">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-gray-400">{label}</p>
      <p className="mt-1 text-xs leading-5 text-gray-700 dark:text-gray-200">{value}</p>
    </div>
  );
}
