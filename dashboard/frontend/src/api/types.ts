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
