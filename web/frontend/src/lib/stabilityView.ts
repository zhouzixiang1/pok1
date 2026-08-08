import type { StabilityObservation } from "../api/control";

type BadgeVariant = "success" | "warning" | "error" | "neutral";

export interface StabilityPresentation {
  label: string;
  detail: string;
  variant: BadgeVariant;
}

export function stabilityPresentation(value: StabilityObservation | null | undefined): StabilityPresentation {
  if (!value) {
    return { label: "验证投影不可用", detail: "后端未返回连续性观测。", variant: "error" };
  }
  if (
    value.schema_version !== 1
    || value.kind !== "national-tcp-uninterrupted-evolution-observation"
    || value.authority !== "operator_acceptance_only"
    || value.strategy_evidence_weight !== 0
    || value.strength_evidence_weight !== 0
  ) {
    return { label: "验证投影不可用", detail: "连续性观测 schema 或零权重权威字段不匹配。", variant: "error" };
  }
  const verification = value.verification;
  if (!verification) {
    return { label: "验证投影不可用", detail: "缺少后台连续性验证快照。", variant: "error" };
  }
  if (verification.state === "pending") {
    return { label: "连续性验证中", detail: "远端和强度身份尚未完成本轮验证。", variant: "warning" };
  }
  if (verification.state === "stale") {
    return { label: "连续性验证已过期", detail: "上次验证已超出有效期；旧结果不延续绿色状态。", variant: "error" };
  }
  if (verification.state === "failed") {
    return {
      label: "连续性验证失败",
      detail: verification.error || value.errors[0] || "后台无法验证当前连续观测身份。",
      variant: "error",
    };
  }
  const verificationAuthority = verification.authority;
  if (
    verification.state === "fresh"
    && (
      !verificationAuthority
      || verificationAuthority.evaluation_epoch !== "national_tcp_policy_v1"
      || !/^[0-9a-f]{64}$/.test(verificationAuthority.epoch_stream_authority_digest || "")
      || !/^[0-9a-f]{40}$/.test(verificationAuthority.repository_head)
      || !verificationAuthority.repository_branch
      || verificationAuthority.repository_branch === "HEAD"
    )
  ) {
    return { label: "验证投影不可用", detail: "连续性快照未绑定当前 epoch 与仓库 HEAD。", variant: "error" };
  }
  if (
    verification.state === "fresh"
    && (
      typeof verification.checked_at !== "number"
      || !Number.isFinite(verification.checked_at)
      || typeof verification.fresh_until !== "number"
      || !Number.isFinite(verification.fresh_until)
      || verification.fresh_until <= Date.now() / 1000
    )
  ) {
    return { label: "连续性验证已过期", detail: "fresh 验证快照已超过后端声明的有效期。", variant: "error" };
  }
  if (
    verification.state !== "fresh"
    || !Number.isFinite(value.count)
    || !Number.isFinite(value.target)
    || value.count < 0
    || value.target <= 0
    || value.count > value.target
    || value.remaining !== value.target - value.count
  ) {
    return { label: "验证投影不可用", detail: "连续性观测字段不完整或互相矛盾。", variant: "error" };
  }

  switch (value.status) {
    case "not_started":
      return {
        label: "尚未开始",
        detail: value.last_reset_reason
          ? `最近一次持久化重置：${value.last_reset_reason}`
          : "尚无修复或重启后的合格发布记录。",
        variant: "neutral",
      };
    case "reset_required":
      return {
        label: "已持久化归零",
        detail: value.last_reset_reason || value.errors[0] || "连续性已失效，必须从下一次合格发布重新计数。",
        variant: "error",
      };
    case "observing":
      return value.continuity_valid && !value.complete
        ? {
            label: `观测中 ${value.count}/${value.target}`,
            detail: `还需 ${value.remaining} 个连续合格代次；修复、重启或身份漂移会持久化归零。`,
            variant: "warning",
          }
        : { label: "验证投影不可用", detail: "observing 状态与连续性字段矛盾。", variant: "error" };
    case "awaiting_strength_cycle":
      return value.continuity_valid && !value.complete && value.count === value.target
        ? {
            label: "代次达标，等待强度周期",
            detail: value.strength_cycle.reason || "等待最新 Bot 进入当前 immutable 70 手强度周期。",
            variant: "warning",
          }
        : { label: "验证投影不可用", detail: "等待强度周期状态与计数不一致。", variant: "error" };
    case "complete":
      return value.continuity_valid
        && value.complete
        && value.strength_cycle_ready
        && value.count === value.target
        ? {
            label: `连续验收完成 ${value.count}/${value.target}`,
            detail: "最新 Bot 已进入身份匹配的 immutable 70 手强度周期。",
            variant: "success",
          }
        : { label: "验证投影不可用", detail: "complete 状态缺少连续性或强度周期证明。", variant: "error" };
  }
}
