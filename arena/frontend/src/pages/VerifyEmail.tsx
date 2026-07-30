import { useCallback, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import CaptchaField, { type CaptchaValue } from '../components/CaptchaField'
import { errMsg } from '../api'

export default function VerifyEmail() {
  const nav = useNavigate()
  const [sp] = useSearchParams()
  const [emailOrUsername, setEmailOrUsername] = useState(sp.get('email') || '')
  const [code, setCode] = useState('')
  const [captcha, setCaptcha] = useState<CaptchaValue>({ captcha_id: '', captcha_answer: '' })
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const onCaptcha = useCallback((v: CaptchaValue) => setCaptcha(v), [])

  const verify = async (e: React.FormEvent) => {
    e.preventDefault()
    setErr('')
    setMsg('')
    setLoading(true)
    try {
      const res = await fetch('/api/auth/verify-email', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          email_or_username: emailOrUsername.trim(),
          code: code.trim(),
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : `${res.status}`)
      setMsg(data.message || '验证成功')
      setTimeout(() => nav('/login'), 900)
    } catch (e) {
      setErr(errMsg(e, '验证失败'))
    } finally {
      setLoading(false)
    }
  }

  const resend = async () => {
    setErr('')
    setMsg('')
    if (!captcha.captcha_id || !captcha.captcha_answer.trim()) {
      setErr('请先填写图形验证码再重发')
      return
    }
    setLoading(true)
    try {
      const res = await fetch('/api/auth/resend-verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          email_or_username: emailOrUsername.trim(),
          captcha_id: captcha.captcha_id,
          captcha_answer: captcha.captcha_answer.trim(),
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : `${res.status}`)
      setMsg(data.message || '已重新发送')
    } catch (e) {
      setErr(errMsg(e, '重发失败'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-md p-4 pt-10">
      <h1 className="mb-1 text-xl font-bold text-gray-900">验证邮箱</h1>
      <p className="mb-6 text-sm text-gray-500">
        请查收邮件中的 6 位验证码(也可能在垃圾箱)
      </p>
      <form
        onSubmit={verify}
        className="flex flex-col gap-3 rounded-xl border border-gray-200 bg-white p-5 shadow-theme-sm"
      >
        <label className="flex flex-col gap-1 text-sm text-gray-700">
          用户名或邮箱
          <input
            value={emailOrUsername}
            onChange={(e) => setEmailOrUsername(e.target.value)}
            required
            className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-gray-900 focus:border-brand-300 focus:outline-none"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm text-gray-700">
          邮箱验证码
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="6 位数字"
            required
            className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 font-mono text-gray-900 focus:border-brand-300 focus:outline-none"
          />
        </label>
        {err && <div className="rounded bg-error-50 px-3 py-2 text-sm text-error-500">{err}</div>}
        {msg && <div className="rounded bg-success-50 px-3 py-2 text-sm text-success-600">{msg}</div>}
        <button
          type="submit"
          disabled={loading || !code.trim()}
          className="rounded bg-brand-500 px-4 py-2 font-bold text-white hover:bg-brand-600 disabled:opacity-50"
        >
          {loading ? '提交中…' : '完成验证'}
        </button>
      </form>

      <div className="mt-4 rounded-xl border border-gray-200 bg-gray-50 p-4">
        <p className="mb-2 text-sm text-gray-500">没收到?填写图形验证码后重发</p>
        <CaptchaField onChange={onCaptcha} />
        <button
          type="button"
          onClick={() => void resend()}
          disabled={loading}
          className="mt-3 w-full rounded border border-gray-300 px-4 py-2 text-sm text-gray-800 hover:bg-gray-100 disabled:opacity-50"
        >
          重发验证码
        </button>
      </div>

      <p className="mt-4 text-center text-sm text-gray-500">
        <Link to="/login" className="text-brand-500 hover:underline">← 返回登录</Link>
      </p>
    </div>
  )
}
