import type { OfficialCertification } from "../api/types.js";
import {
  evidenceTierForOfficialCertification,
  type EvidenceAuthorityLabel,
} from "./evidenceAuthority.js";

/**
 * Shared certification presentation for Bot Inventory / Bot Manager.
 *
 * Formal signed-full-v5 remains the only compliance pass. Staging
 * (``publication_tier=staging`` / ``formal_authority=staging_uncertified`` /
 * ``status=official-staging``) is a distinct published-but-awaiting-cert tier,
 * never shown as “未认证” zero authority and never as compliance.
 */

export interface CertificationView {
  formal: boolean;
  label: string;
  detail: string;
  tone: string;
  /** Same tier Inventory / EvidenceGates use via evidenceAuthority. */
  evidence: EvidenceAuthorityLabel;
  /** Two-tier publication identity; null when neither cert nor bot projects a tier. */
  publicationTier: "staging" | "certified" | null;
  /** Certified-tier annotated tag when projected (null until async cert completes). */
  certifiedTag: string | null;
}

export interface CertificationViewOptions {
  /** Bot-summary publication_tier when cert payload omits it. */
  publication_tier?: "staging" | "certified" | null;
  /** Bot-summary certified_tag (only after async official cert). */
  certified_tag?: string | null;
}

const isDigest = (value: unknown): value is string => (
  typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
);

const TONE = {
  formal:
    "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-900/20 dark:text-emerald-300",
  staging:
    "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-300",
  diagnostic:
    "border-cyan-200 bg-cyan-50 text-cyan-700 dark:border-cyan-800 dark:bg-cyan-900/20 dark:text-cyan-300",
  pending:
    "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-300",
  failed:
    "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300",
  neutral:
    "border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-300",
  warning:
    "border-orange-200 bg-orange-50 text-orange-700 dark:border-orange-800 dark:bg-orange-900/20 dark:text-orange-300",
} as const;

function isFormalSignedFull(certification: OfficialCertification): boolean {
  const ledgerEntry = certification.official_verdict_ledger_entry;
  return certification.formal_certified === true
    && certification.formal_authority === "signed_full_v5"
    && certification.mode === "full"
    && certification.policy_id === "official-full-v5"
    && isDigest(certification.certificate_digest)
    && isDigest(certification.certificate_signature_sha256)
    && isDigest(certification.published_attestation_digest)
    && isDigest(ledgerEntry?.entry_digest)
    && ledgerEntry?.certificate_digest === certification.certificate_digest
    && ledgerEntry?.policy_id === certification.policy_id
    && ledgerEntry?.outcome === "official-certified";
}

function resolvePublicationTier(
  certification: OfficialCertification | null | undefined,
  options: CertificationViewOptions | undefined,
  formal: boolean,
  staging: boolean,
): "staging" | "certified" | null {
  if (formal) return "certified";
  if (staging) return "staging";
  const tier = options?.publication_tier ?? certification?.publication_tier ?? null;
  return tier === "staging" || tier === "certified" ? tier : null;
}

function withTierFields(
  view: Omit<CertificationView, "publicationTier" | "certifiedTag">,
  certification: OfficialCertification | null | undefined,
  options: CertificationViewOptions | undefined,
  staging: boolean,
): CertificationView {
  const certifiedTag = (
    typeof options?.certified_tag === "string" && options.certified_tag.length > 0
      ? options.certified_tag
      : null
  );
  return {
    ...view,
    publicationTier: resolvePublicationTier(certification, options, view.formal, staging),
    certifiedTag,
  };
}

export function certificationView(
  certification?: OfficialCertification | null,
  options?: CertificationViewOptions,
): CertificationView {
  const evidence = evidenceTierForOfficialCertification(certification ?? null);

  if (!certification) {
    const stagingOnly = options?.publication_tier === "staging";
    if (stagingOnly) {
      return withTierFields({
        formal: false,
        label: "已发布 / 待认证",
        detail: "staging tag 已发布；异步官方认证尚未形成 signed_full_v5，不算合规证据。",
        tone: TONE.staging,
        evidence: evidence.tier === "staging" ? evidence : {
          tier: "staging",
          label: "已发布/待认证",
          tone: "warning",
        },
      }, null, options, true);
    }
    return withTierFields({
      formal: false,
      label: "未认证",
      detail: "没有 signed official-full-v5 证书。",
      tone: TONE.neutral,
      evidence,
    }, null, options, false);
  }

  // The signed certificate, deterministic receipt, candidate content, and
  // verdict ledger are validated server-side.  Browser code must not create a
  // second, inevitably weaker certification oracle from raw JSON fields.
  if (isFormalSignedFull(certification)) {
    const firstStrictControl = certification.certification_profile === "first_strict_control_v1"
      && certification.opponent_authority === "system_control"
      && certification.strength_evidence_weight === 0
      && certification.strategy_evidence_weight === 0;
    const normalFull = certification.certification_profile === "official-full-v5"
      && certification.opponent_authority === "strict_published_pool"
      && certification.formal_summary?.self_play_rounds === 5
      && certification.formal_summary.opponent_rounds === 3
      && certification.formal_summary.target_hands === 70
      && certification.strength_evidence_weight === 0
      && certification.strategy_evidence_weight === 0;
    if (firstStrictControl || normalFull) {
      return withTierFields({
        formal: true,
        label: firstStrictControl ? "首代系统控制证书通过" : "正式认证通过",
        detail: firstStrictControl
          ? "first_strict_control_v1：system-control 仅证明官方协议合规；强度与策略证据权重均为 0。"
          : "signed official-full-v5：5 轮自对弈 + 3 轮合格 strict 对手，每轮 70 手。",
        tone: TONE.formal,
        evidence,
      }, certification, options, false);
    }
    return withTierFields({
      formal: false,
      label: "正式证书身份投影不完整",
      detail: "formal_certified 存在，但 profile、对手权威、5/3/70 或零权重字段不匹配；不显示为正式通过，也不猜测为普通 5+3。",
      tone: TONE.failed,
      evidence,
    }, certification, options, false);
  }

  // Staging before any zero/"未认证" fallback so Inventory and BotManager agree.
  if (
    evidence.tier === "staging"
    || certification.status === "official-staging"
    || certification.publication_tier === "staging"
    || options?.publication_tier === "staging"
    || certification.formal_authority === "staging_uncertified"
  ) {
    return withTierFields({
      formal: false,
      label: "已发布 / 待认证",
      detail: "staging tag 已发布；异步官方认证尚未形成 signed_full_v5，不算合规证据。",
      tone: TONE.staging,
      evidence: evidence.tier === "staging" ? evidence : {
        tier: "staging",
        label: "已发布/待认证",
        tone: "warning",
      },
    }, certification, options, true);
  }

  if (certification.status === "official-smoke-pass") {
    return withTierFields({
      formal: false,
      label: "Smoke 诊断通过（非认证）",
      detail: "Smoke 只验证短程官方平台诊断，不能发布 Bot。",
      tone: TONE.diagnostic,
      evidence,
    }, certification, options, false);
  }
  if (certification.status === "official-compliance-pass") {
    return withTierFields({
      formal: false,
      label: "Compliance 诊断通过（非认证）",
      detail: "短程 compliance 不是 signed 5+3×70 正式证书。",
      tone: TONE.diagnostic,
      evidence,
    }, certification, options, false);
  }
  if (certification.status === "local-pass") {
    return withTierFields({
      formal: false,
      label: "本地诊断通过（非认证）",
      detail: "本地 raw TCP 强度/合规门不能替代官方 Windows EXE。",
      tone: TONE.neutral,
      evidence,
    }, certification, options, false);
  }
  if (certification.status === "official-pending") {
    const full = certification.mode === "full";
    return withTierFields({
      formal: false,
      label: full ? "Full 正式认证进行中" : "诊断任务进行中（非认证）",
      detail: full ? "尚未形成签名证书，当前不能显示为通过。" : "该任务不会形成正式发布资格。",
      tone: TONE.pending,
      evidence,
    }, certification, options, false);
  }
  if (certification.status === "official-certified") {
    return withTierFields({
      formal: false,
      label: "正式权威未验证",
      detail: "记录声称 certified，但后端未验证为当前发布物的 signed full-v5；按非认证处理。",
      tone: TONE.failed,
      evidence,
    }, certification, options, false);
  }

  const labels: Partial<Record<OfficialCertification["status"], string>> = {
    "official-failed": "正式认证失败",
    "official-inconclusive": "正式认证无结论",
    "official-unavailable": "认证状态不可用",
    "official-uncertified": "未认证",
  };
  return withTierFields({
    formal: false,
    label: labels[certification.status] ?? "未认证",
    detail: certification.reason || "没有可验证的 signed official-full-v5 证书。",
    tone: certification.status === "official-failed" ? TONE.failed : TONE.warning,
    evidence,
  }, certification, options, false);
}
