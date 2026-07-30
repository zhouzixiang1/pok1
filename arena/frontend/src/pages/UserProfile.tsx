import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'

interface Bot {
  id: number
  name: string
  display_name: string
  protocol: string
  current_version: number
  is_active: boolean
  is_public: boolean
  has_image: boolean
  is_builtin: boolean
  created_at: string
}

interface MatchRow {
  id?: string
  match_id?: string
  bot_a_name: string
  bot_b_name: string
  bot_a_display?: string
  bot_b_display?: string
  bot_a_id?: number
  bot_b_id?: number
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

export default function UserProfile() {
  const { name = '' } = useParams()
  const { user, loading: authLoading, refresh } = useAuth()
  const isMe = !!user && user.username === name

  const [bots, setBots] = useState<Bot[]>([])
  const [matches, setMatches] = useState<MatchRow[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  // 改密码
  const [pwd, setPwd] = useState({ old: '', new: '', confirm: '' })
  const [pwdMsg, setPwdMsg] = useState('')
  const [pwdErr, setPwdErr] = useState('')
  const [pwdBusy, setPwdBusy] = useState(false)

  const load = () => {
    if (!isMe) {
      setLoading(false)
      return
    }
    setLoading(true)
    setErr('')
    Promise.all([
      apiGet<{ bots: Bot[] }>('/api/bots?scope=mine').catch(() => ({ bots: [] })),
      apiGet<{ matches: MatchRow[]; total: number }>('/api/matches?limit=20&offset=0').catch(
        () => ({ matches: [], total: 0 }),
      ),
    ])
      .then(([a, b]) => {
        setBots(a.bots || [])
        setMatches(b.matches || [])
      })
      .catch((e) => setErr(errMsg(e, '加载失败')))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (!authLoading) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authLoading, isMe, name])

  const changePwd = async (e: React.FormEvent) => {
    e.preventDefault()
    setPwdErr('')
    setPwdMsg('')
    if (pwd.new !== pwd.confirm) {
      setPwdErr('两次新密码不一致')
      return
    }
    if (pwd.new.length < 8) {
      setPwdErr('新密码至少 8 位')
      return
    }
    setPwdBusy(true)
    try {
      const d = await apiJson<{ ok: boolean; message?: string }>(
        '/api/auth/change-password',
        'POST',
        { old_password: pwd.old, new_password: pwd.new },
      )
      setPwdMsg(d.message || '密码已修改')
      setPwd({ old: '', new: '', confirm: '' })
      void refresh()
    } catch (e) {
      setPwdErr(errMsg(e, '修改失败'))
    } finally {
      setPwdBusy(false)
    }
  }

  if (authLoading) {
    return <div className="p-8 text-center text-gray-500">加载…</div>
  }

  if (!user) {
    return (
      <div className="p-8 text-center">
        <p className="mb-3 text-gray-700">请先登录</p>
        <Link to="/login" className="text-brand-500 hover:underline">去登录 →</Link>
      </div>
    )
  }

  // 非「我」的页面:新平台无公开用户详情接口,给个友好提示
  if (!isMe) {
    return (
      <div className="mx-auto max-w-4xl p-4">
        <h1 className="mb-3 text-xl font-bold text-gray-900">@{name}</h1>
        <div className="rounded-xl border border-gray-200 bg-white p-6 text-gray-500">
          该用户档案暂不对外公开。可去
          <Link to="/leaderboard" className="mx-1 text-brand-500 hover:underline">排行榜</Link>
          查看 bot 战绩。
        </div>
      </div>
    )
  }

  const activeBots = bots.filter((b) => b.is_active).length
  const totalEarnings = matches.reduce((s, m) => {
    // 我的视角:bot_a 通常是我发起的;若 bot_b_name 是我的用户名相关则取 b
    // 这里没有 owner_id 字段简化处理,只统计我能确认是「我方」的:
    // 由于 list_matches 不直接给 owner 视角,我们用 bot_a_id/bot_b_id 是否在我的 bot 中
    const myBotIds = new Set(bots.map((b) => b.id))
    let earn = 0
    if (myBotIds.has(m.bot_a_id ?? -1)) earn += m.earnings_a
    if (myBotIds.has(m.bot_b_id ?? -1)) earn += m.earnings_b
    return s + earn
  }, 0)

  return (
    <div className="mx-auto max-w-4xl p-4">
      {/* 用户信息 */}
      <div className="mb-4 rounded-2xl border border-gray-200 bg-white p-5">
        <div className="flex items-center gap-3">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-gradient-to-br from-brand-400 to-brand-600 text-2xl font-bold text-gray-900">
            {(user.display_name || user.username).slice(0, 1).toUpperCase()}
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-xl font-bold text-gray-900">
              {user.display_name || user.username}
              {user.role === 'admin' && (
                <span className="ml-2 rounded bg-error-100 px-1.5 text-xs text-error-600">admin</span>
              )}
            </h1>
            <div className="font-mono text-sm text-gray-500">@{user.username}</div>
            <div className="text-xs text-gray-500">{user.email}</div>
          </div>
        </div>
        <div className="mt-4 grid grid-cols-3 gap-3">
          <Stat label="我的 Bot" value={`${bots.length}`} sub={`${activeBots} 上架`} />
          <Stat label="最近 20 场" value={`${matches.length}`} sub="条记录" />
          <Stat
            label="近期净筹码"
            value={`${totalEarnings >= 0 ? '+' : ''}${totalEarnings.toLocaleString()}`}
            cls={totalEarnings >= 0 ? 'text-success-600' : 'text-error-600'}
          />
        </div>
        <div className="mt-3 text-xs text-gray-500">
          注册于 {fmtTime(user.created_at)} · 最近登录 {fmtTime(user.last_login_at)}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
        {/* 左:bot + 对局 */}
        <div className="flex flex-col gap-4">
          {err && <div className="rounded bg-error-50 px-3 py-2 text-sm text-error-500">{err}</div>}

          <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="font-semibold text-gray-800">我的 Bot</h2>
              <Link to="/my-bots" className="text-xs text-brand-500 hover:underline">管理 →</Link>
            </div>
            {loading ? (
              <div className="py-3 text-center text-sm text-gray-500">加载…</div>
            ) : bots.length === 0 ? (
              <div className="py-3 text-center text-sm text-gray-500">
                还没有 bot,<Link to="/my-bots" className="text-brand-500 hover:underline">去上传 →</Link>
              </div>
            ) : (
              <div className="flex flex-col gap-1.5">
                {bots.map((b) => (
                  <div
                    key={b.id}
                    className="flex items-center justify-between rounded border border-gray-200 bg-gray-100 px-3 py-2 text-sm"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="truncate font-semibold text-gray-900">{b.display_name || b.name}</span>
                        {b.is_active ? (
                          <span className="rounded bg-success-500/20 px-1 text-[10px] text-success-600">上架</span>
                        ) : (
                          <span className="rounded bg-gray-100 px-1 text-[10px] text-gray-500">下架</span>
                        )}
                      </div>
                      <div className="font-mono text-xs text-gray-500">
                        {b.name} · {b.protocol} · v{b.current_version}
                      </div>
                    </div>
                    <Link
                      to="/challenge"
                      className="shrink-0 rounded border border-brand-300 px-2 py-0.5 text-xs text-brand-500 hover:bg-white"
                    >
                      对战
                    </Link>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="font-semibold text-gray-800">我的对局(近 20)</h2>
              <Link to="/history" className="text-xs text-brand-500 hover:underline">全部 →</Link>
            </div>
            {matches.length === 0 ? (
              <div className="py-3 text-center text-sm text-gray-500">暂无对局</div>
            ) : (
              <div className="flex flex-col gap-1.5">
                {matches.map((m) => (
                  <Link
                    key={mid(m)}
                    to={`/match/${mid(m)}`}
                    className="flex items-center justify-between rounded border border-gray-200 bg-gray-100 px-3 py-2 text-sm hover:bg-white"
                  >
                    <div className="min-w-0 flex-1 truncate">
                      <span className="text-gray-700">{m.bot_a_display || m.bot_a_name}</span>
                      <span className="mx-1 text-xs text-gray-500">vs</span>
                      <span className="text-gray-700">{m.bot_b_display || m.bot_b_name}</span>
                    </div>
                    <span className="ml-2 shrink-0 font-mono text-xs text-gray-500">
                      {fmtTime(m.started_at)}
                    </span>
                  </Link>
                ))}
              </div>
            )}
          </section>
        </div>

        {/* 右:改密码 */}
        <aside className="rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm">
          <h2 className="mb-3 font-semibold text-gray-800">修改密码</h2>
          <form onSubmit={changePwd} className="flex flex-col gap-2">
            <input
              type="password"
              value={pwd.old}
              onChange={(e) => setPwd({ ...pwd, old: e.target.value })}
              placeholder="当前密码"
              required
              className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-sm text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
            />
            <input
              type="password"
              value={pwd.new}
              onChange={(e) => setPwd({ ...pwd, new: e.target.value })}
              placeholder="新密码(至少 8 位)"
              required
              className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-sm text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
            />
            <input
              type="password"
              value={pwd.confirm}
              onChange={(e) => setPwd({ ...pwd, confirm: e.target.value })}
              placeholder="确认新密码"
              required
              className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-sm text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
            />
            {pwdErr && <div className="text-xs text-error-500">{pwdErr}</div>}
            {pwdMsg && <div className="text-xs text-success-600">{pwdMsg}</div>}
            <button
              type="submit"
              disabled={pwdBusy}
              className="rounded bg-brand-500 px-4 py-2 text-sm font-bold text-white hover:bg-brand-600 disabled:opacity-50"
            >
              {pwdBusy ? '修改中…' : '修改'}
            </button>
          </form>
          <Link
            to="/reset-password"
            className="mt-3 block text-center text-xs text-gray-500 hover:text-brand-500 hover:underline"
          >
            忘记密码?
          </Link>
        </aside>
      </div>
    </div>
  )
}

function Stat({
  label,
  value,
  sub,
  cls,
}: {
  label: string
  value: string
  sub?: string
  cls?: string
}) {
  return (
    <div className="rounded-lg border border-gray-200 bg-gray-100 p-3 text-center">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`mt-1 font-mono text-lg font-bold ${cls || 'text-brand-500'}`}>{value}</div>
      {sub && <div className="text-[10px] text-gray-500">{sub}</div>}
    </div>
  )
}
