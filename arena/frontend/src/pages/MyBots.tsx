import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, apiUpload, errMsg } from '../api'

interface Bot {
  id: number
  owner_id: number
  name: string
  display_name: string
  description: string
  protocol: 'json' | 'tcp'
  entry_file: string
  runtime_lang: string
  current_version: number
  has_image: boolean
  is_builtin: boolean
  is_public: boolean
  is_active: boolean
  created_at: string
  updated_at: string
}

interface Version {
  id: number
  bot_id: number
  version: number
  source_path: string
  upload_note: string
  checksum: string
  created_at: string
}

export default function MyBots() {
  const { user, loading: authLoading } = useAuth()
  const [bots, setBots] = useState<Bot[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  // 新建表单
  const [showUpload, setShowUpload] = useState(false)
  const [form, setForm] = useState({
    name: '',
    protocol: 'json',
    entry_file: 'main.py',
    display_name: '',
    description: '',
  })
  const [uploadFile, setUploadFile] = useState<File | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  // 展开的 bot(版本历史)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [versions, setVersions] = useState<Record<number, Version[]>>({})
  const [versionUpload, setVersionUpload] = useState<Record<number, File | null>>({})
  const [editingBot, setEditingBot] = useState<number | null>(null)
  const [editForm, setEditForm] = useState({ display_name: '', description: '', is_public: true })

  const load = () => {
    setLoading(true)
    setErr('')
    apiGet<{ bots: Bot[] }>('/api/bots?scope=mine')
      .then((d) => setBots(d.bots || []))
      .catch((e) => setErr(errMsg(e, '加载失败')))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (!authLoading && user) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authLoading, user])

  const loadVersions = async (botId: number) => {
    try {
      const d = await apiGet<{ versions: Version[] }>(`/api/bots/${botId}/versions`)
      setVersions((v) => ({ ...v, [botId]: d.versions || [] }))
    } catch (e) {
      setErr(errMsg(e, '版本加载失败'))
    }
  }

  const toggleExpand = (botId: number) => {
    if (expanded === botId) {
      setExpanded(null)
    } else {
      setExpanded(botId)
      if (!versions[botId]) void loadVersions(botId)
    }
  }

  const submitCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!uploadFile) {
      setErr('请选择 zip 包')
      return
    }
    if (!form.name.trim()) {
      setErr('请填 bot name')
      return
    }
    setBusy(true)
    setErr('')
    setMsg('')
    try {
      await apiUpload('/api/bots', uploadFile, {
        name: form.name.trim(),
        protocol: form.protocol,
        entry_file: form.entry_file.trim() || 'main.py',
        display_name: form.display_name.trim(),
        description: form.description.trim(),
      })
      setMsg(`bot「${form.name}」上传成功`)
      setShowUpload(false)
      setForm({ name: '', protocol: 'json', entry_file: 'main.py', display_name: '', description: '' })
      setUploadFile(null)
      if (fileRef.current) fileRef.current.value = ''
      load()
    } catch (e) {
      setErr(errMsg(e, '上传失败'))
    } finally {
      setBusy(false)
    }
  }

  const submitVersion = async (botId: number) => {
    const f = versionUpload[botId]
    if (!f) {
      setErr('请选择新版本的 zip 包')
      return
    }
    setBusy(true)
    setErr('')
    setMsg('')
    try {
      await apiUpload(`/api/bots/${botId}/versions`, f, {
        upload_note: `新版本 ${new Date().toLocaleString()}`,
      })
      setMsg('新版本上传成功')
      setVersionUpload((v) => ({ ...v, [botId]: null }))
      load()
      loadVersions(botId)
    } catch (e) {
      setErr(errMsg(e, '上传失败'))
    } finally {
      setBusy(false)
    }
  }

  const toggleActive = async (b: Bot) => {
    setBusy(true)
    setErr('')
    try {
      await apiJson(`/api/bots/${b.id}/${b.is_active ? 'deactivate' : 'activate'}`, 'POST')
      load()
    } catch (e) {
      setErr(errMsg(e, '操作失败'))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (b: Bot) => {
    if (!window.confirm(`确认删除 bot「${b.name}」?该操作不可撤销(有对局记录的会被拒绝)。`)) return
    setBusy(true)
    setErr('')
    try {
      await apiJson(`/api/bots/${b.id}`, 'DELETE')
      setMsg('已删除')
      load()
    } catch (e) {
      setErr(errMsg(e, '删除失败'))
    } finally {
      setBusy(false)
    }
  }

  const startEdit = (b: Bot) => {
    setEditingBot(b.id)
    setEditForm({
      display_name: b.display_name,
      description: b.description,
      is_public: b.is_public,
    })
  }

  const saveEdit = async (b: Bot) => {
    setBusy(true)
    setErr('')
    try {
      await apiJson(`/api/bots/${b.id}`, 'PATCH', editForm)
      setEditingBot(null)
      load()
    } catch (e) {
      setErr(errMsg(e, '保存失败'))
    } finally {
      setBusy(false)
    }
  }

  if (authLoading) {
    return <div className="p-8 text-center text-slate-400">加载中…</div>
  }
  if (!user) {
    return (
      <div className="p-8 text-center">
        <p className="mb-3 text-slate-300">请先登录</p>
        <Link to="/login" className="text-amber-300 hover:underline">去登录 →</Link>
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-100">我的 Bot</h1>
          <p className="text-sm text-slate-400">上传 / 管理 / 上下架 你的对战程序</p>
        </div>
        <button
          onClick={() => setShowUpload((v) => !v)}
          className="rounded-lg bg-amber-400 px-4 py-2 text-sm font-bold text-slate-900 hover:bg-amber-300"
        >
          {showUpload ? '收起' : '+ 上传新 Bot'}
        </button>
      </div>

      {err && (
        <div className="mb-4 rounded-lg border border-rose-800 bg-rose-900/30 px-3 py-2 text-sm text-rose-400">
          {err}
        </div>
      )}
      {msg && (
        <div className="mb-4 rounded-lg border border-emerald-800 bg-emerald-900/30 px-3 py-2 text-sm text-emerald-300">
          {msg}
        </div>
      )}

      {showUpload && (
        <form
          onSubmit={submitCreate}
          className="mb-6 rounded-xl border border-slate-700 bg-slate-800/60 p-5"
        >
          <h2 className="mb-3 font-semibold text-amber-300">上传新 Bot(.zip 包)</h2>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <L label="Bot name(同一用户内唯一)">
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="my_poker_bot"
                required
                className={inputCls}
              />
            </L>
            <L label="协议">
              <select
                value={form.protocol}
                onChange={(e) => setForm({ ...form, protocol: e.target.value as 'json' | 'tcp' })}
                className={inputCls}
              >
                <option value="json">json(stdin/stdout)</option>
                <option value="tcp">tcp(socket)</option>
              </select>
            </L>
            <L label="入口文件">
              <input
                value={form.entry_file}
                onChange={(e) => setForm({ ...form, entry_file: e.target.value })}
                placeholder="main.py"
                className={inputCls}
              />
            </L>
            <L label="展示名(可空)">
              <input
                value={form.display_name}
                onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                placeholder="我的扑克 AI"
                className={inputCls}
              />
            </L>
            <L label="描述(可空)" full>
              <textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="简单介绍你的策略…"
                rows={2}
                className={inputCls}
              />
            </L>
            <L label="源码 zip 包" full>
              <input
                ref={fileRef}
                type="file"
                accept=".zip,application/zip"
                onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
                required
                className="block w-full text-sm text-slate-300 file:mr-3 file:rounded file:border-0 file:bg-amber-400 file:px-3 file:py-1.5 file:font-bold file:text-slate-900 hover:file:bg-amber-300"
              />
              <p className="mt-1 text-xs text-slate-500">
                zip 根目录需含入口文件(如 main.py)。构建会跑 docker build。
              </p>
            </L>
          </div>
          <button
            type="submit"
            disabled={busy || !uploadFile || !form.name.trim()}
            className="mt-4 rounded-lg bg-amber-400 px-5 py-2 font-bold text-slate-900 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? '上传构建中…' : '上传并构建'}
          </button>
        </form>
      )}

      {loading ? (
        <div className="py-12 text-center text-slate-400">加载…</div>
      ) : bots.length === 0 ? (
        <div className="rounded-xl border border-slate-700 bg-slate-800/40 py-12 text-center text-slate-400">
          你还没有 bot,点击右上「上传新 Bot」开始。
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {bots.map((b) => (
            <div key={b.id} className="rounded-xl border border-slate-700 bg-slate-800/60">
              {/* 头部 */}
              <div className="flex flex-wrap items-center justify-between gap-3 p-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-lg font-bold text-slate-100">
                      {b.display_name || b.name}
                    </span>
                    {b.is_builtin && (
                      <span className="rounded bg-sky-500/20 px-1.5 text-xs text-sky-300">内置</span>
                    )}
                    {b.is_active ? (
                      <span className="rounded bg-emerald-500/20 px-1.5 text-xs text-emerald-300">上架</span>
                    ) : (
                      <span className="rounded bg-slate-600/30 px-1.5 text-xs text-slate-400">下架</span>
                    )}
                    {b.is_public ? (
                      <span className="rounded bg-amber-500/20 px-1.5 text-xs text-amber-300">公开</span>
                    ) : (
                      <span className="rounded bg-slate-600/30 px-1.5 text-xs text-slate-400">私有</span>
                    )}
                    {!b.has_image && (
                      <span className="rounded bg-rose-500/20 px-1.5 text-xs text-rose-300">无镜像</span>
                    )}
                  </div>
                  <div className="mt-1 font-mono text-xs text-slate-500">
                    {b.name} · {b.protocol} · v{b.current_version} · {b.entry_file}
                  </div>
                  {b.description && (
                    <p className="mt-1 text-sm text-slate-400">{b.description}</p>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <button
                    onClick={() => toggleExpand(b.id)}
                    className="rounded border border-slate-600 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-700"
                  >
                    {expanded === b.id ? '收起 ▲' : '版本 ▼'}
                  </button>
                  <button
                    onClick={() => startEdit(b)}
                    className="rounded border border-amber-500/60 px-2.5 py-1 text-xs text-amber-300 hover:bg-slate-700"
                  >
                    编辑
                  </button>
                  <button
                    onClick={() => toggleActive(b)}
                    disabled={busy}
                    className={`rounded border px-2.5 py-1 text-xs ${
                      b.is_active
                        ? 'border-slate-500 text-slate-300 hover:bg-slate-700'
                        : 'border-emerald-500/60 text-emerald-300 hover:bg-slate-700'
                    } disabled:opacity-50`}
                  >
                    {b.is_active ? '下架' : '上架'}
                  </button>
                  <button
                    onClick={() => remove(b)}
                    disabled={busy}
                    className="rounded border border-rose-600/60 px-2.5 py-1 text-xs text-rose-400 hover:bg-slate-700 disabled:opacity-50"
                  >
                    删除
                  </button>
                </div>
              </div>

              {/* 编辑表单 */}
              {editingBot === b.id && (
                <div className="border-t border-slate-700 p-4">
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <L label="展示名">
                      <input
                        value={editForm.display_name}
                        onChange={(e) => setEditForm({ ...editForm, display_name: e.target.value })}
                        className={inputCls}
                      />
                    </L>
                    <L label="是否公开">
                      <select
                        value={editForm.is_public ? '1' : '0'}
                        onChange={(e) => setEditForm({ ...editForm, is_public: e.target.value === '1' })}
                        className={inputCls}
                      >
                        <option value="1">公开(他人可选为对手)</option>
                        <option value="0">私有</option>
                      </select>
                    </L>
                    <L label="描述" full>
                      <textarea
                        value={editForm.description}
                        onChange={(e) => setEditForm({ ...editForm, description: e.target.value })}
                        rows={2}
                        className={inputCls}
                      />
                    </L>
                  </div>
                  <div className="mt-3 flex gap-2">
                    <button
                      onClick={() => saveEdit(b)}
                      disabled={busy}
                      className="rounded bg-emerald-500 px-3 py-1.5 text-xs font-bold text-slate-900 hover:bg-emerald-400 disabled:opacity-50"
                    >
                      保存
                    </button>
                    <button
                      onClick={() => setEditingBot(null)}
                      className="rounded border border-slate-600 px-3 py-1.5 text-xs text-slate-300"
                    >
                      取消
                    </button>
                  </div>
                </div>
              )}

              {/* 版本历史 */}
              {expanded === b.id && (
                <div className="border-t border-slate-700 p-4">
                  <div className="mb-3 flex items-center justify-between gap-2">
                    <h3 className="text-sm font-semibold text-slate-200">版本历史</h3>
                    <div className="flex items-center gap-2">
                      <input
                        type="file"
                        accept=".zip,application/zip"
                        onChange={(e) =>
                          setVersionUpload((v) => ({ ...v, [b.id]: e.target.files?.[0] ?? null }))
                        }
                        className="text-xs text-slate-300 file:mr-2 file:rounded file:border-0 file:bg-amber-400 file:px-2 file:py-1 file:font-bold file:text-slate-900"
                      />
                      <button
                        onClick={() => submitVersion(b.id)}
                        disabled={busy || !versionUpload[b.id]}
                        className="rounded bg-amber-400 px-2.5 py-1 text-xs font-bold text-slate-900 disabled:opacity-50"
                      >
                        上传新版本
                      </button>
                    </div>
                  </div>
                  {(versions[b.id] || []).length === 0 ? (
                    <div className="py-3 text-center text-sm text-slate-500">暂无版本</div>
                  ) : (
                    <div className="flex flex-col gap-1 font-mono text-xs">
                      {versions[b.id]!.map((v) => (
                        <div
                          key={v.id}
                          className={`flex items-center justify-between rounded border border-slate-800 px-3 py-1.5 ${
                            v.version === b.current_version ? 'bg-amber-400/10' : ''
                          }`}
                        >
                          <span>
                            v{v.version}
                            {v.version === b.current_version && (
                              <span className="ml-1 text-amber-300">(当前)</span>
                            )}
                          </span>
                          <span className="truncate text-slate-500" title={v.upload_note}>
                            {v.upload_note || '—'}
                          </span>
                          <span className="text-slate-600">{v.created_at}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const inputCls =
  'w-full rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-amber-400 focus:outline-none'

function L({
  label,
  children,
  full,
}: {
  label: string
  children: React.ReactNode
  full?: boolean
}) {
  return (
    <label className={`flex flex-col gap-1 text-xs text-slate-400 ${full ? 'sm:col-span-2' : ''}`}>
      {label}
      {children}
    </label>
  )
}
