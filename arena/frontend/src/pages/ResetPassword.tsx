import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import CaptchaField, { type CaptchaValue } from '../components/CaptchaField'
import { errMsg } from '../api'

type Phase = 'request' | 'reset' | 'done'

export default function ResetPassword() {
  const [phase, setPhase] = useState<Phase>('request')
  const [emailOrUsername, setEmailOrUsername] = useState('')
  const [code, setCode] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [confirmPwd, setConfirmPwd] = useState('')
  const [captcha, setCaptcha] = useState<CaptchaValue>({ captcha_id: '', captcha_answer: '' })
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const onCaptcha = useCallback((v: CaptchaValue) => setCaptcha(v), [])

  const requestReset = async (e: React.FormEvent) => {
    e.preventDefault()
    setErr('')
    setMsg('')
    if (!captcha.captcha_answer.trim()) {
      setErr('请填写图形验证码')
      return
    }
    setLoading(true)
    try {
      const res = await fetch('/api/auth/request-reset', {
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
      setMsg(data.message || '若账号存在,重置验证码已发送到邮箱')
      setPhase('reset')
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
        body: JSON.stringify({
          email_or_username: emailOrUsername.trim(),
          code: code.trim(),
          new_password: newPwd,
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : `${res.status}`)
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
      <h1 className="mb-1 text-xl font-bold text-gray-900">找回密码</h1>
      <p className="mb-6 text-sm text-gray-500">
        验证码将发送到注册邮箱,填写后即可设置新密码
      </p>

      {phase === 'request' && (
        <form
          onSubmit={requestReset}
          className="flex flex-col gap-3 rounded-xl border border-gray-200 bg-white p-5 shadow-theme-sm"
        >
          <label className="flex flex-col gap-1 text-sm text-gray-700">
            用户名或邮箱
            <input
              value={emailOrUsername}
              onChange={(e) => setEmailOrUsername(e.target.value)}
              placeholder="username 或 you@example.com"
              required
              className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
            />
          </label>
          <CaptchaField onChange={onCaptcha} />
          {err && <div className="rounded bg-error-50 px-3 py-2 text-sm text-error-500">{err}</div>}
          {msg && <div className="rounded bg-success-50 px-3 py-2 text-sm text-success-600">{msg}</div>}
          <button
            type="submit"
            disabled={loading || !emailOrUsername.trim()}
            className="rounded bg-brand-500 px-4 py-2 font-bold text-white transition hover:bg-brand-600 disabled:opacity-50"
          >
            {loading ? '提交中…' : '发送重置验证码'}
          </button>
        </form>
      )}

      {(phase === 'reset' || phase === 'done') && (
        <form
          onSubmit={doReset}
          className="flex flex-col gap-3 rounded-xl border border-gray-200 bg-white p-5 shadow-theme-sm"
        >
          <label className="flex flex-col gap-1 text-sm text-gray-700">
            邮箱验证码
            <input
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="邮件中的 6 位数字"
              required
              className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 font-mono text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
            />
          </label>
          {msg && <div className="rounded bg-success-50 px-3 py-2 text-sm text-success-600">{msg}</div>}
          {phase === 'reset' && (
            <>
              <label className="flex flex-col gap-1 text-sm text-gray-700">
                新密码(至少 8 位)
                <input
                  type="password"
                  value={newPwd}
                  onChange={(e) => setNewPwd(e.target.value)}
                  required
                  className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-gray-900 focus:border-brand-300 focus:outline-none"
                />
              </label>
              <label className="flex flex-col gap-1 text-sm text-gray-700">
                确认新密码
                <input
                  type="password"
                  value={confirmPwd}
                  onChange={(e) => setConfirmPwd(e.target.value)}
                  required
                  className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-gray-900 focus:border-brand-300 focus:outline-none"
                />
              </label>
              {err && (
                <div className="rounded bg-error-50 px-3 py-2 text-sm text-error-500">{err}</div>
              )}
              <button
                type="submit"
                disabled={loading || !code.trim() || !newPwd}
                className="rounded bg-brand-500 px-4 py-2 font-bold text-white transition hover:bg-brand-600 disabled:opacity-50"
              >
                {loading ? '重置中…' : '设置新密码'}
              </button>
            </>
          )}
          {phase === 'done' && (
            <Link
              to="/login"
              className="rounded border border-brand-300 px-4 py-2 text-center font-bold text-brand-500 hover:bg-brand-500/10"
            >
              → 去登录
            </Link>
          )}
        </form>
      )}

      <p className="mt-4 text-center text-sm text-gray-500">
        <Link to="/login" className="text-brand-500 hover:underline">
          ← 返回登录
        </Link>
      </p>
    </div>
  )
}
