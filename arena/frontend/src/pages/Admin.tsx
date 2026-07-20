import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'

interface MatchRow {
  id?: string
  match_id?: string
  bot_a_name: string
  bot_b_name: string
  bot_a_display?: string
  bot_b_display?: string
  owner_id?: number | null
  earnings_a: number
  earnings_b: number
  winner: number | null
  status?: string
  reason: string
  hands_played: number
  started_at: string
}
function mid(m: MatchRow): string {
  return m.id ?? m.match_id ?? ''
}
function fmtTime(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}

export default function Admin() {
  const { user, loading: authLoading } = useAuth()
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  // 生成重置 token
  const [resetInput, setResetInput] = useState('')
  const [resetResult, setResetResult] = useState<{ user?: any; token?: string } | null>(null)

  // 全局对局
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [mLoading, setMLoading] = useState(true)

  const loadMatches = () => {
    setMLoading(true)
    apiGet<{ matches: MatchRow[]; total: number }>('/api/matches?limit=30&offset=0')
      .then((d) => setMatches(d.matches || []))
      .catch((e) => setErr(errMsg(e, '加载失败')))
      .finally(() => setMLoading(false))
  }

  useEffect(() => {
    if (!authLoading && user?.role === 'admin') loadMatches()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authLoading, user])

  const genReset = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!resetInput.trim()) return
    setBusy(true)
    setErr('')
    setMsg('')
    try {
      const d = await apiJson<{ ok: boolean; token: string; user: any }>(
        '/api/auth/admin/create-reset-token',
        'POST',
        { username_or_email: resetInput.trim() },
      )
      setResetResult(d)
      setMsg('重置 token 已生成')
    } catch (e) {
      setErr(errMsg(e, '生成失败'))
    } finally {
      setBusy(false)
    }
  }

  const registerBuiltin = async () => {
    if (!window.confirm('注册内置 bot 库(national_v*)?幂等,可重复执行。')) return
    setBusy(true)
    setErr('')
    try {
      const d = await apiJson<{ count: number }>('/api/bots/register-builtin', 'POST')
      setMsg(`已注册 ${d.count} 个内置 bot`)
    } catch (e) {
      setErr(errMsg(e, '注册失败'))
    } finally {
      setBusy(false)
    }
  }

  if (authLoading) return <div className="p-8 text-center text-slate-400">加载…</div>
  if (!user) {
    return (
      <div className="p-8 text-center">
        <p className="mb-3 text-slate-300">请先登录</p>
        <Link to="/login" className="text-amber-300 hover:underline">去登录 →</Link>
      </div>
    )
  }
  if (user.role !== 'admin') {
    return (
      <div className="p-8 text-center">
        <p className="mb-3 text-rose-400">需要管理员权限</p>
        <Link to="/" className="text-amber-300 hover:underline">回首页</Link>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-5 flex items-center justify-between">
        <h1 className="text-xl font-bold text-slate-100">管理后台</h1>
        <span className="rounded bg-rose-500/30 px-2 py-1 text-xs text-rose-300">admin</span>
      </div>

      {err && (
        <div className="mb-4 rounded border border-rose-800 bg-rose-900/30 px-3 py-2 text-sm text-rose-400">
          {err}
        </div>
      )}
      {msg && (
        <div className="mb-4 rounded border border-emerald-800 bg-emerald-900/30 px-3 py-2 text-sm text-emerald-300">
          {msg}
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {/* 密码重置 token */}
        <section className="rounded-xl border border-slate-700 bg-slate-800/60 p-4">
          <h2 className="mb-3 font-semibold text-slate-200">生成密码重置 token</h2>
          <p className="mb-3 text-xs text-slate-400">
            为某用户生成一次性重置 token,转交给用户后由其在「找回密码」页使用。
          </p>
          <form onSubmit={genReset} className="flex flex-col gap-2">
            <input
              value={resetInput}
              onChange={(e) => setResetInput(e.target.value)}
              placeholder="用户名或邮箱"
              className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-amber-400 focus:outline-none"
            />
            <button
              type="submit"
              disabled={busy || !resetInput.trim()}
              className="rounded bg-amber-400 px-4 py-2 text-sm font-bold text-slate-900 hover:bg-amber-300 disabled:opacity-50"
            >
              {busy ? '生成中…' : '生成 token'}
            </button>
          </form>
          {resetResult && (
            <div className="mt-3 rounded border border-slate-600 bg-slate-900 p-3 text-sm">
              <div className="text-slate-300">
                用户:
                <span className="ml-1 font-semibold text-slate-100">
                  {resetResult.user?.username ?? resetResult.user?.display_name ?? '—'}
                </span>
              </div>
              <div className="mt-1 break-all font-mono text-xs text-amber-300">{resetResult.token}</div>
            </div>
          )}
        </section>

        {/* 内置 bot + 工具 */}
        <section className="rounded-xl border border-slate-700 bg-slate-800/60 p-4">
          <h2 className="mb-3 font-semibold text-slate-200">平台维护</h2>
          <div className="flex flex-col gap-3">
            <button
              onClick={registerBuiltin}
              disabled={busy}
              className="rounded border border-amber-500/60 px-4 py-2 text-sm font-bold text-amber-300 hover:bg-slate-700 disabled:opacity-50"
            >
              注册内置 bot 库(national_v*)
            </button>
            <Link
              to="/leaderboard"
              className="rounded border border-slate-600 px-4 py-2 text-center text-sm text-slate-300 hover:bg-slate-700"
            >
              查看排行榜 →
            </Link>
            <Link
              to="/history"
              className="rounded border border-slate-600 px-4 py-2 text-center text-sm text-slate-300 hover:bg-slate-700"
            >
              查看全部对局 →
            </Link>
          </div>
        </section>
      </div>

      {/* 全局对局 */}
      <section className="mt-4 rounded-xl border border-slate-700 bg-slate-800/60 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-semibold text-slate-200">最近对局(30)</h2>
          <button
            onClick={loadMatches}
            className="text-xs text-amber-300 hover:underline"
          >
            刷新
          </button>
        </div>
        {mLoading ? (
          <div className="py-6 text-center text-sm text-slate-500">加载…</div>
        ) : matches.length === 0 ? (
          <div className="py-6 text-center text-sm text-slate-500">暂无对局</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700 text-slate-400">
                  <th className="px-2 py-2 text-left">对局</th>
                  <th className="px-2 text-left">状态</th>
                  <th className="px-2 text-right">手数</th>
                  <th className="px-2 text-right">收益</th>
                  <th className="px-2 text-left">时间</th>
                </tr>
              </thead>
              <tbody>
                {matches.map((m) => (
                  <tr key={mid(m)} className="border-b border-slate-800/60 hover:bg-slate-800/50">
                    <td className="px-2 py-1.5">
                      <Link to={`/match/${mid(m)}`} className="text-slate-200 hover:text-amber-300 hover:underline">
                        {(m.bot_a_display || m.bot_a_name).slice(0, 14)} vs {(m.bot_b_display || m.bot_b_name).slice(0, 14)}
                      </Link>
                    </td>
                    <td className="px-2 text-slate-400">{m.status ?? '—'}</td>
                    <td className="px-2 text-right font-mono text-slate-300">{m.hands_played}</td>
                    <td className="px-2 text-right font-mono">
                      <span className={m.earnings_a >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                        {m.earnings_a >= 0 ? '+' : ''}{m.earnings_a}
                      </span>
                      <span className="mx-1 text-slate-600">/</span>
                      <span className={m.earnings_b >= 0 ? 'text-emerald-400' : 'text-rose-400'}>
                        {m.earnings_b >= 0 ? '+' : ''}{m.earnings_b}
                      </span>
                    </td>
                    <td className="px-2 text-xs text-slate-500">{fmtTime(m.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  )
}
