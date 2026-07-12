import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { adminToken, apiJson } from '../api'

interface BotUser {
  name: string
  display_name: string
  team: string
  note: string
  active: number
  created_at: string
}

export default function Admin() {
  const token = adminToken.get()
  const nav = useNavigate()

  const [users, setUsers] = useState<BotUser[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  // 新建表单
  const [nName, setNName] = useState('')
  const [nDisp, setNDisp] = useState('')
  const [nTeam, setNTeam] = useState('')
  const [nNote, setNNote] = useState('')

  // 编辑状态
  const [editing, setEditing] = useState<string | null>(null)
  const [eTeam, setETeam] = useState('')
  const [eNote, setENote] = useState('')
  const [eActive, setEActive] = useState(1)

  const loadUsers = async () => {
    setLoading(true)
    setErr('')
    try {
      const r = await fetch('/api/admin/users', { headers: { 'x-admin-token': token! } })
      if (r.status === 401) {
        adminToken.clear()
        nav('/login')
        return
      }
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
      const d = await r.json()
      setUsers(d.users || [])
    } catch (e) {
      setErr(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!token) {
      nav('/login')
      return
    }
    loadUsers()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const logout = async () => {
    try {
      await apiJson('/api/admin/logout', 'POST', undefined, token!)
    } catch {
      // 忽略服务端登出失败,本地仍清理
    }
    adminToken.clear()
    nav('/login')
  }

  const create = async () => {
    setBusy(true)
    setErr('')
    try {
      await apiJson(
        '/api/admin/users',
        'POST',
        {
          name: nName.trim(),
          display_name: nDisp.trim() || nName.trim(),
          team: nTeam.trim(),
          note: nNote.trim(),
        },
        token!,
      )
      setNName('')
      setNDisp('')
      setNTeam('')
      setNNote('')
      await loadUsers()
    } catch (e) {
      setErr(`新建失败: ${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (name: string) => {
    if (!window.confirm(`确认删除 bot「${name}」?该操作不可撤销。`)) return
    setBusy(true)
    setErr('')
    try {
      await apiJson(`/api/admin/users/${encodeURIComponent(name)}`, 'DELETE', undefined, token!)
      await loadUsers()
    } catch (e) {
      setErr(`删除失败: ${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const startEdit = (u: BotUser) => {
    setEditing(u.name)
    setETeam(u.team || '')
    setENote(u.note || '')
    setEActive(u.active ? 1 : 0)
  }

  const saveEdit = async (name: string) => {
    setBusy(true)
    setErr('')
    try {
      await apiJson(
        `/api/admin/users/${encodeURIComponent(name)}`,
        'PUT',
        { team: eTeam.trim(), note: eNote.trim(), active: eActive },
        token!,
      )
      setEditing(null)
      await loadUsers()
    } catch (e) {
      setErr(`保存失败: ${String(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-5 flex items-center justify-between">
        <h1 className="text-xl font-bold text-slate-100">
          管理后台
        </h1>
        <button
          onClick={logout}
          className="rounded border border-slate-600 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
        >
          登出
        </button>
      </div>

      {/* 新建 bot */}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          if (nName.trim() && !busy) create()
        }}
        className="mb-6 rounded-xl border border-slate-700 bg-slate-800/60 p-4"
      >
        <div className="mb-3 text-sm font-semibold text-amber-300">新建 bot</div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <input
            value={nName}
            onChange={(e) => setNName(e.target.value)}
            placeholder="name(唯一标识)"
            className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500"
          />
          <input
            value={nDisp}
            onChange={(e) => setNDisp(e.target.value)}
            placeholder="display_name(可空)"
            className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500"
          />
          <input
            value={nTeam}
            onChange={(e) => setNTeam(e.target.value)}
            placeholder="team(可空)"
            className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500"
          />
          <input
            value={nNote}
            onChange={(e) => setNNote(e.target.value)}
            placeholder="note(可空)"
            className="rounded border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500"
          />
        </div>
        <button
          type="submit"
          disabled={!nName.trim() || busy}
          className="mt-3 rounded bg-amber-400 px-4 py-2 text-sm font-bold text-slate-900 disabled:opacity-50"
        >
          {busy ? '处理中…' : '新建'}
        </button>
      </form>

      {err && (
        <div className="mb-4 rounded border border-rose-800 bg-rose-900/30 px-3 py-2 text-sm text-rose-400">
          {err}
        </div>
      )}

      {loading ? (
        <div className="py-10 text-center text-slate-400">加载用户列表…</div>
      ) : users.length === 0 ? (
        <div className="py-10 text-center text-slate-400">暂无 bot 用户,用上方表单新建一个。</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-700 text-slate-400">
                <th className="py-2 text-left">name</th>
                <th className="text-left">display_name</th>
                <th className="text-left">team</th>
                <th className="text-left">note</th>
                <th className="text-left">active</th>
                <th className="text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) =>
                editing === u.name ? (
                  <tr key={u.name} className="border-b border-slate-800 bg-slate-800/40">
                    <td className="py-2 font-mono text-amber-300">{u.name}</td>
                    <td className="text-slate-300">{u.display_name}</td>
                    <td>
                      <input
                        value={eTeam}
                        onChange={(e) => setETeam(e.target.value)}
                        placeholder="team"
                        className="w-full rounded border border-slate-600 bg-slate-900 px-2 py-1 text-slate-100 placeholder:text-slate-500"
                      />
                    </td>
                    <td>
                      <input
                        value={eNote}
                        onChange={(e) => setENote(e.target.value)}
                        placeholder="note"
                        className="w-full rounded border border-slate-600 bg-slate-900 px-2 py-1 text-slate-100 placeholder:text-slate-500"
                      />
                    </td>
                    <td>
                      <select
                        value={eActive}
                        onChange={(e) => setEActive(Number(e.target.value))}
                        className="rounded border border-slate-600 bg-slate-900 px-2 py-1 text-slate-100"
                      >
                        <option value={1}>启用</option>
                        <option value={0}>停用</option>
                      </select>
                    </td>
                    <td className="text-right whitespace-nowrap">
                      <button
                        onClick={() => saveEdit(u.name)}
                        disabled={busy}
                        className="rounded bg-emerald-500 px-2.5 py-1 text-xs font-bold text-slate-900 disabled:opacity-50"
                      >
                        保存
                      </button>
                      <button
                        onClick={() => setEditing(null)}
                        disabled={busy}
                        className="ml-1 rounded border border-slate-600 px-2.5 py-1 text-xs text-slate-300 disabled:opacity-50"
                      >
                        取消
                      </button>
                    </td>
                  </tr>
                ) : (
                  <tr key={u.name} className="border-b border-slate-800 hover:bg-slate-800/50">
                    <td className="py-2 font-mono text-slate-100">{u.name}</td>
                    <td className="text-slate-200">{u.display_name}</td>
                    <td className="text-slate-400">{u.team || '—'}</td>
                    <td className="max-w-xs truncate text-slate-400" title={u.note}>
                      {u.note || '—'}
                    </td>
                    <td>
                      {u.active ? (
                        <span className="rounded bg-emerald-500/20 px-2 py-0.5 text-xs text-emerald-400">启用</span>
                      ) : (
                        <span className="rounded bg-slate-600/30 px-2 py-0.5 text-xs text-slate-400">停用</span>
                      )}
                    </td>
                    <td className="text-right whitespace-nowrap">
                      <button
                        onClick={() => startEdit(u)}
                        disabled={busy}
                        className="rounded border border-amber-500/60 px-2.5 py-1 text-xs text-amber-300 hover:bg-slate-800 disabled:opacity-50"
                      >
                        编辑
                      </button>
                      <button
                        onClick={() => remove(u.name)}
                        disabled={busy}
                        className="ml-1 rounded border border-rose-600/60 px-2.5 py-1 text-xs text-rose-400 hover:bg-slate-800 disabled:opacity-50"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
