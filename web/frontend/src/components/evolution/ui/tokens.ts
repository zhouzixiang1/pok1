/**
 * Evolution-local visual contract on top of TailAdmin tokens.
 * Spacing / radius / semantic status colors — no separate brand skin.
 */

export const EVOLUTION_RADIUS = {
  surface: "rounded-2xl",
  badge: "rounded-md",
  trackNode: "rounded-full",
} as const;

export const EVOLUTION_GAP = {
  page: "gap-4",
  section: "gap-4",
  cards: "gap-4 lg:gap-6",
} as const;

export type EvolutionStatusTone =
  | "ok"
  | "warn"
  | "error"
  | "info"
  | "neutral"
  | "park";

/** Shared semantic map for OperatorSituation / handoff / eval_wait / badges. */
export const STATUS_TONE_CLASSES: Record<
  EvolutionStatusTone,
  { badge: string; border: string; text: string; dot: string }
> = {
  ok: {
    badge: "bg-success-50 text-success-700 dark:bg-success-900/30 dark:text-success-400",
    border: "border-success-300 dark:border-success-800",
    text: "text-success-700 dark:text-success-400",
    dot: "bg-success-500",
  },
  warn: {
    badge: "bg-warning-50 text-warning-700 dark:bg-warning-900/30 dark:text-warning-400",
    border: "border-warning-300 dark:border-warning-800",
    text: "text-warning-700 dark:text-warning-400",
    dot: "bg-warning-500",
  },
  error: {
    badge: "bg-error-50 text-error-700 dark:bg-error-900/30 dark:text-error-400",
    border: "border-error-300 dark:border-error-800",
    text: "text-error-700 dark:text-error-400",
    dot: "bg-error-500",
  },
  info: {
    badge: "bg-blue-light-50 text-blue-light-700 dark:bg-blue-light-900/30 dark:text-blue-light-400",
    border: "border-blue-light-300 dark:border-blue-light-800",
    text: "text-blue-light-700 dark:text-blue-light-400",
    dot: "bg-blue-light-500",
  },
  neutral: {
    badge: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400",
    border: "border-gray-300 dark:border-gray-700",
    text: "text-gray-600 dark:text-gray-400",
    dot: "bg-gray-400",
  },
  park: {
    // Parked / not-stuck must read as a calm "waiting" state, distinct from the
    // warning tone (warning-*/amber-* resolve to the same hue).  Violet keeps
    // "停泊 / 不是卡住" visually separate from a real warning.
    badge: "bg-violet-50 text-violet-800 dark:bg-violet-950/40 dark:text-violet-300",
    border: "border-violet-300 dark:border-violet-800",
    text: "text-violet-800 dark:text-violet-300",
    dot: "bg-violet-500",
  },
};

export const STREAM_SHELL = {
  outer: "rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1",
  titleBar: "border-b border-gray-200 px-4 py-3 dark:border-border-subtle",
  body: "rounded-b-2xl border-t border-gray-800 bg-gray-950 font-mono text-xs text-gray-100",
} as const;
