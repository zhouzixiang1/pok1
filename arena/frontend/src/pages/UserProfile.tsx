import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { apiGet } from '../api'

interface Rating {
  rating: number
  rd: number
  vol: number
  wins: number
  losses: number
  draws: number
  net_chips: number
  matches_played: number
  last_played_at: string
}
interface MatchRow {
  match_id: string
  name_a: string
  name_b: string
  earnings_a: number
  earnings_b: number
  reason: string
  hands_played: number
  started_at: string
}
interface Data {
  user: { name: string; display_name: string; team: string; note: string; active: number; created_at: string }
  rating: Rating | null
  pair_stats: { name_a: string; name_b: string; bb_per_100_mean: number; ci_low: number; ci_high: number; samples: number }[]
  recent_matches: MatchRow[]
}

export default function UserProfile() {
  const { name = '' } = useParams()
  const [d, setD] = useState<Data | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    setErr('')
    apiGet<Data>(`/api/users/${encodeURIComponent(name)}`)
      .then(setD)
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [name])

  if (loading) return <div className="p-8 text-center text-slate-400">加载…</div>
  if (err) return (
    <div className="p-8 text-center">
      <p className="mb-2 text-rose-400">{err.includes('404') ? `用户不存在: ${name}` : err}</p>
      <Link to="/leaderboard" className="text-amber-300 hover:underline">回天梯</Link>
    </div>
  )
  if (!d) return null

  const r = d.rating
  const me = (m: MatchRow) => (m.name_a === name ? m.earnings_a : m.earnings_b)
  const opp = (m: MatchRow) => (m.name_a === name ? m.name_b : m.name_a)

  return (
    <div className="mx-auto max-w-4xl p-4">
      <div className="mb-4">
        <h1 className="text-xl font-bold text-slate-100">
          {d.user.display_name} <span className="text-base text-slate-400">({d.user.name})</span>
        </h1>
        {d.user.team && <p className="text-sm text-slate-400">队伍: {d.user.team}</p>}
        {d.user.note && <p className="text-sm text-slate-400">{d.user.note}</p>}
        {!d.user.active && <span className="text-xs text-rose-400">已停用</span>}
      </div>
      {r ? (
        <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Card label="rating" value={r.rating.toFixed(1)} hl />
          <Card label="RD" value={r.rd.toFixed(0)} />
          <Card label="战绩" value={`${r.wins}-${r.losses}-${r.draws}`} />
          <Card
            label="净筹码"
            value={(r.net_chips >= 0 ? '+' : '') + r.net_chips.toLocaleString()}
            cls={r.net_chips >= 0 ? 'text-emerald-300' : 'text-rose-300'}
          />
        </div>
      ) : (
        <p className="mb-4 text-slate-400">暂无评分</p>
      )}

      {d.pair_stats.length > 0 && (
        <div className="mb-4">
          <h2 className="mb-2 font-semibold text-slate-200">对各对手 bb/100</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-700 text-slate-400">
                <th className="py-1 text-left">对手</th>
                <th className="text-right">bb/100</th>
                <th className="text-right">95% CI</th>
                <th className="text-right">n</th>
              </tr>
            </thead>
            <tbody>
              {d.pair_stats.map((p, i) => (
                <tr key={i} className="border-b border-slate-800">
                  <td className="py-1">{p.name_a === name ? p.name_b : p.name_a}</td>
                  <td className={`text-right font-mono ${p.bb_per_100_mean >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {p.bb_per_100_mean.toFixed(2)}
                  </td>
                  <td className="text-right font-mono text-slate-400">
                    [{p.ci_low.toFixed(2)}, {p.ci_high.toFixed(2)}]
                  </td>
                  <td className="text-right text-slate-400">{p.samples}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <h2 className="mb-2 font-semibold text-slate-200">最近 {d.recent_matches.length} 场</h2>
      <div className="flex flex-col gap-1">
        {d.recent_matches.map((m) => (
          <Link
            key={m.match_id}
            to={`/match/${m.match_id}`}
            className="flex items-center justify-between rounded border border-slate-800 bg-slate-800/40 px-3 py-2 text-sm hover:bg-slate-800"
          >
            <span className="truncate text-slate-300">vs {opp(m)}</span>
            <span className={`ml-2 font-mono ${me(m) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
              {me(m) >= 0 ? '+' : ''}{me(m)} ({m.reason})
            </span>
          </Link>
        ))}
      </div>
    </div>
  )
}

function Card({ label, value, hl, cls }: { label: string; value: string; hl?: boolean; cls?: string }) {
  return (
    <div className="rounded-lg border border-slate-700 bg-slate-800/60 p-3">
      <div className="text-xs text-slate-400">{label}</div>
      <div className={`font-mono text-lg font-bold ${cls || (hl ? 'text-amber-300' : 'text-slate-100')}`}>{value}</div>
    </div>
  )
}
