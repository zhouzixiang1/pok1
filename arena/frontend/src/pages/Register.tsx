import { useCallback, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import CaptchaField, { type CaptchaValue } from '../components/CaptchaField'
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
  const [captcha, setCaptcha] = useState<CaptchaValue>({ captcha_id: '', captcha_answer: '' })
  const [err, setErr] = useState('')
  const [ok, setOk] = useState('')
  const [loading, setLoading] = useState(false)
  const onCaptcha = useCallback((v: CaptchaValue) => setCaptcha(v), [])

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
    if (!captcha.captcha_id || !captcha.captcha_answer.trim()) {
      setErr('请填写验证码')
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
          captcha_id: captcha.captcha_id,
          captcha_answer: captcha.captcha_answer.trim(),
        }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) {
        throw new Error(typeof data?.detail === 'string' ? data.detail : `${res.status}`)
      }
      setOk('注册成功!验证码已发到邮箱,正在跳转…')
      userToken.clear()
      const email = encodeURIComponent(form.email.trim())
      setTimeout(() => nav(`/verify-email?email=${email}`), 800)
    } catch (e) {
      setErr(errMsg(e, '注册失败'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mx-auto max-w-md p-4 pt-10">
      <h1 className="mb-1 text-xl font-bold text-gray-900">注册新账号</h1>
      <p className="mb-6 text-sm text-gray-500">
        注册后需验证邮箱,然后即可上传 bot、参与对战
      </p>
      <form
        onSubmit={submit}
        className="flex flex-col gap-3 rounded-xl border border-gray-200 bg-white p-5 shadow-theme-sm"
      >
        <Field label="用户名(3-32 字符,字母数字下划线)" value={form.username} onChange={set('username')} placeholder="my_bot_team" />
        <Field label="邮箱(用于验证与找回密码)" type="email" value={form.email} onChange={set('email')} placeholder="you@example.com" />
        <Field label="昵称(可中文,可空)" value={form.display_name} onChange={set('display_name')} placeholder="我的扑克战队" required={false} />
        <Field label="密码(至少 8 位)" type="password" value={form.password} onChange={set('password')} />
        <Field label="确认密码" type="password" value={form.confirm} onChange={set('confirm')} />
        <CaptchaField onChange={onCaptcha} />
        {err && <div className="rounded bg-error-50 px-3 py-2 text-sm text-error-500">{err}</div>}
        {ok && <div className="rounded bg-success-50 px-3 py-2 text-sm text-success-600">{ok}</div>}
        <button
          type="submit"
          disabled={loading}
          className="rounded bg-brand-500 px-4 py-2 font-bold text-white transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? '注册中…' : '注册'}
        </button>
      </form>
      <p className="mt-4 text-center text-sm text-gray-500">
        已有账号?<Link to="/login" className="ml-1 text-brand-500 hover:underline">去登录</Link>
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
  required = true,
}: {
  label: string
  value: string
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void
  type?: string
  placeholder?: string
  required?: boolean
}) {
  return (
    <label className="flex flex-col gap-1 text-sm text-gray-700">
      {label}
      <input
        type={type}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        required={required}
        className="rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
      />
    </label>
  )
}
