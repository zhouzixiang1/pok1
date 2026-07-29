import type { ControlStatus, OperatorTransition } from "../../api/control";
import type {
  OfficialCertificationJob,
  OfficialCertificationJobsProjection,
} from "../../api/types";
import { useOfficialCertificationJobs } from "../../hooks/useOfficialCertificationJobs";
import {
  isNormalOfficialCertificationStage,
  isOfficialCertificationStage,
  officialJobsBindingIssues,
} from "../../api/officialJobs";
import { cn } from "../../lib/utils";

const roundKind = (kind: string) => kind === "self_play" ? "自对弈" : kind === "opponent" ? "认证对手" : kind;

function transitionMatches(
  transition: OperatorTransition | null | undefined,
  status: ControlStatus,
): transition is OperatorTransition {
  const generation = status.active_generation;
  return Boolean(
    transition
    && transition.schema_version === 1
    && transition.kind === "first-strict-official-operator-transition"
    && ["bootstrap_required", "bootstrap_running", "bootstrap_failed", "ready_to_finalize"].includes(transition.state)
    && typeof transition.action === "string"
    && transition.action.trim().length > 0
    && (transition.command == null || typeof transition.command === "string")
    && (transition.reason == null || typeof transition.reason === "string")
    && transition.evaluation_epoch === "national_tcp_policy_v1"
    && generation
    // The first-strict candidate/source versions are branch-configurable
    // (cloud: next_v=1, source_v=null; main: next_v=143, source_v=142).
    // Validate the backend-provided transition identity against the live
    // active generation instead of pinning to a branch-specific literal.
    && typeof generation.next_v === "number"
    && Number.isSafeInteger(generation.next_v)
    && generation.next_v >= 1
    && transition.candidate_version === generation.next_v
    && (transition.source_v == null ? generation.source_v == null : transition.source_v === generation.source_v)
    && transition.workflow_run_id === generation.workflow_run_id
    && transition.checkpoint_stage === "official_bootstrap_required"
    && typeof transition.checkpoint_revision === "number"
    && Number.isInteger(transition.checkpoint_revision)
    && transition.checkpoint_revision > 0
    && typeof transition.candidate_hash === "string"
    && /^[0-9a-f]{64}$/.test(transition.candidate_hash)
    && typeof transition.parked_request_digest === "string"
    && /^[0-9a-f]{64}$/.test(transition.parked_request_digest)
    && (transition.job_id == null || /^[0-9a-f]{64}$/.test(transition.job_id))
    && (transition.certificate_digest == null || /^[0-9a-f]{64}$/.test(transition.certificate_digest))
    && transition.certification_profile === "first_strict_control_v1"
    && transition.opponent_authority === "system_control"
    && transition.strength_evidence_weight === 0
    && transition.strategy_evidence_weight === 0
    && /^[0-9a-f]{64}$/.test(transition.transition_digest),
  );
}

function transitionJobBindingValid(
  transition: OperatorTransition,
  job: OfficialCertificationJob | undefined,
): boolean {
  if (transition.state === "bootstrap_required") {
    return !job
      && !transition.job_id
      && !transition.certificate_digest
      && typeof transition.command === "string"
      && transition.command.trim().length > 0;
  }
  if (transition.state === "bootstrap_failed") {
    // Invalid, ambiguous, or unavailable durable-job discovery is itself a
    // backend-authoritative failure transition.  It intentionally has no job
    // row, but its checkpoint identity and digest still bind reason/action/
    // command to the exact parked request.
    const failureProjectionValid = typeof transition.reason === "string"
      && transition.reason.trim().length > 0
      && typeof transition.command === "string"
      && transition.command.trim().length > 0
      && !transition.certificate_digest;
    return failureProjectionValid && (
      transition.job_id
        ? Boolean(job && transition.job_id === job.job_id)
        : !job
    );
  }
  if (!job || transition.job_id !== job.job_id) return false;
  if (transition.state === "ready_to_finalize") {
    return typeof transition.certificate_digest === "string"
      && transition.certificate_digest === job.certificate_digest
      && typeof transition.command === "string"
      && transition.command.trim().length > 0;
  }
  return transition.state === "bootstrap_running"
    && transition.command == null
    && !transition.certificate_digest;
}

function bootstrapJobValid(job: OfficialCertificationJob): boolean {
  return job.read_only === true
    && job.cancel_allowed === false
    && job.bootstrap_control_id === "first_strict_control_v1"
    && job.certification_profile === "first_strict_control_v1"
    && job.opponent_authority === "system_control"
    && job.formal_profile?.self_play_rounds === 5
    && job.formal_profile.opponent_rounds === 3
    && job.formal_profile.target_hands === 70
    && job.strength_evidence_weight === 0
    && job.strategy_evidence_weight === 0;
}

function TransitionCard({ transition }: { transition: OperatorTransition }) {
  const styles = transition.state === "bootstrap_failed"
    ? "border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300"
    : transition.state === "ready_to_finalize"
      ? "border-green-300 bg-green-50 text-green-800 dark:border-green-900 dark:bg-green-950/30 dark:text-green-300"
      : "border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300";
  const title = {
    bootstrap_required: "等待操作员启动首代系统控制认证",
    bootstrap_running: "首代系统控制认证运行中",
    bootstrap_failed: "首代系统控制认证失败",
    ready_to_finalize: "证书已验证，等待操作员完成首代发布",
  }[transition.state];

  return (
    <div className={cn("mt-3 rounded border p-3 text-xs", styles)}>
      <p className="font-semibold">{title}</p>
      <p className="mt-1">原因：{transition.reason || "后端未提供原因"}</p>
      <p className="mt-1">动作：{transition.action}</p>
      {transition.command && (
        <code className="mt-2 block overflow-x-auto rounded bg-black/5 p-2 font-mono text-[11px] dark:bg-black/30">{transition.command}</code>
      )}
      {transition.certificate_digest && (
        <p className="mt-2 break-all font-mono text-[10px]">certificate {transition.certificate_digest}</p>
      )}
      <p className="mt-2 text-[10px] opacity-80">
        first_strict_control_v1 · system_control · 强度权重 0 · 策略权重 0
      </p>
      <p className="mt-1 break-all font-mono text-[10px] opacity-70">transition {transition.transition_digest}</p>
    </div>
  );
}

export function OfficialCertificationProgress({
  status,
  className,
}: {
  status: ControlStatus | null;
  className?: string;
}) {
  const stage = status?.active_generation?.stage ?? "";
  const shouldPoll = Boolean(status?.epoch_initialized && isOfficialCertificationStage(stage));
  const generation = status?.active_generation ?? null;
  const { jobsProjection, loading, error } = useOfficialCertificationJobs(
    shouldPoll,
    generation,
  );

  return (
    <OfficialCertificationProgressView
      status={status}
      className={className}
      jobsProjection={jobsProjection}
      loading={loading}
      error={error}
    />
  );
}

/** Pure rendering child used when a page already owns the sole jobs poll. */
export function OfficialCertificationProgressView({
  status,
  className,
  jobsProjection,
  loading,
  error,
}: {
  status: ControlStatus | null;
  className?: string;
  jobsProjection: OfficialCertificationJobsProjection | null;
  loading: boolean;
  error: string | null;
}) {
  const stage = status?.active_generation?.stage ?? "";
  const generation = status?.active_generation ?? null;

  if (!status?.epoch_initialized || !isOfficialCertificationStage(stage)) return null;

  const identityMatches = Boolean(
    jobsProjection
    && generation
    && officialJobsBindingIssues(jobsProjection, generation).length === 0,
  );
  const bootstrap = stage === "official_bootstrap_required";
  const normalJobStage = isNormalOfficialCertificationStage(stage);
  // The status projection owns the initial pause. The exact jobs endpoint may
  // refine that same checkpoint-bound transition to running/failed/finalize;
  // prefer the refined record only after its workflow/candidate identity has
  // matched above.
  const projectedTransition = (identityMatches ? jobsProjection?.operator_transition : null)
    ?? status.operator_transition;
  const boundTransition = bootstrap && transitionMatches(projectedTransition, status)
    ? projectedTransition
    : null;
  const expectedAuthority = bootstrap
    ? "operator_bootstrap_full_v5_job"
    : normalJobStage ? "pipeline_attached_full_v5_job" : null;
  const job = identityMatches && expectedAuthority
    ? jobsProjection?.jobs.find((row) => (
        row.workflow_run_id === generation?.workflow_run_id
        && row.candidate_version === generation?.next_v
        && row.formal_authority === expectedAuthority
        && (!bootstrap || (
          row.read_only === true
          && row.cancel_allowed === false
          && bootstrapJobValid(row)
        ))
      ))
    : undefined;
  const transition = boundTransition && transitionJobBindingValid(boundTransition, job)
    ? boundTransition
    : null;
  // The first-strict candidate carries source_v as numeric-high-water
  // continuity only (cloud: v1 with source_v=0; main historically v143/v142).
  // Certification profile comes only from the content-bound transition/job;
  // version/source arithmetic must never reinterpret system-control as normal.
  const certificationProfile = transition?.certification_profile ?? job?.certification_profile ?? null;
  const firstStrictProfile = certificationProfile === "first_strict_control_v1"
    && (transition?.opponent_authority ?? job?.opponent_authority) === "system_control";
  const normalFullProfile = Boolean(
    job
    && job.formal_authority === "pipeline_attached_full_v5_job"
    && job.certification_profile === "official-full-v5"
    && job.opponent_authority === "strict_published_pool"
    && job.formal_profile?.self_play_rounds === 5
    && job.formal_profile.opponent_rounds === 3
    && job.formal_profile.target_hands === 70
    && job.strength_evidence_weight === 0
    && job.strategy_evidence_weight === 0,
  );
  const progress = job?.progress;
  const requested = progress?.rounds_requested ?? (normalFullProfile ? 8 : 0);
  const completed = progress?.rounds_completed ?? 0;
  const activeHands = Math.min(70, Math.max(0, progress?.active_round?.hands_started ?? 0));
  const progressPct = requested > 0
    ? Math.min(100, ((completed + activeHands / 70) / requested) * 100)
    : 0;
  const verdict = job?.compliance_verdict;
  const outcomeFailed = job?.state === "failed"
    || job?.state === "cancelled"
    || verdict?.blocking === true
    || job?.official_status === "official-failed";
  const certificateDigestValid = typeof job?.certificate_digest === "string"
    && /^[0-9a-f]{64}$/.test(job.certificate_digest);

  return (
    <section className={cn("rounded-xl border border-indigo-200 bg-indigo-50 p-4 dark:border-indigo-900/60 dark:bg-indigo-950/20", className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-indigo-900 dark:text-indigo-200">官方 EXE 正式认证</h3>
          <p className="mt-0.5 text-xs text-indigo-700/80 dark:text-indigo-300/80">
            {firstStrictProfile
              ? `v${generation?.next_v ?? "—"} · first_strict_control_v1 · system-control 合规证书 · 零强度权重`
              : normalFullProfile
                ? `v${generation?.next_v ?? "—"} · official-full-v5 · 5 轮自对弈 + 3 轮合格 strict 对手 × 70 手`
                : `v${generation?.next_v ?? "—"} · 认证 profile 权威投影不可用；不从版本号或 source_v 猜测 5+3/system-control`}
          </p>
        </div>
        <span className="rounded bg-white/70 px-2 py-1 font-mono text-[10px] text-indigo-700 dark:bg-black/20 dark:text-indigo-300">
          {stage}
        </span>
      </div>

      {bootstrap && transition && <TransitionCard transition={transition} />}
      {bootstrap && !transition && (
        <p className="mt-3 text-xs text-red-700 dark:text-red-300">
          首代 operator_transition 权威投影缺失或身份不匹配；不从 operator_action、job.state 或 passed=false 猜测下一命令。
        </p>
      )}

      {stage === "publishing" && (
        <p className="mt-3 text-xs text-indigo-700 dark:text-indigo-300">checkpoint 处于 publishing；页面只展示下方身份匹配 job 的权威状态，不从 stage 反推证书或发布完成。</p>
      )}
      {(stage === "official_failed" || stage === "official_inconclusive") && (
        <p className="mt-3 text-xs text-red-700 dark:text-red-300">checkpoint 处于 {stage}；精确任务状态、verdict 与 issues 只采用下方身份匹配的后端 job 投影，后续动作只由 checkpoint route 决定。</p>
      )}

      {loading && !jobsProjection ? (
        <p className="mt-3 text-xs text-indigo-700 dark:text-indigo-300">读取当前 workflow 附着任务…</p>
      ) : error ? (
        <p className="mt-3 text-xs text-red-700 dark:text-red-300">正式任务投影不可用：{error}</p>
      ) : !job ? (
        <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">
          {bootstrap
            ? transition?.state === "bootstrap_required"
              ? "操作员尚未启动唯一授权的只读 system-control job；这是受控暂停，不是认证失败。"
              : transition?.state === "bootstrap_failed"
                ? "后端已用 digest-bound transition 报告 durable job 发现失败；reason、action 和 command 以上方投影为准。"
              : "尚无与 transition、workflow 和 system-control profile 全部匹配的只读任务。"
            : stage === "publishing"
              ? "发布阶段未返回身份匹配的 job 快照；页面不从 stage 反推证书摘要。"
              : stage === "official_failed" || stage === "official_inconclusive"
                ? "失败/无结论阶段没有身份匹配的 job 快照；精确 status、verdict 与 issues 当前不可用。"
                : "当前 checkpoint 尚无可验证的附着 full-v5 job；HTTP 不搜索或提升旧任务。"}
        </p>
      ) : (
        <div className="mt-3 space-y-2">
          <div className={cn("flex flex-wrap items-center gap-x-4 gap-y-1 text-xs", outcomeFailed ? "text-red-800 dark:text-red-200" : "text-indigo-800 dark:text-indigo-200")}>
            <span>任务 <b>{job.state}</b>{job.phase ? ` / ${job.phase}` : ""}</span>
            <span>正式状态 <b>{job.official_status ?? "权威投影不可用"}</b></span>
            <span>轮次 <b>{completed}/{requested || "—"}</b></span>
            <span>通过 <b>{progress?.rounds_passed ?? 0}</b></span>
            <span>attempt <b>{job.attempt ?? progress?.suite_attempt ?? "—"}</b></span>
          </div>
          {requested > 0 && (
            <div className="h-2 overflow-hidden rounded-full bg-indigo-100 dark:bg-indigo-950">
              <div className={cn("h-full rounded-full transition-all", outcomeFailed ? "bg-red-500" : "bg-indigo-500")} style={{ width: `${progressPct}%` }} />
            </div>
          )}
          {progress?.active_round && (
            <p className="text-xs text-indigo-700 dark:text-indigo-300">
              当前：{roundKind(progress.active_round.kind)} #{progress.active_round.index} · 启动 {progress.active_round.hands_started}/70 手 · 可见结算 {progress.active_round.settlements}
            </p>
          )}
          <div className="space-y-1 text-[10px] text-indigo-700 dark:text-indigo-300">
            <p>profile <b>{job.certification_profile ?? "权威投影不可用"}</b> · opponent <b>{job.opponent_authority ?? "权威投影不可用"}</b></p>
            <p>强度权重 <b>{job.strength_evidence_weight ?? "不可用"}</b> · 策略权重 <b>{job.strategy_evidence_weight ?? "不可用"}</b></p>
            {verdict && <p>deterministic verdict：{verdict.ok ? "ok" : verdict.inconclusive ? "inconclusive" : verdict.blocking ? "blocking" : "not-ready"} · {verdict.classification}</p>}
            {certificateDigestValid && <p className="break-all font-mono">certificate {job.certificate_digest}</p>}
            {job.state === "completed" && !certificateDigestValid && <p className="text-red-600">完成任务缺少有效 certificate_digest 权威投影。</p>}
          </div>
          {job.issues && job.issues.length > 0 && (
            <div className="rounded border border-red-200 bg-red-50 p-2 text-[10px] text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300">
              issues：{job.issues.join("；")}
            </div>
          )}
          <p className="break-all font-mono text-[10px] text-indigo-500">job {job.job_id}</p>
        </div>
      )}
    </section>
  );
}
