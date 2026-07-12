import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet } from '../api'

interface MatchRow {
  match_id: string
  name_a: string
  name_b: string
  earnings_a: number
  earnings_b: number
  winner: number | null
  reason: string
  hands_played: number
  started_at: string
  ended_at: string
}

interface Data {
  matches: MatchRow[]
  total: number
}

function fmtTime(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

function earningsStr(n: number): string {
  return (n >= 0 ? '+' : '') + n.toLocaleString()
}

export default function History() {
  const [userInput, setUserInput] = useState('')
  const [user, setUser] = useState('')
  const [limit] = useState(30)
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<Data | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    setLoading(true)
    setErr('')
    const q = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    if (user) q.set('user', user)
    apiGet<Data>(`/api/matches?${q.toString()}`)
      .then(setData)
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [user, limit, offset])

  const onSearch = () => {
    setOffset(0)
    setUser(userInput.trim())
  }

  const matches = data?.matches || []
  const total = data?.total ?? 0
  const from = total > 0 ? offset + 1 : 0
  const to = Math.min(offset + limit, total)
  const hasPrev = offset > 0
  const hasNext = offset + limit < total

  return (
    <div className="mx-auto max-w-4xl p-4">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-slate-100">历史对局</h1>
          <p className="text-sm text-slate-400">
            共 <span className="font-mono text-amber-300">{total.toLocaleString()}</span> 场
          </p>
        </div>
        <div className="flex gap-2">
          <input
            value={userInput}
            onChange={(e) => setUserInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') onSearch()
            }}
            placeholder="按用户名筛选"
            className="w-48 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 placeholder-slate-500 outline-none focus:border-amber-400"
          />
          <button
            onClick={onSearch}
            className="rounded bg-amber-400 px-4 py-1.5 text-sm font-bold text-slate-900 hover:bg-amber-300"
          >
            查询
          </button>
        </div>
      </div>

      {loading ? (
        <div className="py-12 text-center text-slate-400">加载…</div>
      ) : err ? (
        <div className="py-12 text-center text-rose-400">加载失败: {err}</div>
      ) : matches.length === 0 ? (
        <div className="py-12 text-center text-slate-400">暂无对局</div>
      ) : (
        <div className="flex flex-col gap-2">
          {matches.map((m) => {
            const aWin = m.winner === 0
            const bWin = m.winner === 1
            return (
              <Link
                key={m.match_id}
                to={`/match/${m.match_id}`}
                className="block rounded-xl border border-slate-700 bg-slate-800/60 p-4 transition hover:border-amber-400/60 hover:bg-slate-800"
              >
                <div className="flex items-center justify-between gap-2">
                  <span
                    className={`min-w-0 flex-1 truncate font-semibold ${aWin ? 'text-amber-300' : 'text-slate-100'}`}
                  >
                    {m.name_a}
                    {aWin && <span className="ml-1 text-xs">胜</span>}
                  </span>
                  <span className="shrink-0 font-mono text-xs text-slate-500">vs</span>
                  <span
                    className={`min-w-0 flex-1 truncate text-right font-semibold ${bWin ? 'text-amber-300' : 'text-slate-100'}`}
                  >
                    {bWin && <span className="mr-1 text-xs">胜</span>}
                    {m.name_b}
                  </span>
                </div>
                <div className="mt-2 flex items-center justify-between gap-2 text-sm">
                  <span className={`font-mono ${m.earnings_a >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {earningsStr(m.earnings_a)}
                  </span>
                  <span className={`font-mono ${m.earnings_b >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {earningsStr(m.earnings_b)}
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-400">
                  <span className="font-mono">{(m.hands_played ?? 0).toLocaleString()} 手</span>
                  {m.reason && (
                    <>
                      <span>·</span>
                      <span>{m.reason}</span>
                    </>
                  )}
                  <span>·</span>
                  <span>{fmtTime(m.started_at)}</span>
                </div>
              </Link>
            )
          })}
        </div>
      )}

      {!loading && !err && total > 0 && (
        <div className="mt-4 flex items-center justify-between gap-2 text-sm">
          <button
            onClick={() => setOffset((o) => Math.max(0, o - limit))}
            disabled={!hasPrev}
            className="rounded border border-slate-700 px-3 py-1 text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
          >
            上一页
          </button>
          <span className="text-slate-400">
            <span className="font-mono text-slate-100">{from.toLocaleString()}</span> ~{' '}
            <span className="font-mono text-slate-100">{to.toLocaleString()}</span> /{' '}
            <span className="font-mono text-amber-300">{total.toLocaleString()}</span>
          </span>
          <button
            onClick={() => setOffset((o) => Math.min(total - 1, o + limit))}
            disabled={!hasNext}
            className="rounded border border-slate-700 px-3 py-1 text-slate-200 hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      )}
    </div>
  )
}
