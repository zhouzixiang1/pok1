#!/usr/bin/env bash
# Bounded backup of the evolution runtime's authority state (2026-10-18).
#
# Read-only snapshot of .evolution_pok runtime results into
# /home/ubuntu/backups/pok-evolution — survives file corruption, accidental
# deletion, and bad deploys. Restores are OPERATOR-driven and NEVER
# automatic: authority files are CAS/digest-protected and a stale restore
# is worse than a rebuild (see AGENTS.md trust boundary).
#
# Modes:
#   daily  — small critical ledgers/manifests/handoffs/ratings (~8M gz)
#   weekly — daily set + evaluation_cycles/ workflow/ candidates.jsonl (~100M gz)
#
# Retention: newest 14 daily + 4 weekly, then oldest-first pruning until the
# backup root is under SIZE_CAP_BYTES.

set -euo pipefail

RESULTS="/home/ubuntu/pok1/.evolution_pok/web/core/results"
BACKUP_ROOT="/home/ubuntu/backups/pok-evolution"
STAMP="$(date +%Y%m%d-%H%M%S)"
MODE="${1:-daily}"
SIZE_CAP_BYTES=$((1536 * 1024 * 1024))  # 1.5 GiB total cap

DAILY_FILES=(
  abandoned_versions.jsonl
  reaped_bots.jsonl
  reaped_generation_trees.jsonl
  rating_history.jsonl
  glicko_ratings.json
  bot_stats.json
  bot_action_stats.json
  bot_action_stats_per_opp.json
  evaluation_cycle_manifest.json
  elo_daemon_stats.json
  match_history.jsonl
  llm_costs.jsonl
  generation_cost_ledger.jsonl
  eval_rounds.jsonl
  stability_observation.json
  rate_limit_state.json
  post_publication_handoffs
)

case "$MODE" in
  daily)  LABEL="daily" ;;
  weekly) LABEL="weekly" ;;
  *) echo "usage: $0 [daily|weekly]" >&2; exit 2 ;;
esac

mkdir -p "$BACKUP_ROOT"
ARCHIVE="$BACKUP_ROOT/${LABEL}-${STAMP}.tar.gz"

cd "$RESULTS"
# --ignore-failed-read: an absent optional file must not abort the snapshot.
if [ "$MODE" = "weekly" ]; then
  tar --ignore-failed-read -czf "$ARCHIVE.tmp" \
    "${DAILY_FILES[@]}" evaluation_cycles workflow candidates.jsonl 2>/dev/null
else
  tar --ignore-failed-read -czf "$ARCHIVE.tmp" "${DAILY_FILES[@]}" 2>/dev/null
fi
mv "$ARCHIVE.tmp" "$ARCHIVE"

# Retention by count, then by total size cap (oldest first).
for prefix in daily weekly; do
  case "$prefix" in
    daily)  KEEP=14 ;;
    weekly) KEEP=4 ;;
  esac
  # shellcheck disable=SC2046 — empty set must be a no-op, not a failure.
  ls -1t "$BACKUP_ROOT/${prefix}-"*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) \
    | xargs -r rm -f -- || true
done
while [ "$(du -sb "$BACKUP_ROOT" | cut -f1)" -gt "$SIZE_CAP_BYTES" ]; do
  OLDEST="$(ls -1t "$BACKUP_ROOT" | tail -n +2 | head -1 || true)"
  [ -z "${OLDEST:-}" ] && break
  rm -f "$BACKUP_ROOT/$OLDEST"
done

echo "backup ${LABEL} ${STAMP}: $(du -h "$ARCHIVE" | cut -f1) -> $ARCHIVE"
