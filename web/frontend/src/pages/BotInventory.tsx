import { useBots } from "../context/DataProvider";
import { useControlStatus } from "../hooks/useControlStatus";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader, EmptyState } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { evidenceTierForOfficialCertification } from "../domain/evidenceAuthority";
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
  const { status, loading, error } = useControlStatus(5_000);
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
      <PageMeta title="Bot 清单 — Bot 自进化" description="严格发布 Bot 身份清单" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />

      {!status?.epoch_initialized ? (
        <EmptyState message="epoch 未初始化；Bot 清单不可用。" />
      ) : rows.length === 0 ? (
        <EmptyState message="当前严格发布池为空；首个合格发布 Bot 出现后这里会列出。" />
      ) : (
        <>
          <Card className="mb-4">
            <CardHeader title="发布池身份投影" subtitle="strict_published_bot_identities · 不重编号、不合成 tag" />
            <div className="p-3 text-xs text-gray-500 dark:text-gray-400">
              已发布 {rows.length} 个 Bot；身份来自后端投影，不按目录最大编号推断。
            </div>
          </Card>

          <div className="space-y-3">
            {rows.map(({ bot, identity }) => {
              const inPool = activeSet.has(bot.name);
              const cert = bot.official_certification ?? null;
              const tier = evidenceTierForOfficialCertification(cert);
              const identityLabel = identity
                ? `第${identity.generation_ordinal}代 · ${identity.canonical_bot_name} · ${identity.canonical_tag}`
                : `${bot.name}（后端身份投影未配对，不合成 tag）`;
              return (
                <Card key={bot.name}>
                  <CardHeader title={identityLabel} subtitle={bot.name} />
                  <div className="p-4 space-y-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant={inPool ? "success" : "neutral"} size="sm">
                        {inPool ? "active pool" : "未在活跃池"}
                      </Badge>
                      {bot.completed && <Badge variant="success" size="sm">.completed</Badge>}
                      <Badge variant="info" size="sm">v{bot.version}</Badge>
                      <span className={cn("ml-auto rounded border px-1.5 py-0.5 text-xs", TONE_CLASS[tier.tone])}>
                        {tier.label}
                      </span>
                    </div>

                    <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-xs">
                      <InvField label="rating" value={bot.rating ? `${bot.rating.r} (rd ${bot.rating.rd})` : "—"} />
                      <InvField label="win_rate" value={bot.win_rate != null ? `${(bot.win_rate * 100).toFixed(0)}%` : "—"} />
                      <InvField label="selection_score" value={bot.selection_score != null ? bot.selection_score.toFixed(4) : "—"} />
                      <InvField label="strength_samples" value={String(bot.strength_sample_count ?? 0)} />
                      <InvField label="h2h_games" value={String(bot.h2h_games ?? 0)} />
                      <InvField label="h2h_coverage" value={bot.h2h_coverage != null ? `${(bot.h2h_coverage * 100).toFixed(0)}%` : "—"} />
                      <InvField label="primary_70_hand" value={bot.primary_70_hand_match_score != null ? bot.primary_70_hand_match_score.toFixed(4) : "—"} />
                      <InvField label="total_lines" value={String(bot.total_lines)} />
                    </div>

                    {cert && (
                      <div className="text-xs text-gray-500 dark:text-gray-400 border-t border-gray-100 dark:border-gray-800 pt-2">
                        <span className="font-mono">
                          {cert.formal_authority ?? "none"}
                          {cert.certificate_digest ? ` · cert ${cert.certificate_digest.slice(0, 12)}…` : ""}
                        </span>
                        {cert.issues && cert.issues.length > 0 && (
                          <span className="text-error-500 ml-2">issues：{cert.issues.join(", ")}</span>
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
