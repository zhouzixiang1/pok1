import { useEffect, useState, type ReactNode } from 'react'
import { apiGet, errMsg } from '../api'

interface Row {
  bot_id: number
  bot_name: string
  bot_display?: string
  owner_name?: string
  owner_display?: string
  rating: number
  rd: number
  vol?: number
  wins: number
  losses: number
  draws: number
  net_chips: number
  matches_played: number
  is_builtin?: boolean
  last_played_at?: string
}

type Mode = 'rating' | 'chips'

export default function Leaderboard() {
  const [mode, setMode] = useState<Mode>('rating')
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    setLoading(true)
    setErr('')
    const path =
      mode === 'rating'
        ? '/api/leaderboard?limit=100'
        : '/api/leaderboard/by-chips?limit=100'
    apiGet<{ leaderboard: Row[] }>(path)
      .then((d) => setRows(d.leaderboard || []))
      .catch((e) => setErr(errMsg(e, '加载失败')))
      .finally(() => setLoading(false))
  }, [mode])

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-slate-100">排行榜</h1>
          <p className="text-sm text-slate-400">
            {mode === 'rating'
              ? 'Glicko-2 评分 · RD 为不确定度(越小越可信)'
              : '按净筹码(net chips)排序'}
          </p>
        </div>
        <div className="inline-flex overflow-hidden rounded-lg border border-slate-600">
          <button
            onClick={() => setMode('rating')}
            className={`px-3 py-1.5 text-sm transition ${
              mode === 'rating'
                ? 'bg-amber-400 font-bold text-slate-900'
                : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
            }`}
          >
            评分榜
          </button>
          <button
            onClick={() => setMode('chips')}
            className={`px-3 py-1.5 text-sm transition ${
              mode === 'chips'
                ? 'bg-amber-400 font-bold text-slate-900'
                : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
            }`}
          >
            净筹码榜
          </button>
        </div>
      </div>

      {loading ? (
        <Msg>加载天梯…</Msg>
      ) : err ? (
        <Msg>
          <span className="text-rose-400">{err}</span>
        </Msg>
      ) : rows.length === 0 ? (
        <Msg>暂无评分,跑几场对局后再来。</Msg>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-700 bg-slate-800/40">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-700 text-slate-400">
                <th className="px-3 py-2 text-left">排名</th>
                <th className="px-3 text-left">Bot</th>
                <th className="px-3 text-left">所有者</th>
                <th className="px-3 text-right">rating</th>
                <th className="px-3 text-right">RD</th>
                <th className="px-3 text-right">战绩(W-L-D)</th>
                <th className="px-3 text-right">净筹码</th>
                <th className="px-3 text-right">对局</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr
                  key={`${r.bot_id}`}
                  className="border-b border-slate-800/60 transition hover:bg-slate-800/50"
                >
                  <td className="px-3 py-2 text-slate-400">
                    <span
                      className={`font-mono font-bold ${
                        i === 0 ? 'text-amber-300' : i < 3 ? 'text-amber-400/80' : ''
                      }`}
                    >
                      {i + 1}
                    </span>
                  </td>
                  <td className="px-3">
                    <div className="flex items-center gap-1.5">
                      <span className="font-semibold text-slate-100">
                        {r.bot_display || r.bot_name}
                      </span>
                      {r.is_builtin && (
                        <span className="rounded bg-sky-500/20 px-1 text-[10px] text-sky-300">
                          内置
                        </span>
                      )}
                    </div>
                    <div className="font-mono text-xs text-slate-500">{r.bot_name}</div>
                  </td>
                  <td className="px-3 text-slate-300">{r.owner_display || r.owner_name || '—'}</td>
                  <td className="px-3 text-right font-mono font-bold text-amber-300">
                    {(r.rating ?? 0).toFixed(1)}
                  </td>
                  <td className="px-3 text-right font-mono text-slate-400">
                    {(r.rd ?? 0).toFixed(0)}
                  </td>
                  <td className="px-3 text-right font-mono">
                    <span className="text-emerald-400">{r.wins}</span>-
                    <span className="text-rose-400">{r.losses}</span>-
                    <span className="text-slate-400">{r.draws}</span>
                  </td>
                  <td
                    className={`px-3 text-right font-mono ${
                      (r.net_chips ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'
                    }`}
                  >
                    {(r.net_chips ?? 0) >= 0 ? '+' : ''}
                    {(r.net_chips ?? 0).toLocaleString()}
                  </td>
                  <td className="px-3 text-right text-slate-400">{r.matches_played}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function Msg({ children }: { children: ReactNode }) {
  return <div className="py-16 text-center text-slate-400">{children}</div>
}
