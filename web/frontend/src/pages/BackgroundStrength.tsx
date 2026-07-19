import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { StrengthJobsResponse } from "../api/types";
import { useControlStatus } from "../hooks/useControlStatus";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader, EmptyState } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import {
  strengthJobView,
  strengthRejectionLabel,
} from "../domain/strengthJobView";
import { cn } from "../lib/utils";

/**
 * Background Strength — the 70-hand native TCP job lifecycle.
 *
 * Admitted samples come from the current immutable evaluation cycle
 * (identity-bound).  Staged-pending matches are in-flight.  Inadmissible
 * diagnostics explain *why* a 69-hand, stale-identity, or off-pool sample was
 * dropped — they never contribute to strength.  Daemon liveness separates
 * configuration intent from live process availability.
 */
export default function BackgroundStrength() {
  const { status, loading, error } = useControlStatus(5_000);
  const [jobs, setJobs] = useState<StrengthJobsResponse | null>(null);

  useEffect(() => {
    if (!status?.epoch_initialized) { setJobs(null); return; }
    let cancelled = false;
    const refresh = () => api.pipelineStrengthJobs().then((v) => { if (!cancelled) setJobs(v); }).catch((e) => {
      if (!cancelled) setJobs(null);
      console.error("[BackgroundStrength] jobs error:", e);
    });
    refresh();
    const id = setInterval(refresh, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [status?.epoch_initialized]);

  const view = jobs ? strengthJobView(jobs) : null;

  return (
    <>
      <PageMeta title="后台强度任务 — Bot 自进化" description="70 手 native TCP 强度任务" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />

      {!status?.epoch_initialized ? (
        <EmptyState message="epoch 未初始化；强度任务投影不可用。" />
      ) : !view ? (
        <EmptyState message="加载强度任务投影..." />
      ) : (
        <div className="space-y-4">
          {/* Daemon liveness */}
          <Card>
            <CardHeader title="评分 daemon 状态" subtitle="配置意图 vs 实际进程 vs 心跳" />
            <div className="p-3 space-y-1 text-xs">
              <div className="flex items-center gap-2">
                <Badge variant={
                  view.daemon.state === "alive_fresh" ? "success"
                  : view.daemon.state === "unconfigured" ? "neutral"
                  : "error"
                } size="sm">{view.daemon.state}</Badge>
                <span className="text-gray-600 dark:text-gray-300">{view.daemon.detail}</span>
              </div>
              <p className="text-gray-500 dark:text-gray-400 mt-1">
                "配置已启用"不等于"正在运行"；只有 alive + fresh 心跳才确认实际调度。
              </p>
            </div>
          </Card>

          {!view.available ? (
            <Card>
              <CardHeader title="强度证据不可用" subtitle={view.reason} />
              <div className="p-3 text-xs text-gray-500 dark:text-gray-400">
                {view.reason === "active_pool_empty" && "当前严格发布池为空；尚无可进入评分周期的 Bot。"}
                {view.reason === "active_pool_singleton" && "发布池只有 1 个 Bot；至少需要 2 个才能配对。"}
                {view.reason === "awaiting_first_complete_cycle" && "等待首个完整 immutable 70 手 evaluation cycle。"}
                {(view.reason === "evaluation_bundle_unavailable" || view.reason === "evaluation_manifest_missing" || view.reason === "evaluation_identity_invalid" || view.reason === "evaluation_selection_rows_missing") && "evaluation bundle 暂不可用；不伪造强度结果。"}
              </div>
            </Card>
          ) : (
            <>
              {/* Identity + cycle */}
              <Card>
                <CardHeader title="当前 evaluation cycle" subtitle="immutable · identity-bound" />
                <div className="p-3 grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs">
                  <Field label="active_bots" value={view.activeBots.join(", ") || "(空)"} />
                  <Field label="admitted 样本" value={String(view.admittedCount)} mono />
                  <Field label="staged pending" value={String(view.stagedPending.length)} mono />
                  <Field label="不可采纳" value={String(view.inadmissibleDiagnostics.length)} mono />
                  <Field label="identity_digest" value={`${view.evaluationIdentityDigest.slice(0, 12)}…`} mono />
                  <Field label="manifest_digest" value={view.evaluationManifestDigest ? `${view.evaluationManifestDigest.slice(0, 12)}…` : "—"} mono />
                  <Field label="receipt_digest" value={view.epochResetReceiptDigest ? `${view.epochResetReceiptDigest.slice(0, 12)}…` : "—"} mono />
                </div>
              </Card>

              {/* Admitted samples */}
              <Card>
                <CardHeader title="已采纳强度样本" subtitle="完整 70 手 native TCP · 进入 immutable cycle" />
                <div className="p-3 space-y-1 text-xs">
                  {jobs && jobs.available && jobs.admitted_samples.length === 0 ? (
                    <p className="text-gray-400">暂无已采纳样本。</p>
                  ) : (
                    jobs && jobs.available && jobs.admitted_samples.slice(0, 20).map((s) => (
                      <div key={s.id ?? s.timestamp} className="flex items-center gap-2 border-b border-gray-50 dark:border-gray-900 py-0.5">
                        <span className="text-gray-600 dark:text-gray-300 truncate">{s.bot0} vs {s.bot1}</span>
                        <span className="font-mono text-gray-500 ml-auto shrink-0">
                          {s.bot0_wins ?? 0}-{s.bot1_wins ?? 0}-{s.draws ?? 0}
                        </span>
                        <span className="text-gray-400 shrink-0">{s.strength_sample_count ?? 0} 手</span>
                      </div>
                    ))
                  )}
                  {jobs && jobs.available && jobs.admitted_samples.length > 20 && (
                    <p className="text-gray-400 mt-1">（仅显示前 20 条，共 {jobs.admitted_samples.length} 条）</p>
                  )}
                </div>
              </Card>

              {/* Staged pending */}
              {view.stagedPending.length > 0 && (
                <Card>
                  <CardHeader title="staged pending（待提交）" subtitle="已通过 native 校验，尚未进入 immutable cycle" />
                  <div className="p-3 space-y-1 text-xs">
                    {view.stagedPending.map((s) => (
                      <div key={s.filename} className="flex items-center gap-2 border-b border-gray-50 dark:border-gray-900 py-0.5">
                        <Badge variant="warning" size="sm">pending</Badge>
                        <span className="text-gray-600 dark:text-gray-300 truncate">{s.bot0} vs {s.bot1}</span>
                        <span className="font-mono text-gray-500 ml-auto shrink-0 truncate">{s.filename}</span>
                      </div>
                    ))}
                  </div>
                </Card>
              )}

              {/* Inadmissible diagnostics */}
              {view.inadmissibleDiagnostics.length > 0 && (
                <Card>
                  <CardHeader title="不可采纳诊断（零强度权重）" subtitle="说明为何被拒；绝不计入强度" />
                  <div className="p-3 space-y-2">
                    {view.inadmissibleReasonCounts.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 mb-2">
                        {view.inadmissibleReasonCounts.map(({ reason, count }) => (
                          <span key={reason} className="rounded border border-error-300 dark:border-error-800 px-1.5 py-0.5 text-xs text-error-600 dark:text-error-400">
                            {strengthRejectionLabel(reason)} ×{count}
                          </span>
                        ))}
                      </div>
                    )}
                    <div className="space-y-1 text-xs">
                      {view.inadmissibleDiagnostics.slice(0, 20).map((d, i) => (
                        <div key={d.id ?? i} className="border-l-2 border-error-300 dark:border-error-800 pl-2">
                          <div className="text-gray-600 dark:text-gray-300">
                            {d.bot0} vs {d.bot1}
                            {d.hands_per_strength_sample != null && (
                              <span className="ml-1 text-error-500">{d.hands_per_strength_sample} 手</span>
                            )}
                          </div>
                          <div className="text-gray-500">
                            {d.rejection_reasons.map(strengthRejectionLabel).join("；")}
                          </div>
                        </div>
                      ))}
                    </div>
                    <p className="text-xs text-gray-400 mt-1">
                      69 手、旧 artifact、迟到 lease / identity 变更的样本都不可采纳；这里只解释原因，不修复历史。
                    </p>
                  </div>
                </Card>
              )}
            </>
          )}
        </div>
      )}
    </>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-2 border-b border-gray-50 dark:border-gray-900 py-0.5">
      <span className="text-gray-500 dark:text-gray-400 shrink-0">{label}</span>
      <span className={cn("text-gray-800 dark:text-gray-200 truncate text-right", mono && "font-mono")}>{value}</span>
    </div>
  );
}
