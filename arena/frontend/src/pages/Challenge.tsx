import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../components/useAuth'
import { apiGet, apiJson, errMsg } from '../api'

interface Bot {
  id: number
  name: string
  display_name: string
  description: string
  protocol: string
  current_version: number
  has_image: boolean
  is_builtin: boolean
  is_public: boolean
  is_active: boolean
}

export default function Challenge() {
  const { user, loading: authLoading } = useAuth()
  const nav = useNavigate()
  const [mine, setMine] = useState<Bot[]>([])
  const [publicBots, setPublicBots] = useState<Bot[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState('')

  const [myBotId, setMyBotId] = useState<number | null>(null)
  const [oppBotId, setOppBotId] = useState<number | null>(null)

  useEffect(() => {
    if (!authLoading && user) {
      Promise.all([
        apiGet<{ bots: Bot[] }>('/api/bots?scope=mine'),
        apiGet<{ bots: Bot[] }>('/api/bots?scope=public'),
      ])
        .then(([a, b]) => {
          const mineActive = (a.bots || []).filter((x) => x.is_active && x.has_image)
          setMine(mineActive)
          setPublicBots(b.bots || [])
          if (mineActive[0]) setMyBotId(mineActive[0].id)
        })
        .catch((e) => setErr(errMsg(e, '加载失败')))
        .finally(() => setLoading(false))
    } else if (!authLoading && !user) {
      setLoading(false)
    }
  }, [authLoading, user])

  const challenge = async () => {
    if (!oppBotId) {
      setErr('请选择对手 bot')
      return
    }
    setBusy(true)
    setErr('')
    try {
      const d = await apiJson<{ match_id: string; status: string }>('/api/matches/challenge', 'POST', {
        my_bot_id: myBotId ?? undefined,
        opponent_bot_id: oppBotId,
      })
      nav(`/match/${d.match_id}`)
    } catch (e) {
      setErr(errMsg(e, '发起对战失败'))
    } finally {
      setBusy(false)
    }
  }

  if (authLoading || loading) {
    return <div className="p-8 text-center text-slate-400">加载…</div>
  }
  if (!user) {
    return (
      <div className="p-8 text-center">
        <p className="mb-3 text-slate-300">请先登录后再发起对战</p>
        <a href="#/login" className="text-amber-300 hover:underline">去登录 →</a>
      </div>
    )
  }

  const myBot = mine.find((b) => b.id === myBotId)
  const oppBot = publicBots.find((b) => b.id === oppBotId)
  const filteredPublic = publicBots.filter(
    (b) =>
      !filter ||
      b.name.toLowerCase().includes(filter.toLowerCase()) ||
      (b.display_name || '').toLowerCase().includes(filter.toLowerCase()),
  )

  return (
    <div className="mx-auto max-w-5xl p-4">
      <h1 className="mb-1 text-xl font-bold text-slate-100">发起对战</h1>
      <p className="mb-4 text-sm text-slate-400">选你的 bot 和对手,开打后跳转实时观赛</p>

      {err && (
        <div className="mb-4 rounded-lg border border-rose-800 bg-rose-900/30 px-3 py-2 text-sm text-rose-400">
          {err}
        </div>
      )}

      {mine.length === 0 ? (
        <div className="rounded-xl border border-slate-700 bg-slate-800/40 p-8 text-center text-slate-400">
          你还没有可对战的 bot(需上架且有镜像)。<a href="#/my-bots" className="text-amber-300 hover:underline">去上传 →</a>
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[360px_1fr]">
          {/* 左:我的 bot 选择 */}
          <aside className="rounded-xl border border-slate-700 bg-slate-800/60 p-4">
            <h2 className="mb-3 font-semibold text-slate-200">我的 Bot(已上架)</h2>
            <div className="flex flex-col gap-2">
              {mine.map((b) => (
                <BotCard
                  key={b.id}
                  b={b}
                  selected={myBotId === b.id}
                  onClick={() => setMyBotId(b.id)}
                />
              ))}
            </div>
          </aside>

          {/* 右:对手选择 */}
          <section className="rounded-xl border border-slate-700 bg-slate-800/60 p-4">
            <div className="mb-3 flex items-center justify-between gap-2">
              <h2 className="font-semibold text-slate-200">选对手(公开 bot 库)</h2>
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="搜索…"
                className="w-40 rounded border border-slate-600 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-amber-400 focus:outline-none"
              />
            </div>
            <div className="grid max-h-[60vh] grid-cols-1 gap-2 overflow-y-auto sm:grid-cols-2">
              {filteredPublic.length === 0 ? (
                <div className="col-span-full py-6 text-center text-sm text-slate-500">
                  {filter ? '没有匹配的 bot' : '暂无公开 bot'}
                </div>
              ) : (
                filteredPublic.map((b) => (
                  <BotCard
                    key={b.id}
                    b={b}
                    selected={oppBotId === b.id}
                    disabled={b.id === myBotId}
                    onClick={() => setOppBotId(b.id)}
                  />
                ))
              )}
            </div>
          </section>
        </div>
      )}

      {/* 底部确认条 */}
      {mine.length > 0 && (
        <div className="sticky bottom-0 mt-4 flex items-center justify-between gap-3 rounded-xl border border-slate-700 bg-slate-900/95 p-4 backdrop-blur">
          <div className="min-w-0 flex-1 text-sm text-slate-300">
            {myBot && (
              <span className="mr-2">
                <span className="text-slate-500">我:</span>
                <span className="font-bold text-amber-300">{myBot.display_name || myBot.name}</span>
              </span>
            )}
            {oppBot && (
              <span>
                <span className="text-slate-500">vs</span>
                <span className="ml-2 font-bold text-fuchsia-300">{oppBot.display_name || oppBot.name}</span>
              </span>
            )}
            {!oppBot && <span className="text-slate-500">请在右侧选择对手</span>}
          </div>
          <button
            onClick={challenge}
            disabled={busy || !oppBotId}
            className="rounded-lg bg-amber-400 px-6 py-2 font-bold text-slate-900 transition hover:bg-amber-300 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? '开打中…' : '🂡 开打!'}
          </button>
        </div>
      )}
    </div>
  )
}

function BotCard({
  b,
  selected,
  onClick,
  disabled,
}: {
  b: Bot
  selected: boolean
  onClick: () => void
  disabled?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg border p-3 text-left transition disabled:opacity-40 ${
        selected
          ? 'border-amber-400 bg-amber-400/10 ring-1 ring-amber-400/40'
          : 'border-slate-700 bg-slate-900/40 hover:border-slate-500'
      }`}
    >
      <div className="flex items-center gap-1.5">
        <span className="font-semibold text-slate-100">{b.display_name || b.name}</span>
        {b.is_builtin && (
          <span className="rounded bg-sky-500/20 px-1 text-[10px] text-sky-300">内置</span>
        )}
        {disabled && <span className="text-[10px] text-slate-500">(自己的)</span>}
      </div>
      <div className="mt-0.5 font-mono text-xs text-slate-500">
        {b.name} · {b.protocol} · v{b.current_version}
      </div>
      {b.description && (
        <p className="mt-1 line-clamp-2 text-xs text-slate-400">{b.description}</p>
      )}
    </button>
  )
}
