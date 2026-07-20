import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../components/useAuth'
import { ApiError, errMsg } from '../api'

export default function Login() {
  const nav = useNavigate()
  const [sp] = useSearchParams()
  const from = sp.get('from') || '/'
  const { login } = useAuth()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setErr('')
    try {
      await login(username.trim(), password)
      nav(from)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setErr('用户名或密码错误')
      } else {
        setErr(errMsg(e, '登录失败'))
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-sm p-4 pt-12">
      <h1 className="mb-1 text-xl font-bold text-slate-100">登录</h1>
      <p className="mb-6 text-sm text-slate-400">用平台账号登录后才能对战 / 看排行</p>
      <form
        onSubmit={submit}
        className="flex flex-col gap-3 rounded-xl border border-slate-700 bg-slate-800/60 p-5"
      >
        <label className="flex flex-col gap-1 text-sm text-slate-300">
          用户名
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="username"
            autoFocus
            required
            className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100 placeholder:text-slate-500 focus:border-amber-400 focus:outline-none"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm text-slate-300">
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            required
            className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100 placeholder:text-slate-500 focus:border-amber-400 focus:outline-none"
          />
        </label>
        {err && <div className="rounded bg-rose-900/30 px-3 py-2 text-sm text-rose-400">{err}</div>}
        <button
          type="submit"
          disabled={loading || !username.trim() || !password}
          className="rounded bg-amber-400 px-4 py-2 font-bold text-slate-900 transition hover:bg-amber-300 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? '登录中…' : '登录'}
        </button>
      </form>
      <div className="mt-4 flex items-center justify-between text-sm text-slate-400">
        <Link to="/register" className="text-amber-300 hover:underline">
          没有账号?去注册 →
        </Link>
        <Link to="/reset-password" className="text-slate-400 hover:text-amber-300 hover:underline">
          忘记密码?
        </Link>
      </div>
    </div>
  )
}
