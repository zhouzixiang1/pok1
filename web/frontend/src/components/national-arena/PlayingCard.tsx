import { cn } from "../../lib/utils";

const SUITS = ["♠", "♥", "♦", "♣"] as const;
const RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"] as const;

export function PlayingCard({ value, hidden = false }: { value?: string; hidden?: boolean }) {
  const match = value?.match(/^<(\d),(\d{1,2})>$/);
  const suitIndex = match ? Number(match[1]) : -1;
  const rankIndex = match ? Number(match[2]) : -1;
  const valid = suitIndex >= 0 && suitIndex < SUITS.length && rankIndex >= 0 && rankIndex < RANKS.length;
  const suit = valid ? SUITS[suitIndex] : "";
  const rank = valid ? RANKS[rankIndex] : "";
  const red = suit === "♥" || suit === "♦";

  return (
    <div
      aria-label={hidden ? "隐藏牌" : valid ? `${suit}${rank}` : "空牌位"}
      className={cn(
        "relative aspect-[5/7] w-10 shrink-0 overflow-hidden rounded-md border shadow-sm sm:w-12",
        hidden
          ? "border-emerald-200/50 bg-brand-600"
          : valid
            ? "border-gray-200 bg-white"
            : "border-dashed border-white/25 bg-white/5",
      )}
    >
      {hidden ? (
        <div className="absolute inset-1 rounded border border-white/30 bg-brand-500" />
      ) : valid ? (
        <>
          <span className={cn("absolute left-1 top-0.5 text-xs font-bold leading-none", red ? "text-error-600" : "text-gray-900")}>
            {rank}
          </span>
          <span className={cn("flex h-full items-center justify-center text-xl", red ? "text-error-600" : "text-gray-900")}>
            {suit}
          </span>
        </>
      ) : null}
    </div>
  );
}
