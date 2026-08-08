import type { EpochState } from "../api/control.js";

// Pure label map for the backend-defined epoch states (see
// web/core/epoch_authority.py). Kept in lib/ (not inside a component) so any
// consumer — EvolutionPageHeader, a status strip, etc. — can import it without
// pulling in a retired component. Operator copy is pure Chinese.
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
