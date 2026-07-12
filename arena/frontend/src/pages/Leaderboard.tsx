import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { apiGet } from '../api'

interface Row {
  name: string
  display_name: string
  team: string
  rating: number
  rd: number
  wins: number
  losses: number
  draws: number
  net_chips: number
  matches_played: number
}

export default function Leaderboard() {
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  useEffect(() => {
    apiGet<{ leaderboard: Row[] }>('/api/leaderboard?limit=50')
      .then(d => setRows(d.leaderboard || []))
      .catch(e => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [])
  if (loading) return <Msg>加载天梯…</Msg>
  if (err) return <Msg>加载失败: {err}</Msg>
  if (!rows.length) return <Msg>暂无评分,先 serve 跑几场对局。</Msg>
  return (
    <div className="mx-auto max-w-5xl p-4">
      <h1 className="mb-1 text-xl font-bold text-slate-100">天梯榜</h1>
      <p className="mb-4 text-sm text-slate-400">Glicko-2 评分 · RD 为不确定度(越小越可信)</p>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-700 text-slate-400">
            <th className="py-2 text-left">排名</th>
            <th className="text-left">bot</th>
            <th className="text-right">rating</th>
            <th className="text-right">RD</th>
            <th className="text-right">战绩</th>
            <th className="text-right">净筹码</th>
            <th className="text-right">对局</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.name} className="border-b border-slate-800 hover:bg-slate-800/50">
              <td className="py-2 text-slate-400">{i + 1}</td>
              <td>
                <Link to={`/user/${r.name}`} className="text-slate-100 hover:underline">
                  {r.display_name}
                  {r.team && <span className="ml-1 text-xs text-slate-500">{r.team}</span>}
                </Link>
              </td>
              <td className="text-right font-mono font-bold text-amber-300">{r.rating.toFixed(1)}</td>
              <td className="text-right font-mono text-slate-400">{r.rd.toFixed(0)}</td>
              <td className="text-right font-mono">{r.wins}-{r.losses}-{r.draws}</td>
              <td className={`text-right font-mono ${r.net_chips >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {r.net_chips >= 0 ? '+' : ''}{r.net_chips.toLocaleString()}
              </td>
              <td className="text-right text-slate-400">{r.matches_played}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Msg({ children }: { children: ReactNode }) {
  return <div className="mx-auto max-w-5xl p-8 text-center text-slate-400">{children}</div>
}
