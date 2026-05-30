export interface BotRating {
  name: string;
  rank: number;
  rating: number;
  rd: number;
  sigma: number;
  conservative_rating: number;
  confidence: string;
  last_period: string;
  win_rate?: number;
  games?: number;
}

export interface MatchStats {
  total_games: number;
  total_pairs: number;
  total_periods: number;
  most_active_pair: string;
  most_active_count: number;
}

export interface MatchMatrix {
  bots: string[];
  matrix: (number | null)[][];
  source?: string;
}

export interface H2HEntry {
  games: number;
  a_wins: number;
  b_wins: number;
  draws: number;
  win_rate: number;
}

export interface BotStatsEntry {
  wins: number;
  losses: number;
  draws: number;
  games: number;
  win_rate: number;
}

export interface HistoryEntry {
  period: number;
  timestamp: string;
  ratings: Record<string, { r: number; rd: number }>;
}

export interface GenerationLog {
  version: string;
  files: string[];
}

export interface LogContent {
  version: string;
  filename: string;
  content: string;
}

export interface DaemonStatus {
  status: string;
  last_update_age_seconds: number;
  daemon_enabled: boolean;
}

export interface MatchSummary {
  id: string;
  timestamp: string;
  bot0: string;
  bot1: string;
  bot0_wins: number;
  bot1_wins: number;
  draws: number;
}

export interface DisplayFrame {
  round: number;
  round_idx: number;
  round_bet: number;
  round_raise: number;
  round_player_bet: [number, number];
  pot: number;
  player_chips: [number, number];
  public_cards: number[];
  player_cards: [[number, number], [number, number]];
  last_action?: { player_id: number; action: number; action_type: string };
  matchdata: {
    hand: number;
    max_hand: number;
    total_win_chips: [number, number];
    total_win_games: [number, number];
  };
  temp_result?: Array<{ win_chips: number; max_hand_type?: number; max_cards?: number[] }>;
  final_result?: Array<{ win_chips: number; win_games: number }>;
}

export interface GameReplay {
  game: number;
  winner: number;
  bot0_chips: number;
  bot1_chips: number;
  mirror?: boolean;
  logs: Array<Record<string, unknown>>;
}

export interface MatchReplayData extends MatchSummary {
  games: GameReplay[];
}

// Bot management
export interface BotSummary {
  name: string;
  version: number;
  completed: boolean;
  total_lines: number;
  files: string[];
  rating: { r: number; rd: number; conservative: number } | null;
  win_rate?: number;
  games?: number;
  graveyard?: boolean;
}

export interface BotDetail extends BotSummary {
  parent?: string;
}

// Pipeline
export interface PipelineCheckpoint {
  next_v: number;
  source_v: number;
  stage: string;
  master_plan: unknown;
  reviewer_feedback: string;
  generation_attempt: number;
  gate_results?: Record<string, unknown>;
  timestamp: string;
}

export interface WorkerFailure {
  gen: number;
  worker_id: number;
  role: string;
  error: string;
  timestamp?: string;
}

// Prompts
export interface PromptInfo {
  name: string;
  filename?: string;
  exists: boolean;
  lines: number;
  mtime: number | null;
  mtime_str?: string;
  role: string;
}

// Orchestrator session
export interface OrchestratorSession {
  session_id: string | null;
  active: boolean;
}

// Orchestrator log file
export interface OrchestratorLogFile {
  filename: string;
  size_bytes: number;
  mtime: number;
}
