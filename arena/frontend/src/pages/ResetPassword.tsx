import { useState } from 'react'
import { Link } from 'react-router-dom'
import { errMsg } from '../api'

type Phase = 'request' | 'reset' | 'done'

export default function ResetPassword() {
  const [phase, setPhase] = useState<Phase>('request')
  const [emailOrUsername, setEmailOrUsername] = useState('')
  const [token, setToken] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [confirmPwd, setConfirmPwd] = useState('')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)

  const requestReset = async (e: React.FormEvent) => {
    e.preventDefault()
    setErr('')
    setMsg('')
    setLoading(true)
    try {
      const res = await fetch('/api/auth/request-reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ email_or_username: emailOrUsername.trim() }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `${res.status}`)
      if (data.token) {
        // 开发态:后端直接返 token
        setToken(data.token)
        setMsg(
          `重置 token 已生成(本平台无邮件服务,直接显示): ${data.token}。请在下一步设置新密码。`,
        )
        setPhase('reset')
      } else {
        setMsg(data.message || '若账号存在,重置链接已生成')
        setPhase('reset')
      }
    } catch (e) {
      setErr(errMsg(e, '请求失败'))
    } finally {
      setLoading(false)
    }
  }

  const doReset = async (e: React.FormEvent) => {
    e.preventDefault()
    setErr('')
    setMsg('')
    if (newPwd !== confirmPwd) {
      setErr('两次密码不一致')
      return
    }
    if (newPwd.length < 8) {
      setErr('密码至少 8 位')
      return
    }
    setLoading(true)
    try {
      const res = await fetch('/api/auth/reset-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ token: token.trim(), new_password: newPwd }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `${res.status}`)
      setMsg(data.message || '密码已重置')
      setPhase('done')
    } catch (e) {
      setErr(errMsg(e, '重置失败'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-md p-4 pt-10">
      <h1 className="mb-1 text-xl font-bold text-slate-100">找回密码</h1>
      <p className="mb-6 text-sm text-slate-400">
        本平台未配置邮件服务,重置 token 会直接显示。
      </p>

      {phase === 'request' && (
        <form
          onSubmit={requestReset}
          className="flex flex-col gap-3 rounded-xl border border-slate-700 bg-slate-800/60 p-5"
        >
          <label className="flex flex-col gap-1 text-sm text-slate-300">
            用户名或邮箱
            <input
              value={emailOrUsername}
              onChange={(e) => setEmailOrUsername(e.target.value)}
              placeholder="username 或 you@example.com"
              required
              className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100 placeholder:text-slate-500 focus:border-amber-400 focus:outline-none"
            />
          </label>
          {err && <div className="rounded bg-rose-900/30 px-3 py-2 text-sm text-rose-400">{err}</div>}
          {msg && <div className="rounded bg-emerald-900/30 px-3 py-2 text-sm text-emerald-300">{msg}</div>}
          <button
            type="submit"
            disabled={loading || !emailOrUsername.trim()}
            className="rounded bg-amber-400 px-4 py-2 font-bold text-slate-900 transition hover:bg-amber-300 disabled:opacity-50"
          >
            {loading ? '提交中…' : '申请重置'}
          </button>
        </form>
      )}

      {(phase === 'reset' || phase === 'done') && (
        <form
          onSubmit={doReset}
          className="flex flex-col gap-3 rounded-xl border border-slate-700 bg-slate-800/60 p-5"
        >
          <label className="flex flex-col gap-1 text-sm text-slate-300">
            重置 token
            <input
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="粘贴 token"
              required
              className="rounded border border-slate-600 bg-slate-900 px-3 py-2 font-mono text-slate-100 placeholder:text-slate-500 focus:border-amber-400 focus:outline-none"
            />
          </label>
          {msg && <div className="rounded bg-emerald-900/30 px-3 py-2 text-sm text-emerald-300">{msg}</div>}
          {phase === 'reset' && (
            <>
              <label className="flex flex-col gap-1 text-sm text-slate-300">
                新密码(至少 8 位)
                <input
                  type="password"
                  value={newPwd}
                  onChange={(e) => setNewPwd(e.target.value)}
                  required
                  className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100 focus:border-amber-400 focus:outline-none"
                />
              </label>
              <label className="flex flex-col gap-1 text-sm text-slate-300">
                确认新密码
                <input
                  type="password"
                  value={confirmPwd}
                  onChange={(e) => setConfirmPwd(e.target.value)}
                  required
                  className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100 focus:border-amber-400 focus:outline-none"
                />
              </label>
              {err && (
                <div className="rounded bg-rose-900/30 px-3 py-2 text-sm text-rose-400">{err}</div>
              )}
              <button
                type="submit"
                disabled={loading || !token.trim() || !newPwd}
                className="rounded bg-amber-400 px-4 py-2 font-bold text-slate-900 transition hover:bg-amber-300 disabled:opacity-50"
              >
                {loading ? '重置中…' : '设置新密码'}
              </button>
            </>
          )}
          {phase === 'done' && (
            <Link
              to="/login"
              className="rounded border border-amber-400/60 px-4 py-2 text-center font-bold text-amber-300 hover:bg-amber-400/10"
            >
              → 去登录
            </Link>
          )}
        </form>
      )}

      <p className="mt-4 text-center text-sm text-slate-400">
        <Link to="/login" className="text-amber-300 hover:underline">
          ← 返回登录
        </Link>
      </p>
    </div>
  )
}
