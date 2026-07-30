import { useEffect, useRef, useState } from 'react'
import CardView from '../components/CardView'
import { apiGet } from '../api'

/* ══════════════════════════════════════════════════════════
 * 实时观赛牌桌。
 *
 * 数据源:
 *  - 进入时拉 /api/state 获取 current_match_id
 *  - 若有,订阅 /api/matches/{id}/events(SSE)
 *    · 首帧 snapshot(含 events: 已落盘历史事件)
 *    · 后续按 match_id 过滤的事件(hand_start/stage/action/settle/...)
 *  - 没有进行中比赛时,引导去 /history 或 /challenge
 *
 * 事件结构沿用旧版 applyEvent(与原 server SSE 兼容)。
 * ══════════════════════════════════════════════════════════ */

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
  preflop: '翻前',
  flop: '翻牌',
  turn: '转牌',
  river: '河牌',
  showdown: '摊牌',
}

const STATUS_LABEL: Record<string, string> = {
  idle: '空闲',
  running: '进行中',
  pending: '排队中',
  completed: '已完成',
  errored: '出错',
  not_found: '未找到',
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
    case 'snapshot': {
      // /api/matches/{id}/events 首帧:含 status / winner / earnings / events
      s.connected = true
      s.status = ev.status ?? s.status
      s.matchId = ev.match_id ?? s.matchId
      s.handNum = ev.hands_played ?? s.handNum
      s.handsPerMatch = ev.total_hands ?? s.handsPerMatch
      if (Array.isArray(ev.earnings)) s.totalEarnings = ev.earnings
      // 重放历史事件(从空状态开始)
      let st = { ...INITIAL, connected: true, matchId: s.matchId, status: s.status }
      for (const h of ev.events || []) st = applyEvent(st, h)
      return { ...s, ...st, connected: true }
    }
    case 'connected':
      s.connected = true
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
      s.status = 'running'
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
      if (ev.names?.length) s.names = ev.names
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
      s.status = 'completed'
      if (ev.total_earnings) s.totalEarnings = ev.total_earnings
      if (ev.hands_played) s.handNum = ev.hands_played
      s.actingIdx = -1
      s.deadlineMs = 0
      return s
    default:
      return prev
  }
}

export default function ArenaTable() {
  const [state, setState] = useState<ArenaState>(INITIAL)
  const [showLog, setShowLog] = useState(true)
  const [manualMatchId, setManualMatchId] = useState('')
  const [targetMatchId, setTargetMatchId] = useState<string | null>(null)
  const [noMatch, setNoMatch] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)

  // 取当前进行中的对局(自动观赛)
  useEffect(() => {
    if (targetMatchId !== null) return // 已手动指定
    apiGet<any>('/api/state')
      .then((d) => {
        if (d?.current_match_id) {
          setTargetMatchId(d.current_match_id)
        } else {
          setNoMatch(true)
        }
      })
      .catch(() => setNoMatch(true))
  }, [targetMatchId])

  // 订阅 SSE
  useEffect(() => {
    if (!targetMatchId) return
    setState({ ...INITIAL })
    const es = new EventSource(`/api/matches/${encodeURIComponent(targetMatchId)}/events`)
    es.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data)
        setState((prev) => applyEvent(prev, ev))
      } catch {
        /* 忽略 keepalive 注释 */
      }
    }
    es.onopen = () => setState((prev) => ({ ...prev, connected: true }))
    es.onerror = () => setState((prev) => ({ ...prev, connected: false }))
    return () => es.close()
  }, [targetMatchId])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [state.actions])

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
            ? 'border-brand-300 bg-white shadow-[0_0_0_3px_rgba(70,95,255,0.25)]'
            : 'border-white/20 bg-white/95 shadow-theme-sm'
        } ${folded ? 'opacity-50' : ''}`}
      >
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">{isLower ? '▼ 我方' : '▲ 对手'}</span>
          {acting && (
            <span className="rounded bg-brand-500 px-1.5 py-0.5 text-[10px] font-bold text-white">行动中</span>
          )}
          {folded && (
            <span className="rounded bg-gray-200 px-1.5 py-0.5 text-[10px] text-gray-800">已弃牌</span>
          )}
        </div>
        <div className="text-lg font-semibold text-gray-900">{name}</div>
        <div className="flex gap-1">
          {cards.length === 0 ? (
            <>
              <CardView hidden />
              <CardView hidden />
            </>
          ) : (
            cards.map((c, i) => <CardView key={i} card={showCards ? c : undefined} hidden={!showCards} />)
          )}
        </div>
        <div className="mt-1 text-sm">
          <span className="text-gray-500">筹码 </span>
          <span className="font-mono font-bold text-success-600">{chips.toLocaleString()}</span>
        </div>
        <div className="text-xs">
          <span className="text-gray-500">累计 </span>
          <span className={`font-mono ${earnings >= 0 ? 'text-success-500' : 'text-error-500'}`}>
            {earnings >= 0 ? '+' : ''}
            {earnings.toLocaleString()}
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className="mx-auto flex min-h-[80vh] max-w-6xl flex-col gap-3 p-4">
      <header className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-gray-200 bg-white/80 px-4 py-3">
        <div className="flex items-center gap-3">
          <span
            className={`h-3 w-3 rounded-full ${state.connected ? 'bg-success-400' : 'bg-gray-400'}`}
            title={state.connected ? '已连接 SSE' : '断开'}
          />
          <h1 className="text-lg font-bold text-gray-900">观赛大厅</h1>
          <span className="rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-800">
            {STATUS_LABEL[state.status] ?? state.status}
          </span>
        </div>
        <div className="flex items-center gap-4 text-sm text-gray-700">
          <span>
            第 <span className="font-mono font-bold text-brand-500">{state.handNum}</span> /{' '}
            {state.handsPerMatch} 手
          </span>
          {state.matchId && (
            <span className="hidden font-mono text-xs text-gray-500 md:inline">{state.matchId}</span>
          )}
        </div>
      </header>

      {/* 手动指定 match_id */}
      {!targetMatchId && (
        <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm">
          <div className="mb-2 text-sm text-gray-700">
            {noMatch ? '当前没有进行中的对局。可粘贴 match_id 观看回放直播,或去发起对战:' : '加载中…'}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={manualMatchId}
              onChange={(e) => setManualMatchId(e.target.value)}
              placeholder="match_id(如 m-xxxx-a_vs_b)"
              className="flex-1 rounded border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
            />
            <button
              onClick={() => manualMatchId.trim() && setTargetMatchId(manualMatchId.trim())}
              disabled={!manualMatchId.trim()}
              className="rounded bg-brand-500 px-4 py-1.5 text-sm font-bold text-white hover:bg-brand-600 disabled:opacity-50"
            >
              观赛
            </button>
            <a
              href="#/challenge"
              className="rounded border border-brand-300 px-4 py-1.5 text-sm text-brand-500 hover:bg-white"
            >
              发起对战 →
            </a>
            <a
              href="#/history"
              className="rounded border border-gray-300 px-4 py-1.5 text-sm text-gray-700 hover:bg-white"
            >
              看历史
            </a>
          </div>
        </div>
      )}

      {targetMatchId && (
        <main className="grid gap-3 lg:grid-cols-[1fr_320px]">
          <section className="rounded-2xl border border-gray-200 felt-table text-white p-5">
            {renderPlayer(1, false)}
            <div className="my-4 flex flex-col items-center gap-2">
              <div className="text-xs uppercase tracking-wider text-white/70">
                {state.stage ? STAGE_LABEL[state.stage] ?? state.stage : '公共牌'}
              </div>
              <div className="flex min-h-[56px] items-center gap-1.5">
                {state.community.length === 0 ? (
                  <>
                    <CardView empty />
                    <CardView empty />
                    <CardView empty />
                    <CardView empty />
                    <CardView empty />
                  </>
                ) : (
                  state.community.map((c, i) => <CardView key={i} card={c} />)
                )}
              </div>
              <div className="flex items-center gap-4">
                <div className="text-sm text-white/70">
                  底池 <span className="font-mono text-lg font-bold text-warning-500">{state.pot.toLocaleString()}</span>
                </div>
              </div>
            </div>
            {renderPlayer(0, true)}

            {state.lastSettle && (
              <div className="mt-3 rounded-lg border border-gray-300 bg-white/80 p-3 text-sm">
                <div className="font-semibold text-gray-800">
                  第 {state.lastSettle.hand} 手结算:
                  {state.lastSettle.winnerIdx == null
                    ? '平局'
                    : `${state.names[state.lastSettle.winnerIdx] ?? `玩家${state.lastSettle.winnerIdx}`} 赢得底池`}
                  <span className="ml-2 font-mono text-brand-500">{state.lastSettle.pot.toLocaleString()}</span>
                </div>
              </div>
            )}

            {state.matchEndReason && (
              <div
                className={`mt-3 rounded-lg p-3 text-center font-semibold ${
                  state.matchEndReason === 'completed'
                    ? 'bg-success-700/50 text-success-50'
                    : 'bg-error-700/40 text-error-100'
                }`}
              >
                比赛结束 · {state.matchEndReason}
                <span className="ml-2 font-mono">
                  ({state.totalEarnings[0].toLocaleString()} / {state.totalEarnings[1].toLocaleString()})
                </span>
                <a
                  href={`#/match/${state.matchId}`}
                  className="ml-3 text-xs text-brand-500 underline hover:no-underline"
                >
                  看完整回放 →
                </a>
              </div>
            )}
          </section>

          <aside className="flex flex-col rounded-2xl border border-gray-200 bg-white/80">
            <button
              onClick={() => setShowLog((v) => !v)}
              className="flex items-center justify-between px-4 py-2 text-left text-sm font-semibold text-gray-800"
            >
              <span>动作历史 ({state.actions.length})</span>
              <span className="text-xs text-gray-500">{showLog ? '收起 ▲' : '展开 ▼'}</span>
            </button>
            {showLog && (
              <div ref={logRef} className="max-h-[60vh] flex-1 overflow-y-auto px-3 pb-3 font-mono text-xs">
                {state.actions.length === 0 ? (
                  <div className="px-2 py-4 text-center text-gray-500">暂无动作</div>
                ) : (
                  state.actions.map((a, i) => (
                    <div
                      key={i}
                      className={`border-b border-gray-200 py-1 ${a.idx === 0 ? 'text-sky-300' : 'text-fuchsia-300'}`}
                    >
                      <span className="text-gray-500">[H{a.hand} {STAGE_LABEL[a.stage] ?? a.stage}]</span>{' '}
                      {state.names[a.idx] ?? `P${a.idx}`}: {a.text}
                    </div>
                  ))
                )}
              </div>
            )}
          </aside>
        </main>
      )}
    </div>
  )
}
