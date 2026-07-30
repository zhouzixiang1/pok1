import { useCallback, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import CaptchaField, { type CaptchaValue } from '../components/CaptchaField'
import { useAuth } from '../components/useAuth'
import { ApiError, errMsg } from '../api'

export default function Login() {
  const nav = useNavigate()
  const [sp] = useSearchParams()
  const from = sp.get('from') || '/'
  const { login } = useAuth()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [captcha, setCaptcha] = useState<CaptchaValue>({ captcha_id: '', captcha_answer: '' })
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const onCaptcha = useCallback((v: CaptchaValue) => setCaptcha(v), [])

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setErr('')
    try {
      await login(username.trim(), password, captcha.captcha_id, captcha.captcha_answer.trim())
      nav(from)
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setErr('用户名或密码错误')
      } else if (e instanceof ApiError && e.status === 403) {
        setErr(e.detail || '禁止登录')
        if ((e.detail || '').includes('邮箱未验证')) {
          nav(`/verify-email?email=${encodeURIComponent(username.trim())}`)
          return
        }
      } else {
        setErr(errMsg(e, '登录失败'))
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-sm p-4 pt-12">
      <h1 className="mb-1 text-xl font-bold text-gray-900">登录</h1>
      <p className="mb-6 text-sm text-gray-500">用平台账号登录后才能对战 / 看排行</p>
      <form
        onSubmit={submit}
        className="flex flex-col gap-3 rounded-xl border border-gray-200 bg-white p-5 shadow-theme-sm"
      >
        <label className="flex flex-col gap-1 text-sm text-gray-700">
          用户名
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="username"
            autoFocus
            required
            className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm text-gray-700">
          密码
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            required
            className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
          />
        </label>
        <CaptchaField onChange={onCaptcha} />
        {err && <div className="rounded bg-error-50 px-3 py-2 text-sm text-error-500">{err}</div>}
        <button
          type="submit"
          disabled={loading || !username.trim() || !password || !captcha.captcha_answer.trim()}
          className="rounded bg-brand-500 px-4 py-2 font-bold text-white transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? '登录中…' : '登录'}
        </button>
      </form>
      <div className="mt-4 flex items-center justify-between text-sm text-gray-500">
        <Link to="/register" className="text-brand-500 hover:underline">
          没有账号?去注册 →
        </Link>
        <Link to="/reset-password" className="text-gray-500 hover:text-brand-500 hover:underline">
          忘记密码?
        </Link>
      </div>
    </div>
  )
}
