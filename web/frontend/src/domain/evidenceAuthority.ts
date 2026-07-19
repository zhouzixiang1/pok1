import type { AgentGateView } from "../api/types.js";
import type {
  OfficialCertification,
  OfficialCertificationJob,
} from "../api/types.js";
import { criticAdvisoryComplete } from "../lib/pipelinePresentation.js";

/**
 * Evidence authority tiers, ordered from highest to lowest weight.
 *
 * The dashboard renders each evidence row with its tier so an operator never
 * mistakes an advisory critic verdict, a diagnostic Arena run, or a zero-
 * authority legacy artifact for a compliance/strength authority.  This mirrors
 * the runbook: official EXE and Arena have distinct, documented weights.
 */
export type EvidenceAuthorityTier =
  | "compliance" // official-full-v5 certificate, capability probe, native precommit
  | "strength" // immutable 70-hand rating cycle, selection rows
  | "advisory" // schema-valid critic, review feedback (non-blocking)
  | "diagnostic" // Arena sessions, local probes (zero strength weight)
  | "zero"; // archived / retired / unbound rows (must not appear as authority)

export interface EvidenceAuthorityLabel {
  tier: EvidenceAuthorityTier;
  label: string;
  /** Tailwind-flavoured token stem (e.g. "success" -> text-success-600). */
  tone: "success" | "info" | "warning" | "neutral" | "error";
}

export const EVIDENCE_TIER_LABELS: Record<EvidenceAuthorityTier, EvidenceAuthorityLabel> = {
  compliance: { tier: "compliance", label: "合规证据", tone: "success" },
  strength: { tier: "strength", label: "强度证据", tone: "info" },
  advisory: { tier: "advisory", label: "advisory（非门）", tone: "warning" },
  diagnostic: { tier: "diagnostic", label: "诊断（零强度权重）", tone: "neutral" },
  zero: { tier: "zero", label: "零权威（不得采纳）", tone: "error" },
};

export function evidenceTierForGate(gate: AgentGateView | null): EvidenceAuthorityLabel {
  if (!gate) return EVIDENCE_TIER_LABELS.zero;
  if (gate.name === "critic") return EVIDENCE_TIER_LABELS.advisory;
  if (gate.name === "review") return EVIDENCE_TIER_LABELS.advisory;
  if (gate.name === "quality" || gate.name === "precommit_eval") {
    return gate.complete ? EVIDENCE_TIER_LABELS.compliance : EVIDENCE_TIER_LABELS.advisory;
  }
  if (gate.name === "official_full") {
    return gate.complete ? EVIDENCE_TIER_LABELS.compliance : EVIDENCE_TIER_LABELS.zero;
  }
  return EVIDENCE_TIER_LABELS.advisory;
}

export function evidenceTierForOfficialCertification(
  cert: OfficialCertification | null | undefined,
): EvidenceAuthorityLabel {
  if (!cert) return EVIDENCE_TIER_LABELS.zero;
  if (cert.formal_certified === true && cert.formal_authority === "signed_full_v5") {
    return EVIDENCE_TIER_LABELS.compliance;
  }
  if (cert.formal_authority && cert.formal_authority !== "none") {
    return EVIDENCE_TIER_LABELS.advisory;
  }
  return EVIDENCE_TIER_LABELS.zero;
}

export function evidenceTierForBootstrapJob(
  job: OfficialCertificationJob | null | undefined,
): EvidenceAuthorityLabel {
  if (!job) return EVIDENCE_TIER_LABELS.zero;
  if (job.formal_authority === "operator_bootstrap_full_v5_job") {
    // First-strict operator control is explicitly zero strength/strategy weight.
    return { tier: "zero", label: "首代 operator bootstrap（零强度权重）", tone: "neutral" };
  }
  if (job.formal_authority === "pipeline_attached_full_v5_job") {
    return EVIDENCE_TIER_LABELS.advisory;
  }
  return EVIDENCE_TIER_LABELS.zero;
}

/**
 * Project the critic gate into an advisory verdict label.  Re-uses the shared
 * ``criticAdvisoryComplete`` so this view never disagrees with the pipeline
 * status component about what "completed" means.
 */
export function criticAdvisoryVerdictLabel(gate: AgentGateView | null): {
  complete: boolean;
  verdict: string;
} {
  if (!gate) return { complete: false, verdict: "Critic 未运行" };
  const complete = criticAdvisoryComplete(gate.fields);
  const advisory = gate.fields.advisory_approved;
  const verdict = !complete
    ? "Critic 未完成（advisory 结论不可用）"
    : advisory === true
      ? "建议支持"
      : advisory === false
        ? "建议保留意见"
        : "建议结论不可用";
  return { complete, verdict };
}
