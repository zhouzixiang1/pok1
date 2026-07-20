import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { errMsg, userToken } from '../api'

export default function Register() {
  const nav = useNavigate()
  const [form, setForm] = useState({
    username: '',
    email: '',
    password: '',
    confirm: '',
    display_name: '',
  })
  const [err, setErr] = useState('')
  const [ok, setOk] = useState('')
  const [loading, setLoading] = useState(false)

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [k]: e.target.value })

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setErr('')
    setOk('')
    if (form.password !== form.confirm) {
      setErr('两次密码不一致')
      return
    }
    if (form.password.length < 8) {
      setErr('密码至少 8 位')
      return
    }
    if (form.username.trim().length < 3) {
      setErr('用户名至少 3 个字符')
      return
    }
    setLoading(true)
    try {
      const res = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          username: form.username.trim(),
          email: form.email.trim(),
          password: form.password,
          display_name: form.display_name.trim(),
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        throw new Error(data?.detail || `${res.status} ${res.statusText}`)
      }
      setOk('注册成功!正在跳转登录…')
      userToken.clear()
      setTimeout(() => nav(`/login?from=/`), 900)
    } catch (e) {
      setErr(errMsg(e, '注册失败'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-md p-4 pt-10">
      <h1 className="mb-1 text-xl font-bold text-slate-100">注册新账号</h1>
      <p className="mb-6 text-sm text-slate-400">
        注册后即可上传 bot、参与对战、看排行榜
      </p>
      <form
        onSubmit={submit}
        className="flex flex-col gap-3 rounded-xl border border-slate-700 bg-slate-800/60 p-5"
      >
        <Field label="用户名(3-32 字符,字母数字下划线)" value={form.username} onChange={set('username')} placeholder="my_bot_team" />
        <Field label="邮箱(找回密码用)" type="email" value={form.email} onChange={set('email')} placeholder="you@example.com" />
        <Field label="昵称(可中文,可空)" value={form.display_name} onChange={set('display_name')} placeholder="我的扑克战队" />
        <Field label="密码(至少 8 位)" type="password" value={form.password} onChange={set('password')} />
        <Field label="确认密码" type="password" value={form.confirm} onChange={set('confirm')} />
        {err && <div className="rounded bg-rose-900/30 px-3 py-2 text-sm text-rose-400">{err}</div>}
        {ok && <div className="rounded bg-emerald-900/30 px-3 py-2 text-sm text-emerald-300">{ok}</div>}
        <button
          type="submit"
          disabled={loading}
          className="rounded bg-amber-400 px-4 py-2 font-bold text-slate-900 transition hover:bg-amber-300 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? '注册中…' : '注册'}
        </button>
      </form>
      <p className="mt-4 text-center text-sm text-slate-400">
        已有账号?<Link to="/login" className="ml-1 text-amber-300 hover:underline">去登录</Link>
      </p>
    </div>
  )
}

function Field({
  label,
  value,
  onChange,
  type = 'text',
  placeholder,
}: {
  label: string
  value: string
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void
  type?: string
  placeholder?: string
}) {
  return (
    <label className="flex flex-col gap-1 text-sm text-slate-300">
      {label}
      <input
        type={type}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        required
        className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-slate-100 placeholder:text-slate-500 focus:border-amber-400 focus:outline-none"
      />
    </label>
  )
}
