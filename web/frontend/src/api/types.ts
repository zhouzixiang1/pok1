export interface BotRating {
  name: string;
  rank: number;
  rating: number;
  rd: number;
  sigma: number;
  conservative_rating: number;
  confidence: string;
  last_period: string;
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
  matrix: number[][];
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
}

export interface EvolutionState {
  status: string;
  is_working: boolean;
  header: string;
  metrics: Record<string, number>;
  ratings: Array<{
    rank: number;
    name: string;
    rating: number;
    rd: number;
    conservative: number;
  }>;
  active_bots: string[];
  grand_cost_total: number;
  gen_cost_total: number;
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
