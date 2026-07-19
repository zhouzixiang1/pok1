import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { AgentActivityResponse } from "../api/types";
import { useControlStatus } from "../hooks/useControlStatus";
import { useOfficialCertificationJobs } from "../hooks/useOfficialCertificationJobs";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader, EmptyState } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { OfficialCertificationProgress } from "../components/evolution/OfficialCertificationProgress";
import { agentActivityView } from "../domain/agentActivityView";
import {
  evidenceTierForGate,
  evidenceTierForBootstrapJob,
  criticAdvisoryVerdictLabel,
  EVIDENCE_TIER_LABELS,
  type EvidenceAuthorityLabel,
} from "../domain/evidenceAuthority";
import { cn } from "../lib/utils";

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
  const { status, health, loading, error } = useControlStatus(5_000);
  const [agents, setAgents] = useState<AgentActivityResponse | null>(null);
  const { jobsProjection } = useOfficialCertificationJobs(status?.epoch_initialized ?? false);

  useEffect(() => {
    if (!status?.epoch_initialized) { setAgents(null); return; }
    let cancelled = false;
    const refresh = () => api.pipelineAgents().then((v) => { if (!cancelled) setAgents(v); }).catch((e) => {
      if (!cancelled) setAgents(null);
      console.error("[EvidenceGates] agents error:", e);
    });
    refresh();
    const id = setInterval(refresh, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [status?.epoch_initialized]);

  const view = agents ? agentActivityView(agents) : null;
  const gen = status?.active_generation ?? null;
  const pipeline = health?.pipeline ?? null;
  const stability = status?.stability_observation ?? null;

  return (
    <>
      <PageMeta title="证据与质量门 — Bot 自进化" description="分层证据投影" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />

      {!status?.epoch_initialized ? (
        <EmptyState message="epoch 未初始化；证据投影不可用。" />
      ) : (
        <div className="space-y-4">
          {/* Authority tier legend */}
          <Card>
            <CardHeader title="证据权威分层" subtitle="区分合规 / 强度 / advisory / 诊断 / 零权威" />
            <div className="p-3 flex flex-wrap gap-2">
              {(Object.values(EVIDENCE_TIER_LABELS) as EvidenceAuthorityLabel[]).map((tier) => (
                <span key={tier.tier} className={cn("rounded border px-2 py-0.5 text-xs", TONE_CLASS[tier.tone])}>
                  {tier.label}
                </span>
              ))}
            </div>
          </Card>

          {/* Generation identity */}
          <Card>
            <CardHeader title="代次身份" subtitle="canonical identity + workflow attempt" />
            <div className="p-3 grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
              <Field label="generation_ordinal" value={gen?.generation_ordinal != null ? String(gen.generation_ordinal) : "—"} />
              <Field label="canonical_version" value={gen?.canonical_version != null ? String(gen.canonical_version) : "—"} />
              <Field label="canonical_bot_name" value={gen?.canonical_bot_name ?? "—"} mono />
              <Field label="canonical_tag" value={gen?.canonical_tag ?? "—"} mono />
              <Field label="workflow_run_id" value={gen?.workflow_run_id ?? "—"} mono />
              <Field label="checkpoint_revision" value={gen?.checkpoint_revision != null ? String(gen.checkpoint_revision) : "—"} mono />
              <Field label="source_v" value={gen?.source_v != null ? String(gen.source_v) : "—"} mono />
              <Field label="parent2_v" value={gen?.parent2_v != null ? String(gen.parent2_v) : "(单亲)"} mono />
            </div>
          </Card>

          {/* Gates */}
          <Card>
            <CardHeader title="质量门状态" subtitle="quality · review · critic(advisory) · precommit_eval · official_full" />
            <div className="p-3 space-y-2">
              {!view || !view.available ? (
                <p className="text-xs text-gray-400">当前无 strict workflow；无 gate 记录。</p>
              ) : (
                <>
                  <GateRow label="quality" gate={view.gates.quality} />
                  <GateRow label="review" gate={view.gates.review} />
                  <GateRow label="critic (advisory)" gate={view.gates.critic} advisory />
                  <GateRow label="precommit_eval" gate={view.gates.precommit_eval} />
                  <GateRow label="official_full" gate={view.gates.official_full} />
                </>
              )}
            </div>
          </Card>

          {/* Official certification progress */}
          <Card>
            <CardHeader title="官方认证进度" subtitle="official-full-v5 · 5 自对弈 + 3 合格对手 × 70 手" />
            <div className="p-3">
              <OfficialCertificationProgress status={status} />
              {jobsProjection && jobsProjection.jobs.length > 0 && (
                <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-800 space-y-1">
                  {jobsProjection.jobs.map((job) => {
                    const tier = evidenceTierForBootstrapJob(job);
                    return (
                      <div key={job.job_id} className="text-xs flex items-center gap-2">
                        <Badge variant="neutral" size="sm">{job.state}</Badge>
                        <span className={cn("rounded border px-1.5 py-0.5", TONE_CLASS[tier.tone])}>{tier.label}</span>
                        <span className="font-mono text-gray-500 truncate">{job.job_id}</span>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </Card>

          {/* Pipeline recovery evidence */}
          <Card>
            <CardHeader title="流水线恢复证据" subtitle="来自 /api/control/health.pipeline" />
            <div className="p-3 space-y-1 text-xs">
              <Field label="pipeline.authority" value={pipeline?.authority ?? "—"} mono />
              <Field label="pipeline.recovery_blocked" value={String(pipeline?.recovery_blocked ?? false)} mono />
              <Field label="pipeline.identity_changed" value={String(pipeline?.identity_changed ?? false)} mono />
              {pipeline?.identity_mismatches && pipeline.identity_mismatches.length > 0 && (
                <div className="text-error-600 dark:text-error-400">
                  identity_mismatches：{pipeline.identity_mismatches.join(", ")}
                </div>
              )}
              {pipeline?.gate_outcome && (
                <div className="rounded border border-error-300 dark:border-error-800 bg-error-50 dark:bg-error-950/30 p-2 mt-1">
                  <div className="font-semibold text-error-700 dark:text-error-300">
                    终局 gate 拒绝：{pipeline.gate_outcome.gate_name}
                  </div>
                  <div className="text-gray-600 dark:text-gray-300">
                    reason：{pipeline.gate_outcome.reason_code} · receipt：{pipeline.gate_outcome.receipt_digest?.slice(0, 12)}…
                  </div>
                </div>
              )}
            </div>
          </Card>

          {/* Strength evidence summary */}
          <Card>
            <CardHeader title="强度证据摘要" subtitle="immutable 70-hand rating cycle" />
            <div className="p-3 space-y-1 text-xs">
              <Field label="已发布 Bot 数" value={String(status?.active_bots.length ?? 0)} />
              <Field label="严格代次" value={String(status?.strict_generation_count ?? 0)} />
              <Field label="连续验收" value={stability ? `${stability.count}/${stability.target}` : "—"} />
              <Field
                label="strength_cycle_ready"
                value={String(stability?.strength_cycle?.ready ?? false)}
                mono
              />
              {stability?.strength_cycle?.reason && (
                <p className="text-gray-500 dark:text-gray-400 italic">{stability.strength_cycle.reason}</p>
              )}
            </div>
          </Card>
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

function GateRow({ label, gate, advisory }: {
  label: string;
  gate: { complete: boolean; fields: Record<string, unknown> } | null;
  advisory?: boolean;
}) {
  if (!gate) {
    return (
      <div className="flex items-center gap-2 text-xs">
        <Badge variant="neutral" size="sm">未运行</Badge>
        <span className="text-gray-600 dark:text-gray-300">{label}</span>
        <span className="text-gray-400 ml-auto">尚无 gate 记录</span>
      </div>
    );
  }
  const tier = evidenceTierForGate(advisory ? { name: "critic", present: true, complete: gate.complete, fields: gate.fields } : { name: label as "quality", present: true, complete: gate.complete, fields: gate.fields });
  const verdict = advisory ? criticAdvisoryVerdictLabel({ name: "critic", present: true, complete: gate.complete, fields: gate.fields }) : null;
  return (
    <div className="flex items-center gap-2 text-xs">
      <Badge variant={gate.complete ? "success" : "warning"} size="sm">{gate.complete ? "完成" : "未完成"}</Badge>
      <span className={cn("rounded border px-1.5 py-0.5", TONE_CLASS[tier.tone])}>{tier.label}</span>
      <span className="text-gray-600 dark:text-gray-300">{label}</span>
      {verdict && <span className="text-gray-500 ml-2">{verdict.verdict}</span>}
      <span className="text-gray-400 ml-auto truncate">{summaryOfGate(gate.fields)}</span>
    </div>
  );
}

function summaryOfGate(fields: Record<string, unknown>): string {
  const parts: string[] = [];
  if (typeof fields.all_passed === "boolean") parts.push(`all_passed=${fields.all_passed}`);
  if (typeof fields.critical_scenarios_passed === "boolean") parts.push(`critical=${fields.critical_scenarios_passed}`);
  if (typeof fields.passed === "boolean") parts.push(`passed=${fields.passed}`);
  if (typeof fields.decision_pass_rate === "number") parts.push(`decision=${Math.round(fields.decision_pass_rate * 100)}%`);
  if (typeof fields.advisory_score === "number") parts.push(`advisory_score=${fields.advisory_score}`);
  if (typeof fields.quality_score === "number") parts.push(`quality_score=${fields.quality_score}`);
  return parts.join(" · ");
}
