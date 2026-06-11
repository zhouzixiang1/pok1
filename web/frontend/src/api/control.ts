export interface ControlStatus {
  mode: string;
  running: boolean;
  daemon_enabled: boolean;
  daemon_workers: number;
  daemon_pairs: number;
  current_v: number;
  next_v: number;
  generation_count: number;
  decisions: Decision[];
}

export interface Decision {
  tool: string;
  summary: string;
  ts: number;
}

export interface ToolResult {
  tool: string;
  result?: string;
  error?: string;
}

export interface AppConfig {
  mode: string;
  daemon_enabled: boolean;
  daemon_workers: number;
  daemon_pairs: number;
}

const BASE = "/api/control";
const CONTROL_TIMEOUT = 30_000;

async function extractError(res: Response): Promise<never> {
  let msg = `HTTP ${res.status}`;
  try {
    const b = await res.json();
    if (b.detail) msg += `: ${b.detail}`;
  } catch {}
  throw new Error(msg);
}

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { ...init, signal: init?.signal ?? AbortSignal.timeout(CONTROL_TIMEOUT) });
  if (!res.ok) return extractError(res);
  return res.json();
}

export const controlApi = {
  status: () => fetchJSON<ControlStatus>(`${BASE}/status`),
  decisions: (limit = 50) => fetchJSON<Decision[]>(`${BASE}/decisions?limit=${limit}`),
  getConfig: () => fetchJSON<AppConfig>(`${BASE}/config`),
  setConfig: (config: Partial<AppConfig>) =>
    fetchJSON<AppConfig>(`${BASE}/config`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    }),
  start: () => fetchJSON<{ status: string; mode: string }>(`${BASE}/start`, { method: "POST" }),
  stop: () => fetchJSON<{ status: string }>(`${BASE}/stop`, { method: "POST" }),
  callTool: (toolName: string, args: Record<string, unknown> = {}) =>
    fetchJSON<ToolResult>(`${BASE}/tool/${toolName}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args }),
    }),
  listTools: () => fetchJSON<{ tools: string[] }>(`${BASE}/tools`),
};
