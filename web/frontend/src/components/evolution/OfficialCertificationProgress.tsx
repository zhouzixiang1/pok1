import type { ControlStatus } from "../../api/control";
import { useOfficialCertificationJobs } from "../../hooks/useOfficialCertificationJobs";
import { cn } from "../../lib/utils";

const VISIBLE_STAGES = new Set([
  "official_bootstrap_required",
  "official_certifying",
  "official_failed",
  "official_inconclusive",
  "publishing",
]);

const roundKind = (kind: string) => kind === "self_play" ? "自对弈" : kind === "opponent" ? "合格对手" : kind;

export function OfficialCertificationProgress({
  status,
  className,
}: {
  status: ControlStatus | null;
  className?: string;
}) {
  const stage = status?.active_generation?.stage ?? "";
  const shouldPoll = Boolean(
    status?.epoch_initialized
    && (stage === "official_certifying" || stage === "official_bootstrap_required"),
  );
  const { jobsProjection, loading, error } = useOfficialCertificationJobs(shouldPoll);

  if (!status?.epoch_initialized || !VISIBLE_STAGES.has(stage)) return null;

  const generation = status.active_generation;
  const identityMatches = Boolean(
    jobsProjection
    && generation
    && jobsProjection.workflow_run_id === generation.workflow_run_id
    && jobsProjection.candidate_version === generation.next_v,
  );
  const expectedAuthority = stage === "official_bootstrap_required"
    ? "operator_bootstrap_full_v5_job"
    : "pipeline_attached_full_v5_job";
  const job = identityMatches
    ? jobsProjection?.jobs.find((row) => (
        row.workflow_run_id === generation?.workflow_run_id
        && row.candidate_version === generation?.next_v
        && row.formal_authority === expectedAuthority
        && (expectedAuthority !== "operator_bootstrap_full_v5_job"
          || (row.read_only === true && row.cancel_allowed === false && Boolean(row.bootstrap_control_id)))
      ))
    : undefined;
  const progress = job?.progress;
  const requested = progress?.rounds_requested ?? 8;
  const completed = progress?.rounds_completed ?? 0;
  const activeHands = Math.min(70, Math.max(0, progress?.active_round?.hands_started ?? 0));
  const progressPct = requested > 0
    ? Math.min(100, ((completed + activeHands / 70) / requested) * 100)
    : 0;

  return (
    <section className={cn("rounded-xl border border-indigo-200 bg-indigo-50 p-4 dark:border-indigo-900/60 dark:bg-indigo-950/20", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-indigo-900 dark:text-indigo-200">官方 EXE 正式认证</h3>
          <p className="mt-0.5 text-xs text-indigo-700/80 dark:text-indigo-300/80">
            v{generation?.next_v ?? "—"} · official-full-v5 · 5 轮自对弈 + 3 轮合格对手 × 70 手
          </p>
        </div>
        <span className="rounded bg-white/70 px-2 py-1 font-mono text-[10px] text-indigo-700 dark:bg-black/20 dark:text-indigo-300">
          {stage}
        </span>
      </div>

      {stage === "official_bootstrap_required" && (
        <div className="mt-3 rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300">
          <p className="font-semibold">首个严格 Bot 需要显式操作员引导</p>
          <p className="mt-1">{status.operator_action || "按权威状态执行一次性 bootstrap 命令；网页只能读取精确授权任务，不能启动、取消或替代它。"}</p>
          {status.operator_command && (
            <code className="mt-2 block overflow-x-auto rounded bg-black/5 p-2 font-mono text-[11px] dark:bg-black/30">{status.operator_command}</code>
          )}
        </div>
      )}

      {stage === "publishing" ? (
        <p className="mt-3 text-xs text-indigo-700 dark:text-indigo-300">官方 full-v5 门已完成，正在进行内容绑定、签名发布与随后 post-commit 归档。</p>
      ) : stage === "official_failed" || stage === "official_inconclusive" ? (
        <p className="mt-3 text-xs text-red-700 dark:text-red-300">正式认证未通过；结果不会被显示为发布资格，需由 checkpoint 流程决定修复或重试。</p>
      ) : loading && !jobsProjection ? (
        <p className="mt-3 text-xs text-indigo-700 dark:text-indigo-300">读取当前 workflow 附着任务…</p>
      ) : error ? (
        <p className="mt-3 text-xs text-red-700 dark:text-red-300">正式任务投影不可用：{error}</p>
      ) : !job ? (
        <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">
          {stage === "official_bootstrap_required"
            ? "尚未发现唯一且精确授权的 v143 bootstrap full-v5 job；旧版本、未授权或多重匹配任务不会显示。"
            : "当前 checkpoint 尚无可验证的附着 full-v5 job；HTTP 不搜索或提升未附着/旧 epoch 任务。"}
        </p>
      ) : (
        <div className="mt-3 space-y-2">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-indigo-800 dark:text-indigo-200">
            <span>状态 <b>{job.state}</b>{job.phase ? ` / ${job.phase}` : ""}</span>
            <span>轮次 <b>{completed}/{requested}</b></span>
            <span>通过 <b>{progress?.rounds_passed ?? 0}</b></span>
            <span>attempt <b>{job.attempt ?? progress?.suite_attempt ?? "—"}</b></span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-indigo-100 dark:bg-indigo-950">
            <div className="h-full rounded-full bg-indigo-500 transition-all" style={{ width: `${progressPct}%` }} />
          </div>
          {progress?.active_round && (
            <p className="text-xs text-indigo-700 dark:text-indigo-300">
              当前：{roundKind(progress.active_round.kind)} #{progress.active_round.index} · 启动 {progress.active_round.hands_started}/70 手 · 可见结算 {progress.active_round.settlements}
            </p>
          )}
          <p className="break-all font-mono text-[10px] text-indigo-500">job {job.job_id}</p>
        </div>
      )}
    </section>
  );
}
