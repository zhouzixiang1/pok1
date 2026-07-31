import type { ControlStatus, EpochState } from "../../api/control";
import { draftGenerations } from "../../api/control";
import { authorityNextVersion } from "../../hooks/useControlStatus";
import { cn } from "../../lib/utils";
import { canonicalGenerationLabel } from "../../lib/canonicalGenerationIdentity";
import { RefreshStatusBadge } from "./ui";

export const epochStateLabels: Record<EpochState, string> = {
  reset_required: "严格进化需要一次性初始化",
  reset_evidence_requires_recovery: "初始化证据需要人工恢复",
  version_authority_requires_recovery: "真实版本身份需要人工恢复",
  epoch_authority_unavailable: "无法验证当前严格进化身份",
  runtime_reconciliation_in_progress: "正在核对停机前后的运行状态",
  publication_recovery_ready: "一次未完成的发布可以原位续做",
  fresh_bootstrap_ready: "首个严格 Bot 的生产环境已就绪",
  strict_published: "严格发布池已建立",
};

const stateTone: Record<EpochState, string> = {
  reset_required: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/25",
  reset_evidence_requires_recovery: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/25",
  version_authority_requires_recovery: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/25",
  epoch_authority_unavailable: "border-red-300 bg-red-50 dark:border-red-800 dark:bg-red-950/25",
  runtime_reconciliation_in_progress: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/25",
  publication_recovery_ready: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/25",
  fresh_bootstrap_ready: "border-blue-300 bg-blue-50 dark:border-blue-800 dark:bg-blue-950/25",
  strict_published: "border-emerald-300 bg-emerald-50 dark:border-emerald-800 dark:bg-emerald-950/25",
};

interface Props {
  status: ControlStatus | null;
  loading?: boolean;
  error?: string | null;
  /** Epoch-ms of the last successful observation, for the refresh badge. */
  lastUpdated?: number | null;
  compact?: boolean;
  className?: string;
}

export function EpochAuthorityStatus({ status, loading = false, error, lastUpdated = null, compact = false, className }: Props) {
  if (!status) {
    return (
      <div className={cn("rounded-xl border border-gray-200 bg-white p-4 text-sm dark:border-border-subtle dark:bg-surface-1", className)}>
        {loading ? (
          // First-load / refreshing state: the backend projection is being
          // built (a retryable 503, not an authority failure).  Show a neutral
          // "refreshing" state rather than the red fail-closed banner.  Only a
          // genuine non-retryable authority error flips loading off and reaches
          // the red branch below.
          <div>
            <span className="text-gray-500">
              {error ? "正在刷新运行权威（后端投影构建中）…" : "正在核对严格进化与版本身份…"}
            </span>
            {error && (
              <p className="mt-1 text-xs text-gray-400 dark:text-gray-500">{error}</p>
            )}
            <RefreshStatusBadge lastUpdated={lastUpdated} className="mt-1 block text-xs text-gray-400 dark:text-gray-500" />
          </div>
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
  const activeIdentityLabel = status.active_generation
    ? canonicalGenerationLabel(status.active_generation, status.active_generation.next_v)
    : null;
  const drafts = draftGenerations(status);

  return (
    <section className={cn("rounded-xl border p-4", stateTone[status.epoch_state], className)} aria-label="严格进化身份状态">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-sm font-semibold text-gray-900 dark:text-white">{epochStateLabels[status.epoch_state]}</h2>
            <span className="rounded bg-white/70 px-2 py-0.5 font-mono text-[10px] text-gray-600 dark:bg-black/20 dark:text-gray-300">
              {status.evaluation_epoch}
            </span>
          </div>
          <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
            历史版本编号上限 <span className="font-mono font-semibold">v{status.version_authority_high_water}</span>
            {resetBlocked && "（只用于防止版本号倒退，不是可继承父本）"}
            {nextVersion != null && (
              <> · 下一真实版本 <span className="font-mono font-semibold">v{nextVersion}</span></>
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
          {status.epoch_initialized ? "身份已验证" : "禁止启动进化"}
        </span>
      </div>

      {!compact && (
        <div className="mt-3 space-y-2 text-xs text-gray-600 dark:text-gray-300">
          {status.epoch_state === "reset_required" && (
            <p>
              旧 checkpoint、abandoned floor、历史评分与未绑定目录不构成活跃池或强度证据。
              版本号只由后端数字高水位和已验证 checkpoint/提交身份裁决；页面不会仅凭“未发布”断言编号仍可复用。
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
          {status.epoch_state === "runtime_reconciliation_in_progress" && (
            <div className="space-y-1 text-amber-800 dark:text-amber-300">
              <p>
                已存在持久化 reconciliation claim；它是进化、Daemon 与正式运行的启动屏障。
                必须通过后端按 claim 种类给出的操作员命令恢复，不能手工删除 claim 后继续。
              </p>
              <p>
                claim 类型：<span className="font-mono">{status.runtime_reconciliation_kind || "无法验证"}</span>
                {status.runtime_reconciliation_claim_digest && (
                  <> · 摘要 <span className="font-mono">{status.runtime_reconciliation_claim_digest.slice(0, 12)}…</span></>
                )}
              </p>
              {!status.runtime_reconciliation_claim_valid && (
                <p className="text-red-700 dark:text-red-300">
                  claim 无法验证；后端仅允许人工检查，不会猜测或展示执行命令。
                  {status.runtime_reconciliation_claim_issues.length > 0
                    ? ` ${status.runtime_reconciliation_claim_issues.join("、")}`
                    : ""}
                </p>
              )}
            </div>
          )}
          {status.epoch_state === "publication_recovery_ready" && (
            <p className="text-amber-800 dark:text-amber-300">
              后端已证明仅缺一个 create-only 发布标签，且它绑定当前 publishing checkpoint、候选树与正式证书。
              只允许原事务幂等补齐；不能新分配同一版本或启动另一代。
            </p>
          )}
          {status.epoch_state === "fresh_bootstrap_ready" && (
            <p>一次性初始化收据已验证；v{nextVersion} 只有完成原生 TCP、本地门和正式 EXE 全量认证后才会进入发布池。</p>
          )}
          {status.epoch_state === "strict_published" && (
            <p>
              当前可参与评分的发布 Bot：{status.active_bots.length > 0 ? status.active_bots.join("、") : "当前为空"}。
            </p>
          )}

          {debris.length > 0 && (
            <p className="rounded border border-amber-200 bg-white/60 px-2.5 py-2 text-amber-800 dark:border-amber-800 dark:bg-black/10 dark:text-amber-300">
              未发布候选/残骸：{debris.map((version) => `v${version}`).join("、")}。它们没有发布 tag、证书或池权限；
              其中是否存在已提交但未发布、因而已消耗编号的对象，必须服从后端版本权威，不能由此列表推断。
            </p>
          )}

          {status.ignored_checkpoint && (
            <p className="rounded border border-red-200 bg-white/60 px-2.5 py-2 text-red-700 dark:border-red-800 dark:bg-black/10 dark:text-red-300">
              当前 checkpoint/分配投影已隔离
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
              正在处理：{activeIdentityLabel ? (
                <span className="font-mono">{activeIdentityLabel}</span>
              ) : (
                <span className="font-semibold text-red-700 dark:text-red-300">双身份投影不可用</span>
              )}
              {status.active_generation.source_v != null && <> · 主父本 <span className="font-mono">v{status.active_generation.source_v}</span></>}
              <> · 内部阶段 <span className="font-mono">{status.active_generation.stage}</span></>
              <> · 工作流 <span className="font-mono">{status.active_generation.workflow_run_id || "—"}</span></>
              {status.active_generation.recovery_kind === "recorded_abandon_checkpoint_finalize" && (
                <> · 已记录 abandon receipt，等待完成 checkpoint/candidate 清理</>
              )}
              {status.active_generation.recovery_kind === "publication_reconciliation" && (
                <> · 正在恢复同一发布事务</>
              )}
              {status.active_generation.source_v != null
                && status.active_generation.source_v < status.active_generation.next_v
                && (
                <> · v{status.active_generation.source_v} 仅为数字高水位，不表示继承源 artifact</>
              )}
            </p>
          )}

          {drafts.length > 0 && (
            <p>
              并行草稿槽：
              {drafts.map((draft, index) => (
                <span key={`${draft.slot_id}-${draft.next_v}-${draft.workflow_run_id ?? index}`}>
                  {index > 0 ? "；" : ""}
                  <span className="font-mono">v{draft.next_v}</span>
                  <> · 阶段 <span className="font-mono">{draft.stage}</span></>
                  {draft.checkpoint_revision != null && (
                    <> · rev <span className="font-mono">{draft.checkpoint_revision}</span></>
                  )}
                </span>
              ))}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
