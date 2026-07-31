import { useControlStatusValue } from "../context/DataProvider";
import { useOfficialCertificationJobs } from "../hooks/useOfficialCertificationJobs";
import { useBoundAgentActivity } from "../hooks/useBoundAgentActivity";
import PageMeta from "../components/common/PageMeta";
import { EmptyState } from "../components/shared";
import { EvolutionPageHeader } from "../components/evolution/EvolutionPageHeader";
import { PhaseAProjectionStrip } from "../components/evolution/PhaseAProjectionStrip";
import { OfficialCertificationProgressView } from "../components/evolution/OfficialCertificationProgress";
import { OperatorSituation } from "../components/evolution/OperatorSituation";
import {
  EvolutionSection,
  EvolutionStatusBadge,
  EvolutionSurface,
} from "../components/evolution/ui";
import type { EvolutionStatusTone } from "../components/evolution/ui";
import { agentActivityView } from "../domain/agentActivityView";
import {
  evidenceTierForGate,
  evidenceTierForBootstrapJob,
  criticAdvisoryVerdictLabel,
  EVIDENCE_TIER_LABELS,
  type EvidenceAuthorityLabel,
} from "../domain/evidenceAuthority";
import { operatorSituationView } from "../domain/operatorSituationView";
import { cn } from "../lib/utils";
import {
  isOfficialCertificationStage,
  officialJobsBindingIssues,
} from "../api/officialJobs";

const TONE_CLASS: Record<EvidenceAuthorityLabel["tone"], string> = {
  success: "text-success-600 dark:text-success-400 border-success-300 dark:border-success-800",
  info: "text-brand-600 dark:text-brand-400 border-brand-300 dark:border-brand-800",
  warning: "text-warning-600 dark:text-warning-400 border-warning-300 dark:border-warning-800",
  neutral: "text-gray-600 dark:text-gray-300 border-gray-300 dark:border-gray-700",
  error: "text-error-600 dark:text-error-400 border-error-300 dark:border-error-800",
};

/**
 * Evidence & Gates — structured proof-of-publication view, tiered by authority.
 *
 * Every row is tagged with its evidence tier (compliance / strength / advisory
 * / diagnostic / zero) so an operator never mistakes an advisory critic
 * verdict or a diagnostic Arena run for compliance or strength authority.
 */
export default function EvidenceGates() {
  const { status, health, loading, error, lastUpdated } = useControlStatusValue();
  const { agents } = useBoundAgentActivity(
    status?.active_generation,
    status?.epoch_initialized === true,
  );
  const view = agents ? agentActivityView(agents, health?.pipeline?.route) : null;
  const {
    jobsProjection,
    loading: jobsLoading,
    error: jobsError,
  } = useOfficialCertificationJobs(
    Boolean(
      status?.epoch_initialized
      && isOfficialCertificationStage(status.active_generation?.stage)
      && view?.available === true
      && view.officialJobsPollingSupported
    ),
    status?.active_generation,
  );
  const gen = status?.active_generation ?? null;
  const pipeline = health?.pipeline ?? null;
  const stability = status?.stability_observation ?? null;
  const boundJobsProjection = jobsProjection
    && gen
    && officialJobsBindingIssues(jobsProjection, gen).length === 0
    ? jobsProjection
    : null;
  const situation = operatorSituationView(status, health);

  return (
    <div className="space-y-4">
      <PageMeta title="发布资格 — Bot 自进化" description="哪些门已通过、还缺什么证据" />
      <EvolutionPageHeader
        title="发布资格"
        subtitle="官方兼容、真实强度、建议与诊断分轨"
        status={status}
        health={health}
        loading={loading}
        error={error}
        lastUpdated={lastUpdated}
        variant="compact"
      />
      <PhaseAProjectionStrip
        status={status}
        manualRequired={situation?.manualRequired === true}
      />
      <OperatorSituation status={status} health={health} />

      {!status?.epoch_initialized ? (
        <EmptyState message="严格进化尚未初始化；当前没有可验证的发布资格证据。" />
      ) : (
        <>
          <EvolutionSurface padding="sm">
            <EvolutionSection
              title="不同证据能证明什么"
              subtitle="官方兼容、真实强度、建议和诊断不能互相替代"
            />
            <div className="mt-3 flex flex-wrap gap-2">
              {(Object.values(EVIDENCE_TIER_LABELS) as EvidenceAuthorityLabel[]).map((tier) => (
                <span key={tier.tier} className={cn("rounded-md border px-2 py-0.5 text-xs", TONE_CLASS[tier.tone])}>
                  {tier.label}
                </span>
              ))}
            </div>
            <div className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              Official 5+3 只决定官方平台兼容与发布资格，强度权重始终为 0；只有当前评测身份绑定的完整 70 手 native TCP 样本才能证明强度。
            </div>
          </EvolutionSurface>

          <EvolutionSurface padding="sm">
            <EvolutionSection title="本次发布对象" subtitle="用户看到的代次与不可改写的真实发布身份" />
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs md:grid-cols-4">
              <Field label="网页代次" value={gen?.generation_ordinal != null ? `第 ${gen.generation_ordinal} 代` : "—"} />
              <Field label="真实版本" value={gen?.canonical_version != null ? `v${gen.canonical_version}` : "—"} />
              <Field label="Bot 名称" value={gen?.canonical_bot_name ?? "—"} mono />
              <Field label="发布 tag" value={gen?.canonical_tag ?? "—"} mono />
              <Field label="本次工作流" value={gen?.workflow_run_id ?? "—"} mono />
              <Field label="状态修订号" value={gen?.checkpoint_revision != null ? String(gen.checkpoint_revision) : "—"} mono />
              <Field label="主父本" value={gen?.source_v != null ? `v${gen.source_v}` : "无（greenfield）"} mono />
              <Field label="第二父本" value={gen?.parent2_v != null ? `v${gen.parent2_v}` : "无"} mono />
            </div>
          </EvolutionSurface>

          <EvolutionSurface padding="sm">
            <EvolutionSection title="离发布还差哪些门" subtitle="Critic 只给建议；其余门按状态机合同决定是否继续" />
            <div className="mt-3 space-y-2">
              {!view || !view.available ? (
                <p className="text-xs text-gray-400">当前没有可验证的严格工作流或门禁记录。</p>
              ) : (
                <>
                  <GateRow label="代码与策略质量检查" gateName="quality" gate={view.gates.quality} />
                  <GateRow label="独立代码审核" gateName="review" gate={view.gates.review} />
                  <GateRow label="Critic 风险建议（不决定发布）" gateName="critic" gate={view.gates.critic} advisory />
                  <GateRow label="原生 TCP 预发布评测" gateName="precommit_eval" gate={view.gates.precommit_eval} />
                  <GateRow label="官方平台完整认证" gateName="official_full" gate={view.gates.official_full} />
                </>
              )}
            </div>
          </EvolutionSurface>

          <EvolutionSurface padding="sm">
            <EvolutionSection
              title="官方平台兼容认证"
              subtitle="每轮 70 手；首代为 5 自对弈 + 3 system-control，后续为 5 自对弈 + 3 合格 strict 对手；强度权重为 0"
            />
            <div className="mt-3">
              <OfficialCertificationProgressView
                status={status}
                jobsProjection={boundJobsProjection}
                loading={jobsLoading}
                error={jobsError}
              />
              {boundJobsProjection && boundJobsProjection.jobs.length > 0 && (
                <div className="mt-3 space-y-1 border-t border-gray-100 pt-3 dark:border-gray-800">
                  {boundJobsProjection.jobs.filter((job) => (
                    job.workflow_run_id === gen?.workflow_run_id
                    && job.candidate_version === gen?.next_v
                  )).map((job) => {
                    const tier = evidenceTierForBootstrapJob(job);
                    return (
                      <div key={job.job_id} className="flex items-center gap-2 text-xs">
                        <EvolutionStatusBadge tone="neutral">{job.state}</EvolutionStatusBadge>
                        <span className={cn("rounded-md border px-1.5 py-0.5", TONE_CLASS[tier.tone])}>{tier.label}</span>
                        <span className="truncate font-mono text-gray-500">{job.job_id}</span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </EvolutionSurface>

          <EvolutionSurface padding="sm">
            <EvolutionSection title="恢复与身份核对" subtitle="只有这里无冲突，状态机才允许继续" />
            <div className="mt-3 space-y-1 text-xs">
              <Field label="权威来源" value={pipeline?.authority ?? "—"} mono />
              <Field label="恢复被阻断" value={(pipeline?.recovery_blocked ?? false) ? "是" : "否"} />
              <Field label="身份发生变化" value={(pipeline?.identity_changed ?? false) ? "是" : "否"} />
              {pipeline?.identity_mismatches && pipeline.identity_mismatches.length > 0 && (
                <div className="text-error-600 dark:text-error-400">
                  身份不一致字段：{pipeline.identity_mismatches.join(", ")}
                </div>
              )}
              {pipeline?.gate_outcome && (
                <div className="mt-1 rounded-md border border-error-300 bg-error-50 p-2 dark:border-error-800 dark:bg-error-950/30">
                  <div className="font-semibold text-error-700 dark:text-error-300">
                    本次尝试被终局门拒绝：{pipeline.gate_outcome.gate_name}
                  </div>
                  <div className="text-gray-600 dark:text-gray-300">
                    原因：{pipeline.gate_outcome.reason_code} · 受控收据：{pipeline.gate_outcome.receipt_digest?.slice(0, 12)}…
                  </div>
                </div>
              )}
            </div>
          </EvolutionSurface>

          <EvolutionSurface padding="sm">
            <EvolutionSection title="真实强度证据" subtitle="只认完整 70 手 native TCP 的不可变评分周期" />
            <div className="mt-3 space-y-1 text-xs">
              <Field label="已发布 Bot 数" value={String(status?.active_bots.length ?? 0)} />
              <Field label="已发布严格代次" value={String(status?.strict_generation_count ?? 0)} />
              <Field label="连续稳定代次" value={stability ? `${stability.count}/${stability.target}` : "—"} />
              <Field
                label="强度周期可用"
                value={(stability?.strength_cycle?.ready ?? false) ? "是" : "否"}
              />
              {stability?.strength_cycle?.reason && (
                <p className="italic text-gray-500 dark:text-gray-400">{stability.strength_cycle.reason}</p>
              )}
            </div>
          </EvolutionSurface>
        </>
      )}
    </div>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-2 border-b border-gray-50 py-0.5 dark:border-gray-900">
      <span className="shrink-0 text-gray-500 dark:text-gray-400">{label}</span>
      <span className={cn("truncate text-right text-gray-800 dark:text-gray-200", mono && "font-mono")}>{value}</span>
    </div>
  );
}

function GateRow({ label, gateName, gate, advisory }: {
  label: string;
  gateName: "quality" | "review" | "critic" | "precommit_eval" | "official_full";
  gate: { complete: boolean; authority_state: "current" | "historical_invalidated"; fields: Record<string, unknown> } | null;
  advisory?: boolean;
}) {
  if (!gate) {
    return (
      <div className="flex items-center gap-2 text-xs">
        <EvolutionStatusBadge tone="neutral">未运行</EvolutionStatusBadge>
        <span className="text-gray-600 dark:text-gray-300">{label}</span>
        <span className="ml-auto text-gray-400">尚未到达</span>
      </div>
    );
  }
  const projectedGate = { name: gateName, present: true as const, complete: gate.complete, authority_state: gate.authority_state, fields: gate.fields };
  const historical = gate.authority_state === "historical_invalidated";
  const tier = evidenceTierForGate(projectedGate);
  const verdict = advisory && !historical
    ? criticAdvisoryVerdictLabel({ ...projectedGate, name: "critic" })
    : null;
  const tone: EvolutionStatusTone = historical ? "error" : gate.complete ? "ok" : "warn";
  return (
    <div className="flex items-center gap-2 text-xs">
      <EvolutionStatusBadge tone={tone}>
        {historical ? "历史记录已失效" : gate.complete ? "完成" : "未完成"}
      </EvolutionStatusBadge>
      <span className={cn("rounded-md border px-1.5 py-0.5", TONE_CLASS[tier.tone])}>{tier.label}</span>
      <span className="text-gray-600 dark:text-gray-300">{label}</span>
      {verdict && <span className="ml-2 text-gray-500">{verdict.verdict}</span>}
      <span className="ml-auto truncate text-gray-400">
        {historical ? "候选正在修复，必须重新运行此门" : summaryOfGate(gate.fields)}
      </span>
    </div>
  );
}

function summaryOfGate(fields: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof fields.all_passed === "boolean") parts.push(fields.all_passed ? "全部检查通过" : "仍有检查未通过");
  if (typeof fields.critical_scenarios_passed === "boolean") parts.push(fields.critical_scenarios_passed ? "关键场景通过" : "关键场景未通过");
  if (typeof fields.passed === "boolean") parts.push(fields.passed ? "通过" : "未通过");
  if (typeof fields.decision_pass_rate === "number") parts.push(`决策通过率 ${Math.round(fields.decision_pass_rate * 100)}%`);
  if (typeof fields.advisory_score === "number") parts.push(`建议评分 ${fields.advisory_score}`);
  if (typeof fields.quality_score === "number") parts.push(`质量评分 ${fields.quality_score}`);
  return parts.join(" · ");
}
