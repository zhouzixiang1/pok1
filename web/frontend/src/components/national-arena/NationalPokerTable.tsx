import type { ArenaSession } from "../../api/types";
import type { ArenaViewModel } from "../../lib/arenaViewModel";
import { actionLabel } from "../../lib/arenaViewModel";
import { cn } from "../../lib/utils";
import { DecisionClock } from "./DecisionClock";
import { PlayingCard } from "./PlayingCard";

function Seat({
  index,
  name,
  bot,
  chips,
  total,
  action,
  active,
}: {
  index: 0 | 1;
  name: string;
  bot: string;
  chips: number;
  total: number;
  action: string;
  active: boolean;
}) {
  return (
    <div className={cn(
      "w-[min(64vw,240px)] rounded-md border bg-gray-950/80 px-3 py-2 text-white shadow-lg backdrop-blur-sm",
      active ? "border-warning-400" : "border-white/15",
    )}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{name || bot || `玩家 ${index + 1}`}</div>
          <div className="truncate text-[10px] text-gray-400">{bot || (index === 0 ? "桌面上方" : "桌面下方")}</div>
        </div>
        <div className={cn("text-xs font-medium", total >= 0 ? "text-success-400" : "text-error-400")}>
          {total >= 0 ? "+" : ""}{total}
        </div>
      </div>
      <div className="mt-1 flex items-center justify-between text-xs">
        <span className="font-semibold tabular-nums text-warning-300">{chips.toLocaleString()}</span>
        <span className="max-w-24 truncate text-gray-300">{action ? actionLabel(action) : "等待"}</span>
      </div>
    </div>
  );
}

function HoleCards({ cards }: { cards: string[] }) {
  return (
    <div className="flex gap-1">
      {[0, 1].map((index) => <PlayingCard key={index} value={cards[index]} />)}
    </div>
  );
}

export function NationalPokerTable({ session, view }: {
  session: ArenaSession | null;
  view: ArenaViewModel;
}) {
  return (
    <div className="relative min-h-[520px] overflow-hidden rounded-lg border border-gray-200 bg-gray-100 p-3 dark:border-border-subtle dark:bg-surface-0 sm:aspect-[16/10] sm:min-h-0 sm:p-5">
      <div className="absolute inset-4 rounded-[50%] border-[10px] border-emerald-950/55 bg-emerald-700 shadow-inner sm:inset-7">
        <div className="absolute inset-2 rounded-[50%] border border-emerald-400/30" />
      </div>

      <div className="absolute left-1/2 top-3 -translate-x-1/2 sm:top-4">
        <Seat
          index={0}
          name={session?.top_player_name || ""}
          bot={session?.top_bot || ""}
          chips={view.chips[0]}
          total={session?.top_total_earnings || 0}
          action={view.latestActions[0]}
          active={view.actingPlayer === 0}
        />
      </div>
      <div className="absolute bottom-3 left-1/2 -translate-x-1/2 sm:bottom-4">
        <Seat
          index={1}
          name={session?.bottom_player_name || ""}
          bot={session?.bottom_bot || ""}
          chips={view.chips[1]}
          total={session?.bottom_total_earnings || 0}
          action={view.latestActions[1]}
          active={view.actingPlayer === 1}
        />
      </div>

      <div className="absolute left-1/2 top-[23%] -translate-x-1/2 sm:top-[24%]">
        <HoleCards cards={view.holeCards[0]} />
      </div>
      <div className="absolute bottom-[23%] left-1/2 -translate-x-1/2 sm:bottom-[24%]">
        <HoleCards cards={view.holeCards[1]} />
      </div>

      <div className="absolute left-1/2 top-1/2 flex -translate-x-1/2 -translate-y-1/2 gap-1 sm:gap-1.5">
        {[0, 1, 2, 3, 4].map((index) => (
          <PlayingCard key={index} value={view.board[index]} />
        ))}
      </div>
      <div className="absolute left-1/2 top-[59%] -translate-x-1/2 rounded-md bg-black/30 px-3 py-1 text-xs font-semibold text-white sm:top-[60%]">
        底池 <span className="tabular-nums text-warning-300">{view.pot.toLocaleString()}</span>
      </div>

      <div className="absolute left-1/2 top-[38%] -translate-x-1/2 rounded-md bg-black/25 px-2 py-1 text-[10px] font-semibold uppercase text-white/80 sm:left-10 sm:top-1/2 sm:translate-x-0 sm:-translate-y-1/2">
        {view.street}
      </div>
      <div className="absolute right-3 top-[64%] sm:right-9 sm:top-1/2 sm:-translate-y-1/2">
        <DecisionClock
          deadlineEpochMs={view.deadlineEpochMs}
          budgetSeconds={session?.action_timeout_seconds || 60}
        />
      </div>
      <div className="absolute bottom-2 left-3 text-[10px] font-medium text-gray-500 dark:text-gray-400">
        观赛视角
      </div>
    </div>
  );
}
