import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import PageMeta from "../components/common/PageMeta";
import { NationalPokerTable } from "../components/national-arena/NationalPokerTable";
import { Badge } from "../components/shared/Badge";
import { EmptyState } from "../components/shared/EmptyState";
import { SegmentedControl } from "../components/shared/SegmentedControl";
import { api } from "../api/client";
import type {
  ArenaBot,
  ArenaCreatePayload,
  ArenaEvent,
  ArenaMode,
  ArenaSession,
  ArenaWireRecord,
} from "../api/types";
import { actionLabel, buildArenaView } from "../lib/arenaViewModel";
import { BoltIcon, CloseIcon, DownloadIcon, PlugInIcon } from "../icons";
import { cn } from "../lib/utils";

const ACTIVE = new Set(["starting", "waiting_for_players", "ready", "running", "stopping", "finalizing"]);
const EVENT_TYPES = [
  "session_starting", "runtime_capacity_waiting", "runtime_capacity_acquired",
  "official_platform_resource_acquired", "server_listening", "player_connected",
  "connection_rejected", "player_named", "players_ready",
  "match_started", "hand_started", "hole_cards_dealt", "street_started",
  "action_requested", "player_action", "illegal_action", "timeout",
  "hand_finished", "engine_match_summary", "thp_written", "partial_thp_written",
  "match_finished", "bot_process_started", "bot_process_exited",
  "session_stopping", "session_finalizing", "session_stopped", "session_failed", "wire_log_incomplete",
] as const;

const inputClass = "h-10 w-full rounded-md border border-gray-200 bg-white px-3 text-sm text-gray-800 outline-none transition focus:border-brand-400 dark:border-border-subtle dark:bg-surface-0 dark:text-white";

function statusVariant(status?: string): "success" | "warning" | "error" | "info" | "neutral" {
  if (status === "finished") return "success";
  if (status === "failed") return "error";
  if (ACTIVE.has(status || "")) return "info";
  if (status === "stopped") return "warning";
  return "neutral";
}

function statusLabel(status?: string): string {
  return {
    created: "已创建",
    starting: "排队启动",
    waiting_for_players: "等待连接",
    ready: "已就绪",
    running: "对局中",
    stopping: "停止中",
    finalizing: "清理中",
    finished: "本地完成",
    failed: "失败",
    stopped: "已停止",
  }[status || ""] || "空闲";
}

function certificationLabel(bot?: ArenaBot): { text: string; variant: "success" | "warning" | "neutral" } {
  if (!bot) return { text: "未选择", variant: "neutral" };
  if (bot.certification.official_full_certified) {
    return { text: "EXE 正式通过", variant: "success" };
  }
  if (bot.certification.eligibility_basis === "content_bound_grandfather") {
    return { text: "历史过渡准入（未正式认证）", variant: "warning" };
  }
  return { text: "EXE 状态待核", variant: "neutral" };
}

function ConfigLabel({ children }: { children: React.ReactNode }) {
  return <label className="mb-1.5 block text-xs font-medium text-gray-600 dark:text-gray-400">{children}</label>;
}

export default function NationalArena() {
  const [mode, setMode] = useState<ArenaMode>("managed_bots");
  const [bots, setBots] = useState<ArenaBot[]>([]);
  const [sessions, setSessions] = useState<ArenaSession[]>([]);
  const [session, setSession] = useState<ArenaSession | null>(null);
  const [events, setEvents] = useState<ArenaEvent[]>([]);
  const [wire, setWire] = useState<ArenaWireRecord[]>([]);
  const [topBot, setTopBot] = useState("");
  const [bottomBot, setBottomBot] = useState("");
  const [host, setHost] = useState("127.0.0.1");
  const [port, setPort] = useState(10001);
  const [hands, setHands] = useState(70);
  const [timeout, setTimeoutValue] = useState(60);
  const [actionDelay, setActionDelay] = useState(0.3);
  const [controlToken, setControlToken] = useState(
    () => window.sessionStorage.getItem("pok_arena_control_token") || "",
  );
  const [panel, setPanel] = useState("actions");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const wireCursor = useRef(0);
  const eventCursor = useRef(0);

  useEffect(() => {
    if (controlToken) window.sessionStorage.setItem("pok_arena_control_token", controlToken);
    else window.sessionStorage.removeItem("pok_arena_control_token");
  }, [controlToken]);

  const refreshSessions = useCallback(async () => {
    const data = await api.arenaSessions();
    setSessions(data.sessions);
    return data.sessions;
  }, []);

  const selectSession = useCallback(async (sessionId: string) => {
    if (!sessionId) {
      setSession(null);
      setEvents([]);
      setWire([]);
      eventCursor.current = 0;
      return;
    }
    const [nextSession, history] = await Promise.all([
      api.arenaSession(sessionId),
      api.arenaEventHistory(sessionId),
    ]);
    setSession(nextSession);
    setEvents(history.events);
    eventCursor.current = history.events.length
      ? history.events[history.events.length - 1].event_id
      : 0;
    setWire([]);
    wireCursor.current = 0;
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.arenaBots(), api.arenaSessions()])
      .then(([botData, sessionData]) => {
        if (cancelled) return;
        setBots(botData.bots);
        setSessions(sessionData.sessions);
        setTopBot((current) => current || botData.bots[0]?.id || "");
        setBottomBot((current) => current || botData.bots[1]?.id || botData.bots[0]?.id || "");
        const selected = sessionData.sessions.find((item) => ACTIVE.has(item.status)) || sessionData.sessions[0];
        if (selected) void selectSession(selected.session_id);
      })
      .catch((reason) => !cancelled && setError(String(reason)));
    return () => { cancelled = true; };
  }, [selectSession]);

  useEffect(() => {
    if (!session?.session_id) return;
    const sessionId = session.session_id;
    const source = new EventSource(
      `/api/national-arena/sessions/${encodeURIComponent(sessionId)}/events?after_event_id=${eventCursor.current}`,
    );
    const onSnapshot = (raw: MessageEvent) => {
      const payload = JSON.parse(raw.data) as { session: ArenaSession };
      setSession(payload.session);
    };
    const onArenaEvent = (raw: MessageEvent) => {
      const event = JSON.parse(raw.data) as ArenaEvent;
      eventCursor.current = Math.max(eventCursor.current, event.event_id);
      setEvents((current) => current.some((item) => item.event_id === event.event_id)
        ? current
        : [...current, event].slice(-5000));
      if (["hand_finished", "match_finished", "session_failed", "session_stopped", "thp_written"].includes(event.type)) {
        void api.arenaSession(sessionId).then(setSession);
      }
    };
    const onClosed = () => {
      source.close();
      void api.arenaSession(sessionId).then(setSession);
    };
    source.addEventListener("snapshot", onSnapshot as EventListener);
    source.addEventListener("stream_closed", onClosed);
    EVENT_TYPES.forEach((name) => source.addEventListener(name, onArenaEvent as EventListener));
    source.onerror = () => {
      if (session.finished_at) source.close();
    };
    return () => source.close();
  }, [session?.session_id, session?.finished_at]);

  useEffect(() => {
    if (!session?.session_id) return;
    const sessionId = session.session_id;
    let cancelled = false;
    const poll = async () => {
      try {
        const [state, trace] = await Promise.all([
          api.arenaSession(sessionId),
          api.arenaWireHistory(sessionId, wireCursor.current, 1000),
        ]);
        if (cancelled) return;
        setSession(state);
        if (trace.records.length) {
          wireCursor.current = trace.records[trace.records.length - 1].sequence;
          setWire((current) => [...current, ...trace.records].slice(-400));
        }
      } catch {
        // SSE and the next poll remain the recovery path.
      }
    };
    void poll();
    const timer = window.setInterval(poll, ACTIVE.has(session.status) ? 1000 : 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [session?.session_id, session?.status]);

  const view = useMemo(() => buildArenaView(session, events), [session, events]);
  const selectedTop = bots.find((bot) => bot.id === topBot);
  const selectedBottom = bots.find((bot) => bot.id === bottomBot);
  const topCert = certificationLabel(selectedTop);
  const bottomCert = certificationLabel(selectedBottom);

  const startMatch = async () => {
    setBusy(true);
    setError("");
    try {
      const payload: ArenaCreatePayload = {
        mode,
        host,
        port,
        hands,
        action_timeout_seconds: timeout,
        official_action_delay: actionDelay,
        top_bot: mode === "managed_bots" ? topBot : null,
        bottom_bot: mode === "managed_bots" ? bottomBot : null,
      };
      const created = await api.createArenaSession(payload, controlToken);
      const started = await api.startArenaSession(created.session_id, controlToken);
      await refreshSessions();
      await selectSession(started.session_id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const stopMatch = async () => {
    if (!session) return;
    setBusy(true);
    try {
      setSession(await api.stopArenaSession(session.session_id, controlToken));
      await refreshSessions();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageMeta title="国赛对弈 | Bot 自进化" description="National TCP local arena" />
      <div className="space-y-5">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-gray-900 dark:text-white">国赛对弈</h1>
            <div className="mt-2 flex flex-wrap gap-2">
              <Badge variant="warning">本地完成不等于国赛合规</Badge>
              {session && <Badge variant={statusVariant(session.status)} pulse={ACTIVE.has(session.status)}>{statusLabel(session.status)}</Badge>}
              {session?.wire_log_complete === false && <Badge variant="error">通信日志不完整</Badge>}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <select
              aria-label="历史 Arena 会话"
              className="h-9 max-w-56 rounded-md border border-gray-200 bg-white px-2 text-xs text-gray-700 dark:border-border-subtle dark:bg-surface-0 dark:text-gray-300"
              value={session?.session_id || ""}
              onChange={(event) => void selectSession(event.target.value)}
            >
              <option value="">新对局</option>
              {sessions.map((item) => (
                <option key={item.session_id} value={item.session_id}>
                  {item.session_id.replace("arena_", "")} · {statusLabel(item.status)}
                </option>
              ))}
            </select>
            {session?.artifacts.thp && (
              <a
                href={`/api/national-arena/sessions/${encodeURIComponent(session.session_id)}/thp`}
                className="inline-flex h-9 items-center gap-1.5 rounded-md border border-gray-200 px-3 text-xs font-medium text-gray-700 hover:bg-gray-50 dark:border-border-subtle dark:text-gray-300 dark:hover:bg-white/[0.04]"
              >
                <DownloadIcon className="size-4" /> THP
              </a>
            )}
          </div>
        </header>

        <section className="border-y border-gray-200 py-4 dark:border-border-subtle">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <SegmentedControl
              value={mode}
              onChange={(value) => setMode(value as ArenaMode)}
              options={[
                { value: "managed_bots", label: "Bot 池" },
                { value: "external_tcp", label: "外部 TCP" },
              ]}
            />
            {session && ACTIVE.has(session.status) && (
              <button
                type="button"
                onClick={stopMatch}
                disabled={busy}
                className="inline-flex h-9 items-center gap-1.5 rounded-md border border-error-300 px-3 text-xs font-medium text-error-700 hover:bg-error-50 disabled:opacity-50 dark:border-error-700 dark:text-error-400"
              >
                <CloseIcon className="size-4" /> 停止
              </button>
            )}
          </div>

          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            {mode === "managed_bots" ? (
              <>
                <div className="xl:col-span-2">
                  <ConfigLabel>桌面上方 Bot</ConfigLabel>
                  <select className={inputClass} value={topBot} onChange={(event) => setTopBot(event.target.value)}>
                    {bots.map((bot) => <option key={bot.id} value={bot.id}>{bot.display_name}</option>)}
                  </select>
                  <div className="mt-1"><Badge variant={topCert.variant}>{topCert.text}</Badge></div>
                </div>
                <div className="xl:col-span-2">
                  <ConfigLabel>桌面下方 Bot</ConfigLabel>
                  <select className={inputClass} value={bottomBot} onChange={(event) => setBottomBot(event.target.value)}>
                    {bots.map((bot) => <option key={bot.id} value={bot.id}>{bot.display_name}</option>)}
                  </select>
                  <div className="mt-1"><Badge variant={bottomCert.variant}>{bottomCert.text}</Badge></div>
                </div>
              </>
            ) : (
              <div className="xl:col-span-2">
                <ConfigLabel>监听地址</ConfigLabel>
                <input className={inputClass} value={host} onChange={(event) => setHost(event.target.value)} />
              </div>
            )}
            <div>
              <ConfigLabel>TCP 端口</ConfigLabel>
              <input className={inputClass} type="number" min={0} max={65535} value={port} onChange={(event) => setPort(Number(event.target.value))} />
            </div>
            <div>
              <ConfigLabel>手数</ConfigLabel>
              <input className={inputClass} type="number" min={1} max={70} value={hands} onChange={(event) => setHands(Number(event.target.value))} />
            </div>
            <div>
              <ConfigLabel>决策秒数</ConfigLabel>
              <input className={inputClass} type="number" min={1} max={60} value={timeout} onChange={(event) => setTimeoutValue(Number(event.target.value))} />
            </div>
            <div>
              <ConfigLabel>动作延迟</ConfigLabel>
              <input className={inputClass} type="number" min={0} max={5} step={0.05} value={actionDelay} onChange={(event) => setActionDelay(Number(event.target.value))} />
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={startMatch}
              disabled={busy || ACTIVE.has(session?.status || "") || (mode === "managed_bots" && (!topBot || !bottomBot))}
              className="inline-flex h-10 items-center gap-2 rounded-md bg-brand-500 px-4 text-sm font-medium text-white hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {mode === "managed_bots" ? <BoltIcon className="size-4" /> : <PlugInIcon className="size-4" />}
              {busy ? "处理中" : "创建并开始"}
            </button>
            <input
              aria-label="Arena 远程控制令牌"
              className="h-10 w-48 rounded-md border border-gray-200 bg-white px-3 text-xs text-gray-800 outline-none focus:border-brand-400 dark:border-border-subtle dark:bg-surface-0 dark:text-white"
              type="password"
              autoComplete="off"
              placeholder="远程控制令牌"
              value={controlToken}
              onChange={(event) => setControlToken(event.target.value)}
            />
            {session?.status === "waiting_for_players" && (
              <code className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-xs text-gray-700 dark:border-border-subtle dark:bg-white/[0.03] dark:text-gray-300">
                {session.host}:{session.port} · {session.connected_players}/2
              </code>
            )}
            <span className="text-xs text-gray-500">所有正式 bot 仍须通过 Windows EXE 5+3×70 手验收</span>
          </div>
          {error && <div className="mt-3 rounded-md border border-error-200 bg-error-50 px-3 py-2 text-sm text-error-700 dark:border-error-800 dark:bg-error-900/20 dark:text-error-400">{error}</div>}
        </section>

        {session ? (
          <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
            <div className="min-w-0">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-gray-500">
                <span>第 <strong className="text-gray-800 dark:text-gray-200">{view.hand}</strong> / {view.handsTotal} 手</span>
                <span className="tabular-nums">累计 {session.top_total_earnings} : {session.bottom_total_earnings}</span>
              </div>
              <NationalPokerTable session={session} view={view} />
              {session.status === "finished" && (
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-gray-200 pt-3 text-sm dark:border-border-subtle">
                  <span className="font-medium text-gray-800 dark:text-gray-200">本地对局完成 · {session.winner === "tie" ? "平局" : session.winner}</span>
                  <Badge variant="warning">不改变官方 EXE 认证</Badge>
                </div>
              )}
              {session.failure_reason && <div className="mt-3 text-sm text-error-600">{session.failure_reason}</div>}
            </div>

            <aside className="min-w-0 rounded-lg border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-0">
              <div className="border-b border-gray-200 p-3 dark:border-border-subtle">
                <SegmentedControl
                  value={panel}
                  onChange={setPanel}
                  className="w-full [&>button]:flex-1"
                  options={[
                    { value: "actions", label: "动作" },
                    { value: "hands", label: "牌局" },
                    { value: "wire", label: "通信" },
                  ]}
                />
              </div>
              <div className="h-[520px] overflow-y-auto p-3 sm:h-[560px] xl:h-[620px]">
                {panel === "actions" && (
                  view.actions.length ? <div className="space-y-1.5">
                    {[...view.actions].reverse().slice(0, 150).map((action) => (
                      <div key={action.eventId} className="grid grid-cols-[44px_1fr_auto] items-center gap-2 border-b border-gray-100 py-2 text-xs dark:border-border-subtle">
                        <span className="text-gray-400">#{action.hand}</span>
                        <span className="truncate text-gray-700 dark:text-gray-300">
                          {action.playerIdx === 0 ? (session.top_player_name || session.top_bot) : (session.bottom_player_name || session.bottom_bot)}
                        </span>
                        <span className={cn("font-medium", action.action.startsWith("illegal") || action.action === "timeout" ? "text-error-600" : "text-gray-900 dark:text-white")}>
                          {actionLabel(action.action)}
                        </span>
                      </div>
                    ))}
                  </div> : <EmptyState message="暂无动作" />
                )}
                {panel === "hands" && (
                  view.history.length ? <div className="space-y-1.5">
                    {[...view.history].reverse().map((hand) => (
                      <div key={hand.eventId} className="flex items-center justify-between gap-3 border-b border-gray-100 py-2 text-xs dark:border-border-subtle">
                        <span className="text-gray-500">第 {hand.hand} 手</span>
                        <span className="min-w-0 flex-1 truncate text-gray-700 dark:text-gray-300">
                          {hand.winnerIdx === null ? "平局" : hand.winnerIdx === 0 ? (session.top_player_name || session.top_bot) : (session.bottom_player_name || session.bottom_bot)}
                        </span>
                        <span className="font-medium tabular-nums text-gray-900 dark:text-white">{hand.earnings[0]} : {hand.earnings[1]}</span>
                      </div>
                    ))}
                  </div> : <EmptyState message="暂无结算" />
                )}
                {panel === "wire" && (
                  <>
                    {Object.entries(session.artifacts).some(([key]) => key.includes("stdout") || key.includes("stderr") || key.includes("decision")) && (
                      <div className="mb-3 flex flex-wrap gap-1.5 border-b border-gray-100 pb-3 dark:border-border-subtle">
                        {Object.entries(session.artifacts)
                          .filter(([key]) => key.includes("stdout") || key.includes("stderr") || key.includes("decision"))
                          .map(([key]) => (
                            <a
                              key={key}
                              href={`/api/national-arena/sessions/${encodeURIComponent(session.session_id)}/artifacts/${encodeURIComponent(key)}`}
                              className="rounded-md border border-gray-200 px-2 py-1 text-[10px] font-medium text-gray-600 hover:bg-gray-50 dark:border-border-subtle dark:text-gray-400"
                            >
                              {key}
                            </a>
                          ))}
                      </div>
                    )}
                    {wire.length ? <div className="space-y-1.5 font-mono text-[11px]">
                      {[...wire].reverse().map((record) => (
                        <div key={record.sequence} className="border-b border-gray-100 py-2 dark:border-border-subtle">
                          <div className="mb-1 flex items-center justify-between text-gray-400">
                            <span>#{record.sequence} P{record.player_idx + 1}</span>
                            <span>{record.direction === "server_to_bot" ? "SERVER → BOT" : "BOT → SERVER"}</span>
                          </div>
                          <div className="break-all text-gray-700 dark:text-gray-300">{record.payload}</div>
                        </div>
                      ))}
                    </div> : <EmptyState message="暂无通信记录" />}
                  </>
                )}
              </div>
            </aside>
          </div>
        ) : (
          <div className="py-16"><EmptyState message="暂无 Arena 会话" /></div>
        )}
      </div>
    </>
  );
}
