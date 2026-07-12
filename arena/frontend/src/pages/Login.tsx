import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { adminToken, apiJson } from '../api'

export default function Login() {
  const nav = useNavigate()
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setErr('')
    try {
      const d = await apiJson<{ token: string }>('/api/admin/login', 'POST', { username, password })
      adminToken.set(d.token)
      nav('/admin')
    } catch {
      setErr('用户名或密码错误')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-sm p-4 pt-12">
      <h1 className="mb-1 text-xl font-bold text-slate-100">管理员登录</h1>
      <p className="mb-6 text-sm text-slate-400">用于 /admin 管理 bot 用户</p>
      <form onSubmit={submit} className="flex flex-col gap-3 rounded-xl border border-slate-700 bg-slate-800/60 p-5">
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="用户名"
          className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100"
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="密码"
          className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100"
        />
        {err && <div className="text-sm text-rose-400">{err}</div>}
        <button
          disabled={loading || !password}
          className="rounded bg-amber-400 px-4 py-2 font-bold text-slate-900 disabled:opacity-50"
        >
          {loading ? '登录中…' : '登录'}
        </button>
      </form>
      {adminToken.get() && (
        <p className="mt-4 text-sm text-slate-400">
          已登录,<a href="#/admin" className="text-amber-300">进管理后台 →</a>
        </p>
      )}
      <p className="mt-6 text-xs text-slate-600">
        首次使用先 CLI 建管理员:<code className="mx-1 text-slate-400">pok-arena admin set-password --password …</code>
      </p>
    </div>
  )
}
