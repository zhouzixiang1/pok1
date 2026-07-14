import type { ControlStatus, EpochState } from "../../api/control";
import { authorityNextVersion } from "../../hooks/useControlStatus";
import { cn } from "../../lib/utils";

const stateLabels: Record<EpochState, string> = {
  reset_required: "需要执行一次性 epoch 重置",
  reset_evidence_requires_recovery: "重置证据需要人工恢复",
  version_authority_requires_recovery: "版本权威需要人工恢复",
  epoch_authority_unavailable: "epoch 权威不可用",
  fresh_bootstrap_ready: "首个严格版本已就绪",
  strict_published: "严格国赛 epoch 已发布",
};

const stateTone: Record<EpochState, string> = {
  reset_required: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/25",
  reset_evidence_requires_recovery: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/25",
  version_authority_requires_recovery: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/25",
  epoch_authority_unavailable: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/25",
  fresh_bootstrap_ready: "border-blue-300 bg-blue-50 dark:border-blue-800 dark:bg-blue-950/25",
  strict_published: "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/25",
};

interface Props {
  status: ControlStatus | null;
  loading?: boolean;
  error?: string | null;
  compact?: boolean;
  className?: string;
}

export function EpochAuthorityStatus({ status, loading = false, error, compact = false, className }: Props) {
  if (!status) {
    return (
      <div className={cn("rounded-xl border border-gray-200 bg-white p-4 text-sm dark:border-border-subtle dark:bg-surface-1", className)}>
        {loading ? (
          <span className="text-gray-500">正在读取严格国赛 epoch 权威状态…</span>
        ) : (
          <div>
            <p className="font-semibold text-red-600 dark:text-red-300">无法确认版本与运行权威</p>
            <p className="mt-1 text-xs text-gray-500">{error || "控制状态不可用；在恢复读取前不要启动进化。"}</p>
          </div>
        )}
      </div>
    );
  }

  const nextVersion = authorityNextVersion(status);
  const debris = status.unpublished_candidate_versions;
  const resetBlocked = !status.epoch_initialized;

  return (
    <section className={cn("rounded-xl border p-4", stateTone[status.epoch_state], className)} aria-label="严格国赛 epoch 权威">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-white">{stateLabels[status.epoch_state]}</h2>
            <span className="rounded bg-white/70 px-2 py-0.5 font-mono text-[10px] text-gray-600 dark:bg-black/20 dark:text-gray-300">
              {status.evaluation_epoch}
            </span>
          </div>
          <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
            数字权威高水位 <span className="font-mono font-semibold">v{status.version_authority_high_water}</span>
            {resetBlocked && "（仅归档数字边界，不是严格活跃父代）"}
            {nextVersion != null && (
              <> · 下一权威目标 <span className="font-mono font-semibold">v{nextVersion}</span></>
            )}
            <> · 已发布严格代次 <span className="font-mono font-semibold">{status.strict_generation_count}</span></>
          </p>
        </div>
        <span className={cn(
          "rounded-full px-2.5 py-1 text-[11px] font-medium",
          status.epoch_initialized
            ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
            : "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
        )}>
          {status.epoch_initialized ? "权威已初始化" : "禁止启动进化"}
        </span>
      </div>

      {!compact && (
        <div className="mt-3 space-y-2 text-xs text-gray-600 dark:text-gray-300">
          {status.epoch_state === "reset_required" && (
            <p>
              旧 checkpoint、abandoned floor、历史评分与未发布目录均不参与版本号、活跃池或强度证据。
              一次性重置完成前，首个严格目标固定由数字高水位递增。
            </p>
          )}
          {status.epoch_state === "reset_evidence_requires_recovery" && (
            <p className="text-red-700 dark:text-red-300">
              检测到不可验证或中断的重置证据。系统必须保持停止，由操作员检查证据；不能再次覆盖写入 receipt。
            </p>
          )}
          {status.epoch_state === "version_authority_requires_recovery" && (
            <p className="text-red-700 dark:text-red-300">
              数字高水位包含严格版本号，但没有可验证的五文件发布身份与 signed full-v5 证书。不能重跑 reset 或启动进化，必须人工审计异常 tag。
            </p>
          )}
          {status.epoch_state === "epoch_authority_unavailable" && (
            <p className="text-red-700 dark:text-red-300">
              后端无法验证 epoch 权威。所有启动和变更必须保持关闭，直到权威读取恢复。
            </p>
          )}
          {status.epoch_state === "fresh_bootstrap_ready" && (
            <p>一次性重置 receipt 已验证；等待 v{nextVersion} 完成原生 TCP、本地门和正式 EXE 全量认证后发布。</p>
          )}
          {status.epoch_state === "strict_published" && (
            <p>
              活跃池仅包含带严格发布身份的 Bot：{status.active_bots.length > 0 ? status.active_bots.join("、") : "当前为空"}。
            </p>
          )}

          {debris.length > 0 && (
            <p className="rounded border border-amber-200 bg-white/60 px-2.5 py-2 text-amber-800 dark:border-amber-800 dark:bg-black/10 dark:text-amber-300">
              未发布残骸：{debris.map((version) => `v${version}`).join("、")}。这些目录没有发布 tag/证书/池权限，不占版本号，也不能恢复成活跃代次。
            </p>
          )}

          {status.ignored_checkpoint && (
            <p className="rounded border border-red-200 bg-white/60 px-2.5 py-2 text-red-700 dark:border-red-800 dark:bg-black/10 dark:text-red-300">
              已忽略旧 checkpoint
              {status.ignored_checkpoint.next_v != null ? ` v${status.ignored_checkpoint.next_v}` : ""}：
              {status.ignored_checkpoint.issues.join("、") || status.ignored_checkpoint.reason}。
            </p>
          )}

          {status.reset_receipt_issues.length > 0 && status.epoch_state !== "reset_required" && (
            <p className="font-mono text-[11px] text-red-600 dark:text-red-300">{status.reset_receipt_issues.join(" · ")}</p>
          )}

          {status.operator_command && (
            <div>
              <p className="font-medium text-amber-800 dark:text-amber-300">操作员专用命令（只读展示，网页不执行）：</p>
              <code className="mt-1 block overflow-x-auto rounded bg-gray-950 px-3 py-2 text-[11px] text-gray-100">{status.operator_command}</code>
            </div>
          )}

          {status.active_generation && (
            <p>
              权威活动代次：<span className="font-mono">v{status.active_generation.next_v}</span>
              {status.active_generation.source_v != null && <> ← v{status.active_generation.source_v}</>}
              <> · {status.active_generation.stage}</>
              <> · workflow <span className="font-mono">{status.active_generation.workflow_run_id || "—"}</span></>
            </p>
          )}
        </div>
      )}
    </section>
  );
}
