/** 图形验证码输入组件:拉取 /api/auth/captcha,点击刷新。 */
import { useCallback, useEffect, useState } from 'react'

export interface CaptchaValue {
  captcha_id: string
  captcha_answer: string
}

interface Props {
  onChange: (v: CaptchaValue) => void
  className?: string
}

export default function CaptchaField({ onChange, className = '' }: Props) {
  const [id, setId] = useState('')
  const [img, setImg] = useState('')
  const [answer, setAnswer] = useState('')
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  const refresh = useCallback(async () => {
    setLoading(true)
    setErr('')
    setAnswer('')
    try {
      const r = await fetch('/api/auth/captcha', { credentials: 'include' })
      const d = await r.json()
      if (!r.ok) throw new Error(d?.detail || '获取验证码失败')
      setId(d.captcha_id)
      setImg(d.image_base64)
      onChange({ captcha_id: d.captcha_id, captcha_answer: '' })
    } catch (e: any) {
      setErr(e?.message || '获取验证码失败')
    } finally {
      setLoading(false)
    }
  }, [onChange])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <div className={`flex flex-col gap-1 text-sm text-gray-700 ${className}`}>
      <span>验证码</span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => void refresh()}
          title="点击刷新"
          className="h-14 w-40 shrink-0 overflow-hidden rounded border border-gray-300 bg-white"
        >
          {img ? (
            <img src={img} alt="captcha" className="h-full w-full object-contain" />
          ) : (
            <span className="text-xs text-gray-500">{loading ? '加载…' : '刷新'}</span>
          )}
        </button>
        <input
          value={answer}
          onChange={(e) => {
            const v = e.target.value
            setAnswer(v)
            onChange({ captcha_id: id, captcha_answer: v })
          }}
          placeholder="图中字符或算式结果"
          required
          autoComplete="off"
          className="min-w-0 flex-1 rounded-lg border border-gray-300 bg-transparent px-3 py-2.5 text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
        />
      </div>
      {err && <span className="text-xs text-error-500">{err}</span>}
      <span className="text-xs text-gray-500">看不清可点击图片刷新</span>
    </div>
  )
}
