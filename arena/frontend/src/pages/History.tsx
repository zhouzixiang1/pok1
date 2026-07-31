import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, errMsg } from '../api'

interface MatchRow {
  id?: string
  match_id?: string
  bot_a_name: string
  bot_b_name: string
  bot_a_display?: string
  bot_b_display?: string
  earnings_a: number
  earnings_b: number
  winner: number | null
  reason: string
  hands_played: number
  status?: string
  match_type?: string
  started_at: string
  ended_at?: string
}

interface Data {
  matches: MatchRow[]
  total: number
  limit?: number
  offset?: number
}

const STATUS_LABEL: Record<string, string> = {
  pending: '排队中',
  running: '进行中',
  completed: '已完成',
  aborted: '已中止',
  errored: '出错',
  cancelled: '已取消',
}

function mid(m: MatchRow): string {
  return m.id ?? m.match_id ?? ''
}
function fmtTime(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}
function earningsStr(n: number): string {
  return (n >= 0 ? '+' : '') + (n ?? 0).toLocaleString()
}
function disp(m: MatchRow, side: 'a' | 'b'): string {
  return side === 'a'
    ? m.bot_a_display || m.bot_a_name
    : m.bot_b_display || m.bot_b_name
}

const PAGE_SIZE = 20

export default function History() {
  const [statusFilter, setStatusFilter] = useState('')
  const [botInput, setBotInput] = useState('')
  const [botFilter, setBotFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<Data | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')

  useEffect(() => {
    setLoading(true)
    setErr('')
    const q = new URLSearchParams({
      limit: String(PAGE_SIZE),
      offset: String(offset),
    })
    if (statusFilter) q.set('status', statusFilter)
    apiGet<Data>(`/api/matches?${q.toString()}`)
      .then(setData)
      .catch((e) => setErr(errMsg(e, '加载失败')))
      .finally(() => setLoading(false))
  }, [statusFilter, botFilter, offset])

  const onSearch = () => {
    setBotFilter(botInput.trim())
    setOffset(0)
  }

  const matches = (data?.matches || []).filter((m) => {
    if (!botFilter) return true
    const k = botFilter.toLowerCase()
    return (
      (m.bot_a_name || '').toLowerCase().includes(k) ||
      (m.bot_b_name || '').toLowerCase().includes(k) ||
      (m.bot_a_display || '').toLowerCase().includes(k) ||
      (m.bot_b_display || '').toLowerCase().includes(k)
    )
  })
  const total = data?.total ?? 0
  const from = total > 0 ? offset + 1 : 0
  const to = Math.min(offset + PAGE_SIZE, total)
  const hasPrev = offset > 0
  const hasNext = offset + PAGE_SIZE < total

  return (
    <div className="mx-auto max-w-4xl p-4">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-900">对局历史</h1>
          <p className="text-sm text-gray-500">
            共 <span className="font-mono text-brand-500">{total.toLocaleString()}</span> 场
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            状态
            <select
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value)
                setOffset(0)
              }}
              className="rounded border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-900 focus:border-brand-300 focus:outline-none"
            >
              <option value="">全部</option>
              <option value="completed">已完成</option>
              <option value="aborted">已中止</option>
              <option value="running">进行中</option>
              <option value="pending">排队中</option>
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-gray-500">
            按 bot 名筛
            <input
              value={botInput}
              onChange={(e) => setBotInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && onSearch()}
              placeholder="bot 名(任意一方)"
              className="w-44 rounded border border-gray-200 bg-white px-2 py-1.5 text-sm text-gray-900 placeholder:text-gray-500 focus:border-brand-300 focus:outline-none"
            />
          </label>
          <button
            onClick={onSearch}
            className="rounded bg-brand-500 px-4 py-1.5 text-sm font-bold text-white hover:bg-brand-600"
          >
            筛选
          </button>
        </div>
      </div>

      {loading ? (
        <div className="py-12 text-center text-gray-500">加载…</div>
      ) : err ? (
        <div className="py-12 text-center text-error-500">{err}</div>
      ) : matches.length === 0 ? (
        <div className="py-12 text-center text-gray-500">
          {botFilter ? `没有匹配「${botFilter}」的对局` : '暂无对局'}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {matches.map((m) => {
            const aWin = m.winner === 0
            const bWin = m.winner === 1
            return (
              <Link
                key={mid(m)}
                to={`/match/${mid(m)}`}
                className="block rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm transition hover:border-brand-300 hover:bg-white"
              >
                <div className="flex items-center justify-between gap-2">
                  <span
                    className={`min-w-0 flex-1 truncate font-semibold ${
                      aWin ? 'text-brand-500' : 'text-gray-900'
                    }`}
                  >
                    {disp(m, 'a')}
                    {aWin && <span className="ml-1 text-xs">胜</span>}
                  </span>
                  <span className="shrink-0 font-mono text-xs text-gray-500">vs</span>
                  <span
                    className={`min-w-0 flex-1 truncate text-right font-semibold ${
                      bWin ? 'text-brand-500' : 'text-gray-900'
                    }`}
                  >
                    {bWin && <span className="mr-1 text-xs">胜</span>}
                    {disp(m, 'b')}
                  </span>
                </div>
                <div className="mt-2 flex items-center justify-between gap-2 text-sm">
                  <span className={`font-mono ${m.earnings_a >= 0 ? 'text-success-500' : 'text-error-500'}`}>
                    {earningsStr(m.earnings_a)}
                  </span>
                  <span className={`font-mono ${m.earnings_b >= 0 ? 'text-success-500' : 'text-error-500'}`}>
                    {earningsStr(m.earnings_b)}
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-gray-500">
                  <span className="font-mono">{(m.hands_played ?? 0).toLocaleString()} 手</span>
                  {m.status && (
                    <>
                      <span>·</span>
                      <span
                        className={
                          m.status === 'running'
                            ? 'text-success-500'
                            : m.status === 'aborted'
                              ? 'text-error-500'
                              : m.status === 'errored'
                                ? 'text-error-500'
                                : ''
                        }
                      >
                        {STATUS_LABEL[m.status] ?? m.status}
                      </span>
                    </>
                  )}
                  {m.reason && (
                    <>
                      <span>·</span>
                      <span className="truncate">{m.reason}</span>
                    </>
                  )}
                  <span>·</span>
                  <span>{fmtTime(m.started_at)}</span>
                </div>
              </Link>
            )
          })}
        </div>
      )}

      {!loading && !err && total > 0 && (
        <div className="mt-4 flex items-center justify-between gap-2 text-sm">
          <button
            onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
            disabled={!hasPrev}
            className="rounded border border-gray-200 px-3 py-1 text-gray-800 hover:bg-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            上一页
          </button>
          <span className="text-gray-500">
            <span className="font-mono text-gray-900">{from.toLocaleString()}</span> ~{' '}
            <span className="font-mono text-gray-900">{to.toLocaleString()}</span> /{' '}
            <span className="font-mono text-brand-500">{total.toLocaleString()}</span>
          </span>
          <button
            onClick={() => setOffset((o) => Math.min(total - 1, o + PAGE_SIZE))}
            disabled={!hasNext}
            className="rounded border border-gray-200 px-3 py-1 text-gray-800 hover:bg-white disabled:cursor-not-allowed disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      )}
    </div>
  )
}
