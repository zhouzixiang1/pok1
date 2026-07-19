import type {
  StrengthJobsResponse,
  StrengthStagedPendingSample,
  StrengthInadmissibleDiagnostic,
  DaemonHealthSnapshot,
} from "../api/types.js";

export type DaemonLivenessState =
  | "alive_fresh"
  | "alive_stale_heartbeat"
  | "configured_dead"
  | "configured_unverifiable"
  | "unconfigured"
  | "unknown";

export interface DaemonLivenessView {
  state: DaemonLivenessState;
  configured: boolean;
  alive: boolean;
  heartbeatFresh: boolean;
  detail: string;
}

export function daemonLivenessView(daemon: DaemonHealthSnapshot | null | undefined): DaemonLivenessView {
  if (!daemon) {
    return {
      state: "unknown",
      configured: false,
      alive: false,
      heartbeatFresh: false,
      detail: "daemon 健康投影不可用",
    };
  }
  const configured = daemon.configured === true;
  const alive = daemon.alive === true;
  const heartbeatFresh = daemon.heartbeat_status === "fresh";
  let state: DaemonLivenessState;
  if (!configured) {
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
  if (alive && heartbeatFresh) state = "alive_fresh";
  else if (alive && daemon.heartbeat_status && daemon.heartbeat_status !== "fresh" && daemon.heartbeat_status !== "not_applicable") {
    state = "alive_stale_heartbeat";
  } else if (alive && !daemon.heartbeat_status) {
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
      case "configured_unverifiable":
        return `配置已启用 · ${pid} · 心跳状态缺失，不可验证`;
      case "unconfigured":
        return "配置未启用（不会调度强度对局）";
      default:
        return "daemon 健康投影不可用";
    }
  })();

  return { state, configured, alive, heartbeatFresh, detail };
}

export interface StrengthJobView {
  available: true;
  evaluationIdentityDigest: string;
  evaluationManifestDigest: string | null;
  epochResetReceiptDigest: string | null;
  activeBots: string[];
  daemon: DaemonLivenessView;
  admittedCount: number;
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
    activeBots: response.active_bots,
    daemon,
    admittedCount: response.admitted_samples.length,
    stagedPending: response.staged_pending,
    inadmissibleDiagnostics: response.inadmissible_diagnostics,
    inadmissibleReasonCounts: [...reasonCounts.entries()]
      .map(([reason, count]) => ({ reason, count }))
      .sort((a, b) => b.count - a.count),
    daemonStats: response.daemon_stats,
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
