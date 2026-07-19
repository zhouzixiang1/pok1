import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { StrengthJobsResponse } from "../api/types";
import { strengthJobsBindingIssues } from "../api/strengthJobs";
import { useControlStatus } from "../hooks/useControlStatus";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader, EmptyState } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { OperatorSituation } from "../components/evolution/OperatorSituation";
import {
  strengthJobView,
  strengthRejectionLabel,
  daemonActivityLabel,
  producerConsumerCapabilityView,
} from "../domain/strengthJobView";
import { cn } from "../lib/utils";

/**
 * Background Strength — the 70-hand native TCP job lifecycle.
 *
 * Admitted samples come from the current immutable evaluation cycle
 * (identity-bound). Staged-pending rows are durable inputs awaiting atomic
 * cycle publication, not proof that a match process is currently running. Inadmissible
 * diagnostics explain *why* a 69-hand, stale-identity, or off-pool sample was
 * dropped — they never contribute to strength.  Daemon liveness separates
 * configuration intent from live process availability.
 */
export default function BackgroundStrength() {
  const { status, health, loading, error } = useControlStatus(5_000);
  const [jobs, setJobs] = useState<StrengthJobsResponse | null>(null);
  const strengthAuthorityKey = status?.epoch_initialized
    ? `${status.reset_receipt_digest ?? "missing"}:${status.active_bots.join("|")}`
    : "uninitialized";

  useEffect(() => {
    if (!status?.epoch_initialized || status.reset_receipt_valid !== true) { setJobs(null); return; }
    setJobs(null);
    let cancelled = false;
    const refresh = () => api.pipelineStrengthJobs().then((v) => { if (!cancelled) setJobs(v); }).catch((e) => {
      if (!cancelled) setJobs(null);
      console.error("[BackgroundStrength] jobs error:", e);
    });
    refresh();
    const id = setInterval(refresh, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [status?.epoch_initialized, status?.reset_receipt_valid, strengthAuthorityKey]);

  const bindingIssues = jobs && status
    ? strengthJobsBindingIssues(jobs, {
        active_bots: status.active_bots,
        reset_receipt_digest: status.reset_receipt_digest,
      })
    : [];
  const view = jobs && bindingIssues.length === 0 ? strengthJobView(jobs) : null;
  const capability = view ? producerConsumerCapabilityView(view.capabilities) : null;
  const observerIssue = jobs
    ? (jobs.observer.issues.join("、") || (jobs.available === false ? jobs.reason : "observer_incomplete"))
    : "observer_incomplete";
  const bindingFailureMessage = jobs && bindingIssues.length > 0
    ? jobs.observer.complete
      ? `后台评测与当前发布池/reset 身份不一致（${bindingIssues.join("、")}）；旧观察已隐藏，等待新权威。`
      : `后台证据观察失败（${observerIssue}），且未形成当前发布池/reset 的完整绑定；没有展示任何部分结果。`
    : null;

  return (
    <>
      <PageMeta title="后台 70 手评测 — Bot 自进化" description="真正影响强度与选代的 native TCP 后台评测" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />
      <OperatorSituation status={status} health={health} className="mb-4" />

      {!status?.epoch_initialized ? (
        <EmptyState message="严格进化尚未初始化；当前没有可验证的后台强度评测。" />
      ) : status.reset_receipt_valid !== true ? (
        <EmptyState message="策略 epoch reset 收据当前不可验证；后台强度观察不会启动，也不会回退到旧周期。" />
      ) : bindingFailureMessage ? (
        <EmptyState message={bindingFailureMessage} />
      ) : !view ? (
        <EmptyState message="正在读取后台评测权威；读取完成前不猜测队列状态。" />
      ) : (
        <div className="space-y-4">
          <Card>
            <CardHeader title="当前能力边界" subtitle="不要把未来设计当成已经启用的功能" />
            <div className="p-3 text-xs leading-5 text-gray-600 dark:text-gray-300">
              当前页面只读后端明确声明可用的能力。<span className="font-semibold">{capability?.label}</span>：
              {capability?.detail} 无论能力是否启用，只有绑定当前 reset、发布池与评测身份的完整 70 手证据才能进入强度周期。
            </div>
          </Card>

          {/* Daemon liveness */}
          <Card>
            <CardHeader title="后台评分进程是否真的在工作" subtitle="“配置为启用”不等于“进程存活且心跳新鲜”" />
            <div className="p-3 space-y-1 text-xs">
              <div className="flex items-center gap-2">
                <Badge variant={
                  view.daemon.state === "alive_fresh" ? "success"
                  : view.daemon.state === "unconfigured" ? "neutral"
                  : "error"
                } size="sm">{
                  view.daemon.state === "alive_fresh" ? "进程健康"
                  : view.daemon.state === "configuration_conflict" ? "配置禁用但进程仍存活"
                  : view.daemon.state === "configured_dead" ? "已配置但进程不在"
                  : view.daemon.state === "alive_stale_heartbeat" ? "进程在但心跳过期"
                  : view.daemon.state === "configured_unverifiable" ? "进程在但无法验证心跳"
                  : view.daemon.state === "unconfigured" ? "未配置"
                  : "状态未知"
                }</Badge>
                <span className="text-gray-600 dark:text-gray-300">{view.daemon.detail}</span>
              </div>
              <p className="text-gray-700 dark:text-gray-200">当前工作：{daemonActivityLabel(view.daemon.activityState)}</p>
              <p className="text-gray-500 dark:text-gray-400 mt-1">
                只有“已配置 + 进程身份匹配 + 心跳新鲜”才能证明守护进程可工作；这仍不等于当前一定有对局正在跑。
              </p>
            </div>
          </Card>

          {!view.available ? (
            <Card>
              <CardHeader title="为什么还没有强度结果" subtitle={view.reason} />
              <div className="p-3 text-xs text-gray-500 dark:text-gray-400">
                {view.reason === "active_pool_empty" && "当前还没有正式发布 Bot；候选和 Official 认证任务都不能进入强度评分。"}
                {view.reason === "active_pool_singleton" && "发布池只有 1 个 Bot；至少需要 2 个已发布 Bot 才能形成 H2H 评分周期。"}
                {view.reason === "awaiting_first_complete_cycle" && "至少两个 Bot 已发布，正在等待首个完整 70 手 native TCP 不可变评分周期。"}
                {(view.reason === "evaluation_bundle_unavailable" || view.reason === "evaluation_manifest_missing" || view.reason === "evaluation_identity_invalid" || view.reason === "evaluation_selection_rows_missing") && "当前评分周期的身份或清单不可验证；页面不会回退到旧评分或默认分。"}
              </div>
            </Card>
          ) : (
            <>
              {!view.observerComplete && (
                <Card>
                  <CardHeader title="后台证据观察未完整" subtitle="读取超过有界预算或验证失败；部分扫描结果不会冒充完整队列" />
                  <div className="p-3 text-xs text-error-600 dark:text-error-400">
                    {view.observerIssues.join("；") || "observer_incomplete"}
                  </div>
                </Card>
              )}
              {/* Identity + cycle */}
              <Card>
                <CardHeader title="当前不可变评分周期" subtitle="所有样本必须绑定同一评测身份" />
                <div className="p-3 grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs">
                  <Field label="参与评分的 Bot" value={view.activeBots.join(", ") || "(空)"} />
                  <Field label="已纳入周期" value={`${view.admittedCount} 条`} />
                  <Field label="已落盘待发布" value={`${view.stagedPendingTotal} 条`} />
                  <Field label="被拒样本" value={`${view.inadmissibleTotal} 条`} />
                  <Field label="评测身份" value={`${view.evaluationIdentityDigest.slice(0, 12)}…`} mono />
                  <Field label="周期清单" value={view.evaluationManifestDigest ? `${view.evaluationManifestDigest.slice(0, 12)}…` : "—"} mono />
                  <Field label="epoch 收据" value={view.epochResetReceiptDigest ? `${view.epochResetReceiptDigest.slice(0, 12)}…` : "—"} mono />
                  <Field label="发布池绑定" value={view.authorityBinding.complete ? "完整" : "不完整"} />
                  <Field label="队列租约能力" value={view.capabilities.queued_running_leases ? "可用" : "未启用"} />
                </div>
              </Card>

              {/* Admitted samples */}
              <Card>
                <CardHeader title="已纳入强度计算" subtitle="每个样本都是一场完整 70 手 native TCP；Official 与 Arena 不在这里" />
                <div className="p-3 space-y-1 text-xs">
                  {jobs && jobs.available && jobs.admitted_samples.length === 0 ? (
                    <p className="text-gray-400">暂无已采纳样本。</p>
                  ) : (
                    jobs && jobs.available && jobs.admitted_samples.slice(0, 20).map((s) => (
                      <div key={s.id ?? s.timestamp} className="flex items-center gap-2 border-b border-gray-50 dark:border-gray-900 py-0.5">
                        <span className="text-gray-600 dark:text-gray-300 truncate">{s.bot0} vs {s.bot1}</span>
                        <span className="font-mono text-gray-500 ml-auto shrink-0">
                          样本胜/负/和 {s.bot0_wins ?? 0}-{s.bot1_wins ?? 0}-{s.draws ?? 0}
                        </span>
                        <span className="text-gray-400 shrink-0">{s.strength_sample_count ?? 0} 个完整 70 手样本</span>
                      </div>
                    ))
                  )}
                  {jobs && jobs.available && jobs.pagination.admitted_total > jobs.admitted_samples.slice(0, 20).length && (
                    <p className="text-gray-400 mt-1">（当前页仅显示前 20 条，共 {jobs.pagination.admitted_total} 条）</p>
                  )}
                </div>
              </Card>

              {/* Staged pending */}
              {view.stagedPending.length > 0 && (
                <Card>
                  <CardHeader title="已落盘，等待发布进评分周期" subtitle="不是“正在对局”；只有周期原子发布后才有强度权威" />
                  <div className="p-3 space-y-1 text-xs">
                    {view.stagedPending.map((s) => (
                      <div key={s.filename} className="flex items-center gap-2 border-b border-gray-50 dark:border-gray-900 py-0.5">
                        <Badge variant="warning" size="sm">等待周期发布</Badge>
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
                  <CardHeader title="被拒的样本（零强度权重）" subtitle="只解释拒绝原因，不修补、不复用、不计分" />
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
                      69 手、旧 Bot 字节、迟到租约或评测身份变化的样本都不可采纳；这里只解释原因，不修改历史。
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
