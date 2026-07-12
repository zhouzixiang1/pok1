import { useEffect, useRef, useState } from 'react'

// ── 卡牌:<suit,rank> -> ♠A 等 ──────────────────────────────
// suit 0=♠ 1=♥ 2=♦ 3=♣ ; rank 0=2..8=T..12=A
const SUIT_SYMBOL: Record<string, string> = { '0': '♠', '1': '♥', '2': '♦', '3': '♣' }
const SUIT_RED: Record<string, boolean> = { '0': false, '1': true, '2': true, '3': false }
const RANK: Record<string, string> = {
  '0': '2', '1': '3', '2': '4', '3': '5', '4': '6', '5': '7', '6': '8',
  '7': '9', '8': 'T', '9': 'J', '10': 'Q', '11': 'K', '12': 'A',
}

function parseCard(s: string): { suit: string; rank: string } | null {
  const m = /^<(\d+),(\d+)>$/.exec(s)
  return m ? { suit: m[1], rank: m[2] } : null
}

function CardView({ card, hidden }: { card?: string; hidden?: boolean }) {
  if (hidden) {
    return (
      <span className="inline-flex h-14 w-10 items-center justify-center rounded-md border border-slate-500 bg-gradient-to-b from-slate-700 to-slate-800 text-lg shadow">
        🂠
      </span>
    )
  }
  const c = card ? parseCard(card) : null
  if (!c) {
    return (
      <span className="inline-flex h-14 w-10 items-center justify-center rounded-md border border-dashed border-slate-600 text-xs text-slate-600">
        —
      </span>
    )
  }
  const red = SUIT_RED[c.suit]
  return (
    <span
      className={`inline-flex h-14 w-10 flex-col items-center justify-center rounded-md border border-slate-300 bg-white font-bold shadow ${
        red ? 'text-rose-600' : 'text-slate-900'
      }`}
    >
      <span className="text-sm leading-none">{RANK[c.rank]}</span>
      <span className="text-lg leading-none">{SUIT_SYMBOL[c.suit]}</span>
    </span>
  )
}

// ── 状态 ────────────────────────────────────────────────────
interface ActionLog {
  idx: number
  action: string
  amount?: number
  stage: string
  hand: number
  text: string
}
interface SettleInfo {
  hand: number
  isShowdown: boolean
  winnerIdx: number | null
  pot: number
  earnings: number[]
  sbHand?: string
  bbHand?: string
}
interface ArenaState {
  connected: boolean
  status: string
  matchId: string | null
  names: string[]
  handsPerMatch: number
  handNum: number
  totalEarnings: number[]
  matchesPlayed: number
  chips: number[]
  holeCards: string[][]
  community: string[]
  pot: number
  stage: string
  folded: boolean[]
  actingIdx: number
  deadlineMs: number
  actions: ActionLog[]
  lastSettle: SettleInfo | null
  matchEndReason: string | null
  matchEndLoser: number | null
}

const INITIAL: ArenaState = {
  connected: false,
  status: 'idle',
  matchId: null,
  names: ['等待连接…', '等待连接…'],
  handsPerMatch: 70,
  handNum: 0,
  totalEarnings: [0, 0],
  matchesPlayed: 0,
  chips: [20000, 20000],
  holeCards: [[], []],
  community: [],
  pot: 0,
  stage: '',
  folded: [false, false],
  actingIdx: -1,
  deadlineMs: 0,
  actions: [],
  lastSettle: null,
  matchEndReason: null,
  matchEndLoser: null,
}

const STAGE_LABEL: Record<string, string> = {
  preflop: '翻前', flop: '翻牌', turn: '转牌', river: '河牌',
}

const STATUS_LABEL: Record<string, string> = {
  idle: '未启动', listening: '等待引擎连接', waiting_clients: '等待第二个引擎',
  playing: '比赛进行中', ended: '比赛结束', stopping: '停止中', match_ended: '比赛结束',
}

function actionText(ev: any): string {
  const a = ev.action
  if (a === 'fold') return '弃牌'
  if (a === 'check') return '过牌'
  if (a === 'call') return `跟注 ${ev.amount ?? 0}`
  if (a === 'raise') return `加注到 ${ev.amount}`
  if (a === 'allin') return `全押 ${ev.amount ?? ''}`
  if (a === 'timeout') return '⏱ 超时→弃牌'
  if (typeof a === 'string' && a.startsWith('illegal:')) return `⛔ 非法 ${a.slice(8)} →弃牌`
  return String(a)
}

function applyEvent(prev: ArenaState, ev: any): ArenaState {
  const t = ev.type
  const s: ArenaState = { ...prev }
  switch (t) {
    case 'snapshot':
      s.connected = true
      s.status = ev.status ?? s.status
      s.matchId = ev.match_id ?? s.matchId
      if (ev.names?.length) s.names = ev.names
      s.handsPerMatch = ev.hands_per_match ?? s.handsPerMatch
      s.handNum = ev.hand_num ?? 0
      s.totalEarnings = ev.total_earnings ?? s.totalEarnings
      s.matchesPlayed = ev.matches_played ?? 0
      return s
    case 'connected':
      s.connected = true
      return s
    case 'server_started':
      s.status = 'listening'
      return s
    case 'match_start':
      s.matchId = ev.match_id
      s.names = ev.names?.length ? ev.names : s.names
      s.handsPerMatch = ev.hands ?? s.handsPerMatch
      s.handNum = 0
      s.totalEarnings = [0, 0]
      s.actions = []
      s.lastSettle = null
      s.matchEndReason = null
      s.matchEndLoser = null
      s.status = 'playing'
      return s
    case 'hand_start':
      s.handNum = ev.hand
      s.chips = ev.player_chips ?? s.chips
      s.pot = ev.pot ?? 0
      s.holeCards = [[], []]
      s.community = []
      s.stage = 'preflop'
      s.folded = [false, false]
      s.actingIdx = -1
      s.deadlineMs = 0
      s.lastSettle = null
      return s
    case 'cards_dealt':
      if (ev.hole_cards) s.holeCards = ev.hole_cards
      return s
    case 'stage':
      s.stage = ev.stage
      if (ev.cards) s.community = [...s.community, ...ev.cards]
      return s
    case 'action_requested':
      s.actingIdx = ev.player_idx
      s.deadlineMs = ev.deadline_epoch_ms ?? 0
      s.pot = ev.pot ?? s.pot
      return s
    case 'action': {
      const text = actionText(ev)
      s.actions = [
        ...s.actions.slice(-200),
        { idx: ev.player_idx, action: ev.action, amount: ev.amount, stage: ev.stage, hand: ev.hand, text },
      ]
      if (ev.player_chips) s.chips = ev.player_chips
      if (ev.pot != null) s.pot = ev.pot
      const folded =
        ev.action === 'fold' ||
        ev.action === 'timeout' ||
        (typeof ev.action === 'string' && ev.action.startsWith('illegal:'))
      if (folded && typeof ev.player_idx === 'number') {
        const f = [...s.folded]
        f[ev.player_idx] = true
        s.folded = f
      }
      s.actingIdx = -1
      return s
    }
    case 'settle':
      s.lastSettle = {
        hand: ev.hand,
        isShowdown: ev.is_showdown,
        winnerIdx: ev.winner_idx ?? null,
        pot: ev.pot,
        earnings: ev.earnings,
        sbHand: ev.sb_hand,
        bbHand: ev.bb_hand,
      }
      if (ev.player_chips) s.chips = ev.player_chips
      s.pot = 0
      s.actingIdx = -1
      s.deadlineMs = 0
      return s
    case 'match_end':
      s.matchEndReason = ev.reason ?? 'completed'
      s.matchEndLoser = ev.loser_idx ?? null
      s.status = 'ended'
      if (ev.total_earnings) s.totalEarnings = ev.total_earnings
      if (ev.hands_played) s.handNum = ev.hands_played
      s.actingIdx = -1
      s.deadlineMs = 0
      return s
    default:
      return prev
  }
}

function useNowTick(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const i = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(i)
  }, [])
  return now
}

export default function ArenaTable() {
  const [state, setState] = useState<ArenaState>(INITIAL)
  const [showLog, setShowLog] = useState(true)
  const logRef = useRef<HTMLDivElement>(null)
  const now = useNowTick()

  useEffect(() => {
    const es = new EventSource('/api/arena/events')
    es.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data)
        setState((prev) => applyEvent(prev, ev))
      } catch {
        /* 忽略非 JSON 帧(keepalive 注释行不进 onmessage) */
      }
    }
    es.onopen = () => setState((prev) => ({ ...prev, connected: true }))
    es.onerror = () => setState((prev) => ({ ...prev, connected: false }))
    return () => es.close()
  }, [])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [state.actions])

  const remaining =
    state.deadlineMs > 0 ? Math.max(0, Math.round((state.deadlineMs - now) / 1000)) : null

  const renderPlayer = (idx: number, isLower: boolean) => {
    const name = state.names[idx] ?? `玩家${idx}`
    const chips = state.chips[idx] ?? 20000
    const cards = state.holeCards[idx] ?? []
    const folded = state.folded[idx]
    const acting = state.actingIdx === idx
    const earnings = state.totalEarnings[idx] ?? 0
    const showCards = cards.length > 0 && (!folded || state.lastSettle?.isShowdown)
    return (
      <div
        className={`flex flex-col items-center gap-1 rounded-xl border p-4 transition ${
          acting
            ? 'border-amber-400 bg-amber-400/10 shadow-[0_0_20px_rgba(251,191,36,0.3)]'
            : 'border-slate-700 bg-slate-800/60'
        } ${folded ? 'opacity-50' : ''}`}
      >
        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-400">{isLower ? '▼ 桌面下方' : '▲ 桌面上方'}</span>
          {acting && (
            <span className="rounded bg-amber-400 px-1.5 py-0.5 text-[10px] font-bold text-slate-900">
              行动中
            </span>
          )}
          {folded && (
            <span className="rounded bg-slate-600 px-1.5 py-0.5 text-[10px] text-slate-200">已弃牌</span>
          )}
        </div>
        <div className="text-lg font-semibold text-slate-100">{name}</div>
        <div className="flex gap-1">
          {cards.length === 0 ? (
            <>
              <CardView />
              <CardView />
            </>
          ) : (
            cards.map((c, i) => <CardView key={i} card={showCards ? c : undefined} hidden={!showCards} />)
          )}
        </div>
        <div className="mt-1 text-sm">
          <span className="text-slate-400">筹码 </span>
          <span className="font-mono font-bold text-emerald-300">{chips.toLocaleString()}</span>
        </div>
        <div className="text-xs">
          <span className="text-slate-500">累计 </span>
          <span className={`font-mono ${earnings >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {earnings >= 0 ? '+' : ''}
            {earnings.toLocaleString()}
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col gap-3 p-4">
      <header className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate-700 bg-slate-900/80 px-4 py-3">
        <div className="flex items-center gap-3">
          <span
            className={`h-3 w-3 rounded-full ${state.connected ? 'bg-emerald-400' : 'bg-slate-500'}`}
            title={state.connected ? '已连接 SSE' : '断开'}
          />
          <h1 className="text-lg font-bold text-slate-100">德州扑克对弈平台</h1>
          <span className="rounded bg-slate-700 px-2 py-0.5 text-xs text-slate-200">
            {STATUS_LABEL[state.status] ?? state.status}
          </span>
        </div>
        <div className="flex items-center gap-4 text-sm text-slate-300">
          <span>
            第 <span className="font-mono font-bold text-amber-300">{state.handNum}</span> /{' '}
            {state.handsPerMatch} 手
          </span>
          <span>
            已完成 <span className="font-mono">{state.matchesPlayed}</span> 场
          </span>
          {state.matchId && (
            <span className="hidden font-mono text-xs text-slate-500 md:inline">{state.matchId}</span>
          )}
        </div>
      </header>

      <main className="grid gap-3 lg:grid-cols-[1fr_320px]">
        <section className="rounded-2xl border border-slate-700 bg-gradient-to-b from-emerald-950 to-slate-900 p-5">
          {renderPlayer(1, false)}
          <div className="my-4 flex flex-col items-center gap-2">
            <div className="text-xs uppercase tracking-wider text-emerald-300/70">
              {state.stage ? STAGE_LABEL[state.stage] ?? state.stage : '公共牌'}
            </div>
            <div className="flex min-h-[56px] items-center gap-1.5">
              {state.community.length === 0 ? (
                <>
                  <CardView />
                  <CardView />
                  <CardView />
                  <CardView />
                  <CardView />
                </>
              ) : (
                state.community.map((c, i) => <CardView key={i} card={c} />)
              )}
            </div>
            <div className="flex items-center gap-4">
              <div className="text-sm text-slate-400">
                底池 <span className="font-mono text-lg font-bold text-amber-300">{state.pot.toLocaleString()}</span>
              </div>
              {remaining != null && (
                <div
                  className={`rounded-lg px-3 py-1 font-mono text-lg font-bold ${
                    remaining <= 10 ? 'bg-rose-600 text-white' : 'bg-slate-700 text-amber-300'
                  }`}
                >
                  ⏱ {remaining}s
                </div>
              )}
            </div>
          </div>
          {renderPlayer(0, true)}

          {state.lastSettle && (
            <div className="mt-3 rounded-lg border border-slate-600 bg-slate-800/80 p-3 text-sm">
              <div className="font-semibold text-slate-200">
                第 {state.lastSettle.hand} 手结算：
                {state.lastSettle.winnerIdx == null
                  ? '平局'
                  : `${state.names[state.lastSettle.winnerIdx] ?? `玩家${state.lastSettle.winnerIdx}`} 赢得底池`}
                <span className="ml-2 font-mono text-amber-300">{state.lastSettle.pot.toLocaleString()}</span>
              </div>
              {state.lastSettle.isShowdown && (
                <div className="mt-1 text-xs text-slate-400">
                  {state.lastSettle.sbHand && <>SB({state.lastSettle.sbHand}) </>}
                  {state.lastSettle.bbHand && <>BB({state.lastSettle.bbHand})</>}
                </div>
              )}
            </div>
          )}

          {state.matchEndReason && (
            <div
              className={`mt-3 rounded-lg p-3 text-center font-semibold ${
                state.matchEndReason === 'completed'
                  ? 'bg-emerald-700/40 text-emerald-200'
                  : 'bg-rose-700/40 text-rose-200'
              }`}
            >
              比赛结束 ·{' '}
              {state.matchEndReason === 'disconnected'
                ? `玩家${state.matchEndLoser ?? ''}(${state.names[state.matchEndLoser ?? -1] ?? ''}) 断线弃权`
                : state.matchEndReason === 'completed'
                ? '正常完成'
                : state.matchEndReason}
              <span className="ml-2 font-mono">
                ({state.totalEarnings[0].toLocaleString()} / {state.totalEarnings[1].toLocaleString()})
              </span>
            </div>
          )}
        </section>

        <aside className="flex flex-col rounded-2xl border border-slate-700 bg-slate-900/80">
          <button
            onClick={() => setShowLog((v) => !v)}
            className="flex items-center justify-between px-4 py-2 text-left text-sm font-semibold text-slate-200"
          >
            <span>动作历史 ({state.actions.length})</span>
            <span className="text-xs text-slate-500">{showLog ? '收起 ▲' : '展开 ▼'}</span>
          </button>
          {showLog && (
            <div ref={logRef} className="max-h-[60vh] flex-1 overflow-y-auto px-3 pb-3 font-mono text-xs">
              {state.actions.length === 0 ? (
                <div className="px-2 py-4 text-center text-slate-500">暂无动作</div>
              ) : (
                state.actions.map((a, i) => (
                  <div
                    key={i}
                    className={`border-b border-slate-800 py-1 ${
                      a.idx === 0 ? 'text-sky-300' : 'text-fuchsia-300'
                    }`}
                  >
                    <span className="text-slate-500">[H{a.hand} {STAGE_LABEL[a.stage] ?? a.stage}]</span>{' '}
                    {state.names[a.idx] ?? `P${a.idx}`}: {a.text}
                  </div>
                ))
              )}
            </div>
          )}
        </aside>
      </main>

      <footer className="text-center text-xs text-slate-600">
        pok-arena · 刷新页面靠 SSE snapshot 首帧恢复 · TCP 50101 / Web 50180
      </footer>
    </div>
  )
}
