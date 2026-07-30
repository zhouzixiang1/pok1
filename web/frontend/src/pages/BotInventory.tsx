import { useBots } from "../context/DataProvider";
import { useControlStatus } from "../hooks/useControlStatus";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader, EmptyState } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { OperatorSituation } from "../components/evolution/OperatorSituation";
import { certificationView } from "../domain/certificationView";
import { cn } from "../lib/utils";
import type { EvidenceAuthorityLabel } from "../domain/evidenceAuthority";

const TONE_CLASS: Record<EvidenceAuthorityLabel["tone"], string> = {
  success: "text-success-600 dark:text-success-400 border-success-300 dark:border-success-800",
  info: "text-brand-600 dark:text-brand-400 border-brand-300 dark:border-brand-800",
  warning: "text-warning-600 dark:text-warning-400 border-warning-300 dark:border-warning-800",
  neutral: "text-gray-600 dark:text-gray-300 border-gray-300 dark:border-gray-700",
  error: "text-error-600 dark:text-error-400 border-error-300 dark:border-error-800",
};

/**
 * Bot Inventory — a generation-ordinal-sorted view of published strict bots,
 * each rendered with its canonical identity triple (ordinal · bot name · tag).
 *
 * The dashboard never re-indexes bots or synthesises a tag from a version
 * number.  Every identity field comes from the backend's
 * ``strict_published_bot_identities`` projection; the highest-numbered
 * directory is not treated as completion proof.
 */
export default function BotInventory() {
  const { active: bots } = useBots();
  const { status, health, loading, error } = useControlStatus(5_000);
  const identities = status?.strict_published_bot_identities ?? [];
  const activeSet = new Set(status?.active_bots ?? []);

  // Pair each published bot with its backend-owned identity by canonical tag.
  // If the backend identity list is unavailable we render the bots without a
  // synthesised ordinal/tag (fail-closed), never guessing from version order.
  const rows = bots.map((bot) => {
    const identity = identities.find((id) => id.canonical_version === bot.version) ?? null;
    return { bot, identity };
  });

  return (
    <>
      <PageMeta title="发布概览 — Bot 自进化" description="哪些 Bot 已真正发布并进入活跃池" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />
      <OperatorSituation status={status} health={health} className="mb-4" />

      {!status?.epoch_initialized ? (
        <EmptyState message="严格进化尚未初始化；当前没有可验证的发布清单。" />
      ) : rows.length === 0 ? (
        <EmptyState message="还没有 Bot 完成签名证书、commit、.completed 和 annotated tag，因此发布池仍为空。正在生产的候选不会提前出现在这里。" />
      ) : (
        <>
          <Card className="mb-4">
            <CardHeader title="已发布 Bot 概览" subtitle="这里只列真正完成发布链的 Bot；源码和证书详情在“严格发布 Bot”页" />
            <div className="p-3 text-xs text-gray-500 dark:text-gray-400">
              已发布 {rows.length} 个 Bot。网页代次由后端分配，真实 tag 不可改写；不会按目录编号猜测或重排。
            </div>
          </Card>

          <div className="space-y-3">
            {rows.map(({ bot, identity }) => {
              const inPool = activeSet.has(bot.name);
              const cert = bot.official_certification ?? null;
              const certView = certificationView(cert, {
                publication_tier: bot.publication_tier ?? cert?.publication_tier ?? null,
                certified_tag: bot.certified_tag ?? null,
              });
              const tier = certView.evidence;
              const identityLabel = identity
                ? `第${identity.generation_ordinal}代 · ${identity.canonical_bot_name} · ${identity.canonical_tag}`
                : `${bot.name}（发布身份未能交叉验证）`;
              return (
                <Card key={bot.name}>
                  <CardHeader title={identityLabel} subtitle={bot.name} />
                  <div className="p-4 space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={inPool ? "success" : "neutral"} size="sm">
                        {inPool ? "当前可参与评分" : "已发布但不在当前评分池"}
                      </Badge>
                      {bot.completed && <Badge variant="success" size="sm">.completed</Badge>}
                      <Badge variant="info" size="sm">v{bot.version}</Badge>
                      {certView.publicationTier && (
                        <Badge variant={certView.publicationTier === "certified" ? "success" : "warning"} size="sm">
                          {certView.publicationTier}
                        </Badge>
                      )}
                      <span className={cn("rounded border px-1.5 py-0.5 text-xs", TONE_CLASS[tier.tone])} title={certView.detail}>
                        {certView.label}
                      </span>
                      {certView.certifiedTag && (
                        <span className="rounded border border-emerald-200 bg-emerald-50 px-1.5 py-0.5 font-mono text-[10px] text-emerald-700 dark:border-emerald-800 dark:bg-emerald-900/20 dark:text-emerald-300">
                          {certView.certifiedTag}
                        </span>
                      )}
                    </div>

                    <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs">
                      <InvField label="评分 / 不确定度" value={bot.rating ? `${bot.rating.r} / ${bot.rating.rd}` : "—"} />
                      <InvField label="完整样本胜率" value={bot.win_rate != null ? `${(bot.win_rate * 100).toFixed(0)}%` : "—"} />
                      <InvField label="选代分" value={bot.selection_score != null ? bot.selection_score.toFixed(4) : "—"} />
                      <InvField label="完整 70 手样本" value={String(bot.strength_sample_count ?? 0)} />
                      <InvField label="H2H 样本" value={String(bot.h2h_games ?? 0)} />
                      <InvField label="H2H 覆盖" value={bot.h2h_coverage != null ? `${(bot.h2h_coverage * 100).toFixed(0)}%` : "—"} />
                      <InvField label="主要 70 手得分" value={bot.primary_70_hand_match_score != null ? bot.primary_70_hand_match_score.toFixed(4) : "—"} />
                      <InvField label="代码总行数" value={String(bot.total_lines)} />
                    </div>

                    {cert && (
                      <div className="text-xs text-gray-500 dark:text-gray-400 border-t border-gray-100 dark:border-gray-800 pt-2">
                        <span className="font-mono">
                          官方兼容权威：{cert.formal_authority ?? "none"}
                          {cert.certificate_digest ? ` · cert ${cert.certificate_digest.slice(0, 12)}…` : ""}
                        </span>
                        {cert.issues && cert.issues.length > 0 && (
                          <span className="text-error-500 ml-2">认证问题：{cert.issues.join(", ")}</span>
                        )}
                      </div>
                    )}
                  </div>
                </Card>
              );
            })}
          </div>
        </>
      )}
    </>
  );
}

function InvField({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between border-b border-gray-50 dark:border-gray-900 py-0.5">
      <span className="text-gray-500 dark:text-gray-400">{label}</span>
      <span className="font-mono text-gray-800 dark:text-gray-200">{value}</span>
    </div>
  );
}
