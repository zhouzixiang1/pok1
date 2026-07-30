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

interface EmailTemplate {
  key: string
  subject: string
  body_html: string
  body_text: string
  updated_at?: string
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

  const [resetInput, setResetInput] = useState('')
  const [resetResult, setResetResult] = useState<{ user?: any; token?: string } | null>(null)

  const [matches, setMatches] = useState<MatchRow[]>([])
  const [mLoading, setMLoading] = useState(true)

  const [templates, setTemplates] = useState<EmailTemplate[]>([])
  const [tplKey, setTplKey] = useState('')
  const [tplSubject, setTplSubject] = useState('')
  const [tplHtml, setTplHtml] = useState('')
  const [tplText, setTplText] = useState('')
  const [testTo, setTestTo] = useState('')
  const [outbox, setOutbox] = useState<any[]>([])

  const loadMatches = () => {
    setMLoading(true)
    apiGet<{ matches: MatchRow[]; total: number }>('/api/matches?limit=30&offset=0')
      .then((d) => setMatches(d.matches || []))
      .catch((e) => setErr(errMsg(e, '加载失败')))
      .finally(() => setMLoading(false))
  }

  const loadTemplates = () => {
    apiGet<{ templates: EmailTemplate[] }>('/api/admin/email-templates')
      .then((d) => {
        const list = d.templates || []
        setTemplates(list)
        if (list.length && !tplKey) {
          const first = list[0]
          setTplKey(first.key)
          setTplSubject(first.subject)
          setTplHtml(first.body_html)
          setTplText(first.body_text)
        }
      })
      .catch((e) => setErr(errMsg(e, '加载邮件模板失败')))
  }

  const loadOutbox = () => {
    apiGet<{ items: any[] }>('/api/admin/email-outbox?limit=20')
      .then((d) => setOutbox(d.items || []))
      .catch(() => {/* 忽略 */})
  }

  useEffect(() => {
    if (!authLoading && user?.role === 'admin') {
      loadMatches()
      loadTemplates()
      loadOutbox()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authLoading, user])

  const selectTpl = (key: string) => {
    const t = templates.find((x) => x.key === key)
    if (!t) return
    setTplKey(t.key)
    setTplSubject(t.subject)
    setTplHtml(t.body_html)
    setTplText(t.body_text)
  }

  const saveTpl = async () => {
    if (!tplKey) return
    setBusy(true)
    setErr('')
    setMsg('')
    try {
      const d = await apiJson<{ template: EmailTemplate }>(
        `/api/admin/email-templates/${encodeURIComponent(tplKey)}`,
        'PUT',
        { subject: tplSubject, body_html: tplHtml, body_text: tplText },
      )
      setMsg(`模板 ${tplKey} 已保存`)
      setTemplates((prev) =>
        prev.map((t) => (t.key === tplKey ? d.template : t)),
      )
    } catch (e) {
      setErr(errMsg(e, '保存失败'))
    } finally {
      setBusy(false)
    }
  }

  const testSend = async () => {
    if (!tplKey || !testTo.trim()) return
    setBusy(true)
    setErr('')
    setMsg('')
    try {
      const d = await apiJson<{ ok: boolean; message: string }>(
        `/api/admin/email-templates/${encodeURIComponent(tplKey)}/test-send`,
        'POST',
        { to: testTo.trim() },
      )
      setMsg(d.message || '试发成功')
      loadOutbox()
    } catch (e) {
      setErr(errMsg(e, '试发失败'))
    } finally {
      setBusy(false)
    }
  }

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
      setMsg('重置 token 已生成(用户也可走邮箱验证码流程)')
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

  if (authLoading) return <div className="p-8 text-center text-gray-500">加载…</div>
  if (!user) {
    return (
      <div className="p-8 text-center">
        <p className="mb-3 text-gray-700">请先登录</p>
        <Link to="/login" className="text-brand-500 hover:underline">去登录 →</Link>
      </div>
    )
  }
  if (user.role !== 'admin') {
    return (
      <div className="p-8 text-center">
        <p className="mb-3 text-error-500">需要管理员权限</p>
        <Link to="/" className="text-brand-500 hover:underline">回首页</Link>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-5 flex items-center justify-between">
        <h1 className="text-xl font-bold text-gray-900">管理后台</h1>
        <span className="rounded bg-error-100 px-2 py-1 text-xs text-error-600">admin</span>
      </div>

      {err && (
        <div className="mb-4 rounded border border-error-200 bg-error-50 px-3 py-2 text-sm text-error-500">
          {err}
        </div>
      )}
      {msg && (
        <div className="mb-4 rounded border border-success-200 bg-success-50 px-3 py-2 text-sm text-success-600">
          {msg}
        </div>
      )}

      {/* 邮件内容管理 */}
      <section className="mb-4 rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm">
        <h2 className="mb-3 font-semibold text-gray-800">邮件内容管理</h2>
        <p className="mb-3 text-xs text-gray-500">
          占位符: <code className="text-brand-500">{'{{username}}'}</code>{' '}
          <code className="text-brand-500">{'{{code}}'}</code>{' '}
          <code className="text-brand-500">{'{{expires_minutes}}'}</code>
        </p>
        <div className="mb-3 flex flex-wrap gap-2">
          {templates.map((t) => (
            <button
              key={t.key}
              type="button"
              onClick={() => selectTpl(t.key)}
              className={`rounded px-3 py-1 text-xs ${
                tplKey === t.key
                  ? 'bg-brand-500 font-bold text-white'
                  : 'border border-gray-300 text-gray-700 hover:bg-gray-100'
              }`}
            >
              {t.key}
            </button>
          ))}
        </div>
        {tplKey && (
          <div className="flex flex-col gap-2">
            <label className="text-sm text-gray-700">
              主题
              <input
                value={tplSubject}
                onChange={(e) => setTplSubject(e.target.value)}
                className="mt-1 w-full rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-sm text-gray-900 focus:border-brand-300 focus:outline-none"
              />
            </label>
            <label className="text-sm text-gray-700">
              HTML 正文
              <textarea
                value={tplHtml}
                onChange={(e) => setTplHtml(e.target.value)}
                rows={5}
                className="mt-1 w-full rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 font-mono text-xs text-gray-900 focus:border-brand-300 focus:outline-none"
              />
            </label>
            <label className="text-sm text-gray-700">
              纯文本正文
              <textarea
                value={tplText}
                onChange={(e) => setTplText(e.target.value)}
                rows={3}
                className="mt-1 w-full rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 font-mono text-xs text-gray-900 focus:border-brand-300 focus:outline-none"
              />
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => void saveTpl()}
                disabled={busy}
                className="rounded bg-brand-500 px-4 py-2 text-sm font-bold text-white hover:bg-brand-600 disabled:opacity-50"
              >
                保存模板
              </button>
              <input
                value={testTo}
                onChange={(e) => setTestTo(e.target.value)}
                placeholder="试发邮箱"
                className="min-w-[12rem] flex-1 rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-sm text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
              />
              <button
                type="button"
                onClick={() => void testSend()}
                disabled={busy || !testTo.trim()}
                className="rounded border border-brand-300 px-4 py-2 text-sm text-brand-500 hover:bg-gray-100 disabled:opacity-50"
              >
                试发
              </button>
            </div>
          </div>
        )}
        {outbox.length > 0 && (
          <div className="mt-4 overflow-x-auto">
            <h3 className="mb-2 text-sm font-semibold text-gray-700">最近发信</h3>
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-gray-200 text-gray-500">
                  <th className="px-2 py-1 text-left">时间</th>
                  <th className="px-2 text-left">收件人</th>
                  <th className="px-2 text-left">模板</th>
                  <th className="px-2 text-left">状态</th>
                </tr>
              </thead>
              <tbody>
                {outbox.map((o) => (
                  <tr key={o.id} className="border-b border-gray-100">
                    <td className="px-2 py-1 text-gray-500">{fmtTime(o.created_at)}</td>
                    <td className="px-2 text-gray-700">{o.to_addr}</td>
                    <td className="px-2 text-gray-500">{o.template_key || '—'}</td>
                    <td className={`px-2 ${o.status === 'sent' ? 'text-success-500' : 'text-error-500'}`}>
                      {o.status}
                      {o.error ? `: ${String(o.error).slice(0, 40)}` : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm">
          <h2 className="mb-3 font-semibold text-gray-800">生成密码重置 token(兜底)</h2>
          <p className="mb-3 text-xs text-gray-500">
            正常用户走邮箱验证码;此功能供管理员线下转交。
          </p>
          <form onSubmit={genReset} className="flex flex-col gap-2">
            <input
              value={resetInput}
              onChange={(e) => setResetInput(e.target.value)}
              placeholder="用户名或邮箱"
              className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-sm text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
            />
            <button
              type="submit"
              disabled={busy || !resetInput.trim()}
              className="rounded bg-brand-500 px-4 py-2 text-sm font-bold text-white hover:bg-brand-600 disabled:opacity-50"
            >
              {busy ? '生成中…' : '生成 token'}
            </button>
          </form>
          {resetResult && (
            <div className="mt-3 rounded border border-gray-300 bg-white p-3 text-sm">
              <div className="text-gray-700">
                用户:
                <span className="ml-1 font-semibold text-gray-900">
                  {resetResult.user?.username ?? resetResult.user?.display_name ?? '—'}
                </span>
              </div>
              <div className="mt-1 break-all font-mono text-xs text-brand-500">{resetResult.token}</div>
            </div>
          )}
        </section>

        <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm">
          <h2 className="mb-3 font-semibold text-gray-800">平台维护</h2>
          <div className="flex flex-col gap-3">
            <button
              onClick={registerBuiltin}
              disabled={busy}
              className="rounded border border-brand-300 px-4 py-2 text-sm font-bold text-brand-500 hover:bg-gray-100 disabled:opacity-50"
            >
              注册内置 bot 库(national_v*)
            </button>
            <Link
              to="/leaderboard"
              className="rounded border border-gray-300 px-4 py-2 text-center text-sm text-gray-700 hover:bg-gray-100"
            >
              查看排行榜 →
            </Link>
            <Link
              to="/history"
              className="rounded border border-gray-300 px-4 py-2 text-center text-sm text-gray-700 hover:bg-gray-100"
            >
              查看全部对局 →
            </Link>
          </div>
        </section>
      </div>

      <section className="mt-4 rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-semibold text-gray-800">最近对局(30)</h2>
          <button
            onClick={loadMatches}
            className="text-xs text-brand-500 hover:underline"
          >
            刷新
          </button>
        </div>
        {mLoading ? (
          <div className="py-6 text-center text-sm text-gray-500">加载…</div>
        ) : matches.length === 0 ? (
          <div className="py-6 text-center text-sm text-gray-500">暂无对局</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 text-gray-500">
                  <th className="px-2 py-2 text-left">对局</th>
                  <th className="px-2 text-left">状态</th>
                  <th className="px-2 text-right">手数</th>
                  <th className="px-2 text-right">收益</th>
                  <th className="px-2 text-left">时间</th>
                </tr>
              </thead>
              <tbody>
                {matches.map((m) => (
                  <tr key={mid(m)} className="border-b border-gray-100 hover:bg-gray-50">
                    <td className="px-2 py-1.5">
                      <Link to={`/match/${mid(m)}`} className="text-gray-800 hover:text-brand-500 hover:underline">
                        {(m.bot_a_display || m.bot_a_name).slice(0, 14)} vs {(m.bot_b_display || m.bot_b_name).slice(0, 14)}
                      </Link>
                    </td>
                    <td className="px-2 text-gray-500">{m.status ?? '—'}</td>
                    <td className="px-2 text-right font-mono text-gray-700">{m.hands_played}</td>
                    <td className="px-2 text-right font-mono">
                      <span className={m.earnings_a >= 0 ? 'text-success-500' : 'text-error-500'}>
                        {m.earnings_a >= 0 ? '+' : ''}{m.earnings_a}
                      </span>
                      <span className="mx-1 text-gray-400">/</span>
                      <span className={m.earnings_b >= 0 ? 'text-success-500' : 'text-error-500'}>
                        {m.earnings_b >= 0 ? '+' : ''}{m.earnings_b}
                      </span>
                    </td>
                    <td className="px-2 text-xs text-gray-500">{fmtTime(m.started_at)}</td>
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
