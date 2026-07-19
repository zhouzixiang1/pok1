import type {
  StrengthJobsResponse,
  StrengthStagedPendingSample,
  StrengthInadmissibleDiagnostic,
  DaemonHealthSnapshot,
  StrengthAuthorityBinding,
  StrengthJobsCapabilities,
} from "../api/types.js";

export type DaemonLivenessState =
  | "alive_fresh"
  | "alive_stale_heartbeat"
  | "configured_dead"
  | "configuration_conflict"
  | "configured_unverifiable"
  | "unconfigured"
  | "unknown";

export interface DaemonLivenessView {
  state: DaemonLivenessState;
  configured: boolean;
  alive: boolean;
  heartbeatFresh: boolean;
  activityState: string | null;
  detail: string;
}

export function daemonLivenessView(daemon: DaemonHealthSnapshot | null | undefined): DaemonLivenessView {
  if (!daemon) {
    return {
      state: "unknown",
      configured: false,
      alive: false,
      heartbeatFresh: false,
      activityState: null,
      detail: "daemon 健康投影不可用",
    };
  }
  const configured = daemon.configured === true;
  const alive = daemon.alive === true;
  const heartbeatFresh = daemon.heartbeat_status === "fresh";
  const activityState = typeof daemon.activity_state === "string" ? daemon.activity_state : null;
  let state: DaemonLivenessState;
  if (!configured && alive) {
    state = "configuration_conflict";
  } else if (!configured) {
    state = "unconfigured";
  } else if (!alive) {
    state = "configured_dead";
  } else if (!heartbeatFresh) {
    state = alive ? "alive_stale_heartbeat" : "configured_dead";
  } else {
    state = "alive_fresh";
  }
  // heartbeat_status may be missing on a healthy older snapshot; only treat
  // explicit non-fresh values as stale.
  if (configured && alive && heartbeatFresh) state = "alive_fresh";
  else if (configured && alive && daemon.heartbeat_status && daemon.heartbeat_status !== "fresh" && daemon.heartbeat_status !== "not_applicable") {
    state = "alive_stale_heartbeat";
  } else if (configured && alive && !daemon.heartbeat_status) {
    state = "configured_unverifiable";
  }

  const detail = (() => {
    const pid = typeof daemon.pid === "number" ? `PID ${daemon.pid}` : "PID 未知";
    const age = typeof daemon.heartbeat_age_sec === "number"
      ? `心跳 ${Math.round(daemon.heartbeat_age_sec)}s 前`
      : "无心跳";
    switch (state) {
      case "alive_fresh":
        return `配置已启用 · ${pid} · ${age}`;
      case "alive_stale_heartbeat":
        return `配置已启用 · ${pid} · 心跳过期（${daemon.heartbeat_status}）`;
      case "configured_dead":
        return `配置已启用 · 进程不在（${daemon.health_error || "alive=false"}）`;
      case "configuration_conflict":
        return `配置明确禁用，但检测到存活进程（${pid}）；按配置漂移错误处理`;
      case "configured_unverifiable":
        return `配置已启用 · ${pid} · 心跳状态缺失，不可验证`;
      case "unconfigured":
        return "配置未启用（不会调度强度对局）";
      default:
        return "daemon 健康投影不可用";
    }
  })();

  return { state, configured, alive, heartbeatFresh, activityState, detail };
}

/** Human label for the daemon's backend-owned activity state. */
export function daemonActivityLabel(value: string | null): string {
  const labels: Record<string, string> = {
    waiting_for_first_published_bot: "等待首个已发布 Bot",
    waiting_for_minimum_rating_pool: "等待至少两个已发布 Bot",
    waiting_for_second_published_bot: "等待第二个已发布 Bot",
    idle: "健康等待下一场评测",
    active: "正在调度原生 70 手对局",
    running: "正在运行原生 70 手对局",
    publishing_cycle: "正在发布不可变评分周期",
  };
  return value ? (labels[value] ?? `后端状态：${value}`) : "活动状态未提供";
}

export interface StrengthJobView {
  available: true;
  evaluationIdentityDigest: string;
  evaluationManifestDigest: string | null;
  epochResetReceiptDigest: string | null;
  authorityBinding: StrengthAuthorityBinding;
  capabilities: StrengthJobsCapabilities;
  activeBots: string[];
  daemon: DaemonLivenessView;
  admittedCount: number;
  stagedPendingTotal: number;
  inadmissibleTotal: number;
  observerComplete: boolean;
  observerIssues: string[];
  stagedPending: StrengthStagedPendingSample[];
  inadmissibleDiagnostics: StrengthInadmissibleDiagnostic[];
  /** Short label per rejection reason code, for the diagnostics panel. */
  inadmissibleReasonCounts: Array<{ reason: string; count: number }>;
  daemonStats: Record<string, unknown>;
}

export interface StrengthJobViewUnavailable {
  available: false;
  reason: string;
  evaluation_epoch: "national_tcp_policy_v1";
  active_bots: string[];
  epochResetReceiptDigest: string | null;
  authorityBinding: StrengthAuthorityBinding;
  capabilities: StrengthJobsCapabilities;
  daemon: DaemonLivenessView;
}

export function strengthJobView(
  response: StrengthJobsResponse,
): StrengthJobView | StrengthJobViewUnavailable {
  const daemon = daemonLivenessView(response.daemon);
  if (!response.available) {
    return {
      available: false,
      reason: response.reason,
      evaluation_epoch: response.evaluation_epoch,
      active_bots: response.active_bots,
      epochResetReceiptDigest: response.epoch_reset_receipt_digest,
      authorityBinding: response.authority_binding,
      capabilities: response.capabilities,
      daemon,
    };
  }
  const reasonCounts = new Map<string, number>();
  for (const diag of response.inadmissible_diagnostics) {
    for (const reason of diag.rejection_reasons) {
      reasonCounts.set(reason, (reasonCounts.get(reason) ?? 0) + 1);
    }
  }
  return {
    available: true,
    evaluationIdentityDigest: response.evaluation_identity_digest,
    evaluationManifestDigest: response.evaluation_manifest_digest,
    epochResetReceiptDigest: response.epoch_reset_receipt_digest,
    authorityBinding: response.authority_binding,
    capabilities: response.capabilities,
    activeBots: response.active_bots,
    daemon,
    admittedCount: response.pagination.admitted_total,
    stagedPendingTotal: response.pagination.staged_pending_total,
    inadmissibleTotal: response.pagination.inadmissible_total,
    observerComplete: response.observer.complete,
    observerIssues: response.observer.issues,
    stagedPending: response.staged_pending,
    inadmissibleDiagnostics: response.inadmissible_diagnostics,
    inadmissibleReasonCounts: [...reasonCounts.entries()]
      .map(([reason, count]) => ({ reason, count }))
      .sort((a, b) => b.count - a.count),
    daemonStats: response.daemon_stats,
  };
}

export interface ProducerConsumerCapabilityView {
  enabled: boolean;
  partial: boolean;
  label: string;
  detail: string;
}

/** Render only capabilities explicitly projected by the backend. */
export function producerConsumerCapabilityView(
  capabilities: StrengthJobsCapabilities,
): ProducerConsumerCapabilityView {
  const enabled = capabilities.durable_job_lifecycle
    && capabilities.queued_running_leases
    && capabilities.producer_consumer_dispatch;
  const partial = !enabled && (
    capabilities.durable_job_lifecycle
    || capabilities.queued_running_leases
    || capabilities.producer_consumer_dispatch
  );
  if (enabled) {
    return {
      enabled: true,
      partial: false,
      label: "生产者—消费者调度已启用",
      detail: "后端声明 durable lifecycle、queued/running leases 与 dispatcher 均可用；当前页仍只展示响应中经过结构校验的证据，不从 staged 文件猜 job 状态。",
    };
  }
  if (partial) {
    return {
      enabled: false,
      partial: true,
      label: "生产者—消费者能力不完整",
      detail: "后端只启用了部分 job lifecycle 能力；在三项能力全部成立前，页面不会声称队列正在运行。",
    };
  }
  return {
    enabled: false,
    partial: false,
    label: "生产者—消费者调度尚未启用",
    detail: "后端明确声明 durable lifecycle、queued/running leases 与 dispatcher 均不可用；当前只展示评分守护进程已有证据。",
  };
}

/** Human-readable Chinese label for one admission rejection reason code. */
export function strengthRejectionLabel(reason: string): string {
  const map: Record<string, string> = {
    execution_mode_not_native_tcp: "执行模式非 native TCP",
    evaluation_epoch_mismatch: "evaluation_epoch 不匹配",
    evaluation_identity_digest_mismatch: "evaluation_identity_digest 已变更（迟到/旧周期）",
    bot0_not_in_active_pool: "bot0 不在当前发布池",
    bot1_not_in_active_pool: "bot1 不在当前发布池",
    self_match: "自对（bot0 == bot1）",
    strength_sample_unit_not_70_hand_match: "样本单位非 70_hand_match",
    hands_per_strength_sample_not_70: "非 70 手（如 69 手不可采纳）",
    strength_not_admitted: "strength_admitted 非真",
    strength_not_complete: "strength_complete 非真",
    strength_compliance_not_passed: "strength_compliance_passed 非真",
    id_not_string: "id 非字符串",
    net_chips_not_list: "net_chips 非数组",
    strength_sample_count_mismatch: "strength_sample_count 与样本数不一致",
    empty_samples: "样本为空",
  };
  return map[reason] ?? reason;
}
