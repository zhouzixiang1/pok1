import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { apiGet } from '../api'

interface MatchMeta {
  match_id: string
  name_a: string
  name_b: string
  hands_played: number
  earnings_a: number
  earnings_b: number
  winner: number | null
  reason: string
  net_bb_a?: number
  started_at: string
  ended_at: string
}
interface MatchEvent {
  type: string
  hand?: number
  stage?: string
  player_idx?: number
  action?: string
  amount?: number
  pot?: number
  player_chips?: number[]
  winner_idx?: number | null
  reason?: string
  [k: string]: any
}
interface MatchDetailData {
  match: MatchMeta | null
  thp: string | null
  events: MatchEvent[]
}

const STAGE_LABEL: Record<string, string> = {
  preflop: '翻前',
  flop: '翻牌',
  turn: '转牌',
  river: '河牌',
  showdown: '摊牌',
}

// 管道/SSE 类事件不属于对局回放,从时间线过滤掉。
const SKIP_TYPES = new Set(['action_requested', 'connected', 'server_started', 'snapshot'])

function fmtTime(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

function earnStr(n: number): string {
  return (n >= 0 ? '+' : '') + n.toLocaleString()
}

function actionLabel(ev: MatchEvent): string {
  const a = ev.action
  switch (a) {
    case 'fold':
      return '弃牌'
    case 'check':
      return '过牌'
    case 'call':
      return '跟注'
    case 'raise':
      return `加注到 ${ev.amount ?? 0}`
    case 'allin':
      return `全押 ${ev.amount ?? 0}`
    case 'timeout':
      return '超时弃牌'
    default:
      if (typeof a === 'string' && a.startsWith('illegal:')) return '非法弃牌'
      return String(a ?? '')
  }
}

export default function MatchDetail() {
  const { id } = useParams()
  const [data, setData] = useState<MatchDetailData | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    setErr('')
    apiGet<MatchDetailData>(`/api/matches/${encodeURIComponent(id ?? '')}`)
      .then(setData)
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [id])

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl p-4">
        <div className="py-20 text-center text-slate-400">加载对局…</div>
      </div>
    )
  }
  if (err || !data) {
    const notFound = err.includes('404')
    return (
      <div className="mx-auto max-w-4xl p-4">
        <div className="py-20 text-center">
          <p className="mb-3 text-rose-400">
            {notFound ? `对局不存在: ${id}` : `加载失败: ${err}`}
          </p>
          <Link to="/history" className="text-amber-300 hover:underline">
            ← 返回历史对局
          </Link>
        </div>
      </div>
    )
  }

  const m = data.match
  const events = (data.events || []).filter((ev) => !SKIP_TYPES.has(ev.type))
  const thp = data.thp

  const pname = (idx?: number): string => {
    if (idx === 0) return m?.name_a || 'P0'
    if (idx === 1) return m?.name_b || 'P1'
    return idx == null ? '?' : `P${idx}`
  }
  const infoText = (ev: MatchEvent): string => {
    switch (ev.type) {
      case 'hand_start':
        return `第 ${ev.hand ?? '?'} 手 · 开局`
      case 'settle': {
        const w = ev.winner_idx
        const who = w == null ? '平局' : `${pname(w)} 赢`
        return `第 ${ev.hand ?? '?'} 手 · 结算 · ${who}${
          ev.pot != null ? ` · 底池 ${ev.pot.toLocaleString()}` : ''
        }`
      }
      case 'stage':
        return `▷ ${STAGE_LABEL[ev.stage ?? ''] ?? ev.stage ?? ''}`
      case 'cards_dealt':
        return '发底牌'
      case 'match_start':
        return '比赛开始'
      case 'match_end':
        return `比赛结束${ev.reason ? ` · ${ev.reason}` : ''}`
      default:
        return ev.type
    }
  }

  return (
    <div className="mx-auto max-w-4xl p-4">
      <div className="mb-3">
        <Link to="/history" className="text-sm text-slate-400 hover:text-amber-300 hover:underline">
          ← 历史对局
        </Link>
      </div>

      {/* 顶部元数据卡 */}
      <div className="rounded-2xl border border-slate-700 bg-slate-800/60 p-5">
        {m ? (
          <>
            <div className="break-all font-mono text-xs text-slate-500">{m.match_id}</div>
            <div className="mt-3 grid grid-cols-[1fr_auto_1fr] items-center gap-3">
              <div className="text-center">
                <div
                  className={`flex items-center justify-center gap-1 truncate text-lg font-bold ${
                    m.winner === 0 ? 'text-amber-300' : 'text-slate-100'
                  }`}
                >
                  <span className="truncate">{m.name_a}</span>
                  {m.winner === 0 && <span className="shrink-0 text-xs">胜</span>}
                </div>
                <div
                  className={`mt-1 font-mono text-2xl font-bold ${
                    m.earnings_a >= 0 ? 'text-emerald-400' : 'text-rose-400'
                  }`}
                >
                  {earnStr(m.earnings_a)}
                </div>
              </div>
              <div className="text-sm text-slate-500">vs</div>
              <div className="text-center">
                <div
                  className={`flex items-center justify-center gap-1 truncate text-lg font-bold ${
                    m.winner === 1 ? 'text-amber-300' : 'text-slate-100'
                  }`}
                >
                  {m.winner === 1 && <span className="shrink-0 text-xs">胜</span>}
                  <span className="truncate">{m.name_b}</span>
                </div>
                <div
                  className={`mt-1 font-mono text-2xl font-bold ${
                    m.earnings_b >= 0 ? 'text-emerald-400' : 'text-rose-400'
                  }`}
                >
                  {earnStr(m.earnings_b)}
                </div>
              </div>
            </div>
            <div className="mt-4 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 text-sm text-slate-400">
              <span>
                <span className="font-mono text-slate-200">
                  {(m.hands_played ?? 0).toLocaleString()}
                </span>{' '}
                手
              </span>
              {m.reason && (
                <>
                  <span className="text-slate-600">·</span>
                  <span>{m.reason}</span>
                </>
              )}
              {m.winner == null && (
                <>
                  <span className="text-slate-600">·</span>
                  <span>未分胜负</span>
                </>
              )}
              {typeof m.net_bb_a === 'number' && Number.isFinite(m.net_bb_a) && (
                <>
                  <span className="text-slate-600">·</span>
                  <span
                    className={`font-mono ${m.net_bb_a >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}
                  >
                    {(m.net_bb_a >= 0 ? '+' : '') + m.net_bb_a.toFixed(2)} bb/100
                  </span>
                </>
              )}
            </div>
            <div className="mt-1 text-center text-xs text-slate-500">
              {fmtTime(m.started_at)} ~ {fmtTime(m.ended_at)}
            </div>
          </>
        ) : (
          <div className="py-4 text-center text-slate-400">
            <div className="break-all font-mono text-xs text-slate-500">{id}</div>
            <p className="mt-2 text-sm">无元数据(对局可能未写入数据库),仅展示棋谱与事件。</p>
          </div>
        )}
      </div>

      {/* 事件时间线 */}
      <section className="mt-4 rounded-2xl border border-slate-700 bg-slate-800/60 p-4">
        <h2 className="mb-3 font-semibold text-slate-200">
          事件时间线 <span className="text-slate-500">({events.length})</span>
        </h2>
        {events.length === 0 ? (
          <p className="py-8 text-center text-slate-500">暂无事件记录</p>
        ) : (
          <div className="max-h-[70vh] overflow-y-auto font-mono text-xs">
            {events.map((ev, i) => {
              if (ev.type === 'action' && ev.player_idx != null && ev.action != null) {
                const chips = Array.isArray(ev.player_chips)
                  ? ev.player_chips[ev.player_idx]
                  : undefined
                return (
                  <div
                    key={i}
                    className="flex flex-wrap items-baseline gap-x-2 border-b border-slate-800/50 py-1"
                  >
                    <span className="text-slate-500">
                      [H{ev.hand ?? '?'} {STAGE_LABEL[ev.stage ?? ''] ?? ev.stage ?? ''}]
                    </span>
                    <span className={ev.player_idx === 0 ? 'text-sky-300' : 'text-fuchsia-300'}>
                      {pname(ev.player_idx)}
                    </span>
                    <span className="text-slate-100">{actionLabel(ev)}</span>
                    {ev.pot != null && (
                      <span className="text-slate-500">底池 {ev.pot.toLocaleString()}</span>
                    )}
                    {chips != null && (
                      <span className="text-slate-500">筹码 {chips.toLocaleString()}</span>
                    )}
                  </div>
                )
              }
              return (
                <div
                  key={i}
                  className="border-b border-slate-800/50 py-1 text-slate-500"
                >
                  <span className="text-slate-600">[{ev.hand != null ? `H${ev.hand}` : '·'}]</span>{' '}
                  {infoText(ev)}
                </div>
              )
            })}
          </div>
        )}
      </section>

      {/* THP 棋谱 */}
      {thp && (
        <section className="mt-4 rounded-2xl border border-slate-700 bg-slate-800/60 p-4">
          <h2 className="mb-3 font-semibold text-slate-200">THP 棋谱</h2>
          <pre className="overflow-x-auto rounded bg-slate-950/60 p-3 font-mono text-xs text-slate-300">
            {thp}
          </pre>
        </section>
      )}
    </div>
  )
}
