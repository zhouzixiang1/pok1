import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import CardView from '../components/CardView'
import { apiGet, errMsg, isUnauthorized } from '../api'

/* ══════════════════════════════════════════════════════════
 * 类型(对齐后端 /api/matches/{id}/replay)
 * ══════════════════════════════════════════════════════════ */

interface MatchMeta {
  id?: string
  match_id?: string
  bot_a_id?: number
  bot_b_id?: number
  bot_a_name: string
  bot_b_name: string
  bot_a_display?: string
  bot_b_display?: string
  status?: string
  total_hands?: number
  hands_played: number
  earnings_a: number
  earnings_b: number
  winner: number | null
  reason: string
  started_at: string
  ended_at?: string
}

interface Action {
  player_idx: number
  action: string
  amount?: number | null
  stage: string
  pot?: number | null
  chips_after?: number[] | null
}

interface Settle {
  winner_idx: number | null
  earnings: number[] | null
  is_showdown: boolean
  pot: number | null
}

interface HandSnapshot {
  hand: number
  sb_idx?: number | null
  bb_idx?: number | null
  names: string[]
  initial_chips: number[] | null
  hole_cards: unknown[][]
  community: unknown[]
  actions: Action[]
  settle: Settle | null
  final_chips: number[] | null
}

interface ReplayData {
  match: MatchMeta
  snapshots: HandSnapshot[]
  events: any[]
}

/* ══════════════════════════════════════════════════════════
 * 辅助
 * ══════════════════════════════════════════════════════════ */

const STAGE_LABEL: Record<string, string> = {
  preflop: '翻前',
  flop: '翻牌',
  turn: '转牌',
  river: '河牌',
  showdown: '摊牌',
}

function fmtTime(s?: string): string {
  if (!s) return '—'
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString()
}
function earnStr(n: number): string {
  return (n >= 0 ? '+' : '') + (n ?? 0).toLocaleString()
}
function actionLabel(a: Action): string {
  const act = a.action
  switch (act) {
    case 'fold':
      return '弃牌'
    case 'check':
      return '过牌'
    case 'call':
      return '跟注'
    case 'raise':
      return `加注到 ${a.amount ?? 0}`
    case 'allin':
      return `全押 ${a.amount ?? 0}`
    case 'timeout':
      return '⏱ 超时弃牌'
    default:
      if (typeof act === 'string' && act.startsWith('illegal:')) return `⛔ 非法 ${act.slice(8)} →弃牌`
      return String(act ?? '')
  }
}

/* ══════════════════════════════════════════════════════════
 * 主组件
 * ══════════════════════════════════════════════════════════ */

export default function MatchDetail() {
  const { id } = useParams()
  const [data, setData] = useState<ReplayData | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  // 回放控制:当前手索引(0..len-1)、当前步(0..actions.length)
  const [handIdx, setHandIdx] = useState(0)
  const [stepIdx, setStepIdx] = useState(0)
  const [auto, setAuto] = useState(false)
  const [autoSpeed, setAutoSpeed] = useState(700)

  const load = () => {
    setLoading(true)
    setErr('')
    apiGet<ReplayData>(`/api/matches/${encodeURIComponent(id ?? '')}/replay`)
      .then((d) => {
        setData(d)
        setHandIdx(0)
        setStepIdx(0)
      })
      .catch((e) => {
        if (isUnauthorized(e)) setErr('请先登录后再查看回放')
        else setErr(errMsg(e, '加载失败'))
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id])

  // 自动播放:推 step,到末尾推手
  useEffect(() => {
    if (!auto || !data) return
    const t = setTimeout(() => {
      const snap = data.snapshots[handIdx]
      if (!snap) {
        setAuto(false)
        return
      }
      if (stepIdx < snap.actions.length) {
        setStepIdx((s) => s + 1)
      } else if (handIdx < data.snapshots.length - 1) {
        setHandIdx((h) => h + 1)
        setStepIdx(0)
      } else {
        setAuto(false)
      }
    }, autoSpeed)
    return () => clearTimeout(t)
  }, [auto, autoSpeed, stepIdx, handIdx, data])

  if (loading) {
    return (
      <div className="mx-auto max-w-5xl p-4">
        <div className="py-20 text-center text-slate-400">加载回放…</div>
      </div>
    )
  }
  if (err || !data) {
    const notFound = err.includes('404') || err.includes('尚无回放')
    return (
      <div className="mx-auto max-w-5xl p-4">
        <div className="py-20 text-center">
          <p className="mb-3 text-rose-400">
            {notFound ? `对局或回放不存在: ${id}` : err}
          </p>
          <Link to="/history" className="text-amber-300 hover:underline">
            ← 返回历史对局
          </Link>
        </div>
      </div>
    )
  }

  const m = data.match
  const snaps = data.snapshots || []
  const snap = snaps[handIdx]
  const noReplay = snaps.length === 0

  const goPrevHand = () => {
    setAuto(false)
    setHandIdx((h) => Math.max(0, h - 1))
    setStepIdx(0)
  }
  const goNextHand = () => {
    setAuto(false)
    setHandIdx((h) => Math.min(snaps.length - 1, h + 1))
    setStepIdx(0)
  }
  const goPrevStep = () => {
    setAuto(false)
    if (stepIdx > 0) setStepIdx((s) => s - 1)
    else if (handIdx > 0) {
      const prev = snaps[handIdx - 1]
      setHandIdx((h) => h - 1)
      setStepIdx(prev ? prev.actions.length : 0)
    }
  }
  const goNextStep = () => {
    setAuto(false)
    if (snap && stepIdx < snap.actions.length) setStepIdx((s) => s + 1)
    else if (handIdx < snaps.length - 1) {
      setHandIdx((h) => h + 1)
      setStepIdx(0)
    }
  }
  const jumpHand = (h: number) => {
    setAuto(false)
    setHandIdx(h)
    setStepIdx(0)
  }

  return (
    <div className="mx-auto max-w-5xl p-4">
      <div className="mb-3">
        <Link to="/history" className="text-sm text-slate-400 hover:text-amber-300 hover:underline">
          ← 历史对局
        </Link>
      </div>

      <MatchMetaCard m={m} id={id ?? ''} />

      {noReplay ? (
        <div className="mt-4 rounded-xl border border-slate-700 bg-slate-800/60 p-8 text-center text-slate-400">
          {m.status === 'running' || m.status === 'pending' ? (
            <>
              <p className="mb-3">对局进行中,暂无完整回放数据。</p>
              <p className="text-sm">可稍后刷新,或在「观赛」页实时观看。</p>
            </>
          ) : (
            <p>该对局暂无回放数据。</p>
          )}
        </div>
      ) : (
        snap && (
          <ReplayBoard
            m={m}
            snaps={snaps}
            handIdx={handIdx}
            stepIdx={stepIdx}
            auto={auto}
            autoSpeed={autoSpeed}
            onPrevHand={goPrevHand}
            onNextHand={goNextHand}
            onPrevStep={goPrevStep}
            onNextStep={goNextStep}
            onSeekStep={(n) => {
              setAuto(false)
              setStepIdx(Math.max(0, Math.min(n, snap.actions.length)))
            }}
            onToggleAuto={() => setAuto((a) => !a)}
            onSpeedChange={setAutoSpeed}
            onJumpHand={jumpHand}
          />
        )
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
 * 元数据卡
 * ══════════════════════════════════════════════════════════ */

function MatchMetaCard({ m, id }: { m: MatchMeta; id: string }) {
  const aWin = m.winner === 0
  const bWin = m.winner === 1
  return (
    <div className="rounded-2xl border border-slate-700 bg-slate-800/60 p-5">
      <div className="break-all font-mono text-xs text-slate-500">{id}</div>
      <div className="mt-3 grid grid-cols-[1fr_auto_1fr] items-center gap-3">
        <div className="text-center">
          <div className={`truncate text-lg font-bold ${aWin ? 'text-amber-300' : 'text-slate-100'}`}>
            {m.bot_a_display || m.bot_a_name}
            {aWin && <span className="ml-1 text-xs">胜</span>}
          </div>
          <div className={`mt-1 font-mono text-2xl font-bold ${m.earnings_a >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {earnStr(m.earnings_a)}
          </div>
        </div>
        <div className="text-sm text-slate-500">vs</div>
        <div className="text-center">
          <div className={`truncate text-lg font-bold ${bWin ? 'text-amber-300' : 'text-slate-100'}`}>
            {bWin && <span className="mr-1 text-xs">胜</span>}
            {m.bot_b_display || m.bot_b_name}
          </div>
          <div className={`mt-1 font-mono text-2xl font-bold ${m.earnings_b >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {earnStr(m.earnings_b)}
          </div>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 text-sm text-slate-400">
        <span>
          <span className="font-mono text-slate-200">{(m.hands_played ?? 0).toLocaleString()}</span> 手
        </span>
        {m.status && (
          <>
            <span className="text-slate-600">·</span>
            <span>{m.status}</span>
          </>
        )}
        {m.reason && (
          <>
            <span className="text-slate-600">·</span>
            <span>{m.reason}</span>
          </>
        )}
        {m.winner == null && (
          <>
            <span className="text-slate-600">·</span>
            <span>未分胜负</span>
          </>
        )}
      </div>
      <div className="mt-1 text-center text-xs text-slate-500">
        {fmtTime(m.started_at)} {m.ended_at && `~ ${fmtTime(m.ended_at)}`}
      </div>
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
 * 图形化回放器
 * ══════════════════════════════════════════════════════════ */

interface BoardProps {
  m: MatchMeta
  snaps: HandSnapshot[]
  handIdx: number
  stepIdx: number
  auto: boolean
  autoSpeed: number
  onPrevHand: () => void
  onNextHand: () => void
  onPrevStep: () => void
  onNextStep: () => void
  onSeekStep: (n: number) => void
  onToggleAuto: () => void
  onSpeedChange: (ms: number) => void
  onJumpHand: (h: number) => void
}

function ReplayBoard(props: BoardProps) {
  const { m, snaps, handIdx, stepIdx, auto, autoSpeed } = props
  const snap = snaps[handIdx]
  const actions = snap.actions
  const visibleActions = actions.slice(0, stepIdx)
  const lastAction = visibleActions[visibleActions.length - 1]
  const curStage = deriveStage(snap, stepIdx)
  const chips = deriveChips(snap, stepIdx)
  const community = deriveCommunity(snap, stepIdx)
  const lastActionRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (lastActionRef.current) lastActionRef.current.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [stepIdx, handIdx])

  // 自己的手牌:第一手牌发下后(stepIdx>0)或本手已结束就显示
  const showHole = stepIdx > 0 || snap.actions.length === 0
  // 摊牌:本手结算且为 showdown,显示对手手牌
  const atHandEnd = stepIdx >= snap.actions.length && snap.settle != null
  const revealOpponent = atHandEnd && !!snap.settle?.is_showdown

  const names = snap.names.length === 2 ? snap.names : [m.bot_a_name, m.bot_b_name]
  const sbIdx = snap.sb_idx ?? 0
  const bbIdx = snap.bb_idx ?? 1
  const pot = lastAction?.pot ?? (atHandEnd ? snap.settle?.pot : 0) ?? 0

  return (
    <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_360px]">
      {/* 牌桌 */}
      <section className="rounded-2xl border border-slate-700 bg-gradient-to-b from-emerald-950 to-slate-900 p-5">
        {/* 上方玩家(P1) */}
        <PlayerSeat
          idx={1}
          name={names[1]}
          chips={chips?.[1] ?? snap.final_chips?.[1] ?? snap.initial_chips?.[1] ?? 20000}
          cards={snap.hole_cards?.[1] || []}
          showCards={revealOpponent}
          isSB={bbIdx === 1}
          isBB={sbIdx === 1}
          actingIdx={lastAction?.player_idx}
          highlightWinner={snap.settle?.winner_idx === 1 && atHandEnd}
        />

        {/* 公共牌 + 底池 */}
        <div className="my-5 flex flex-col items-center gap-2">
          <div className="text-xs uppercase tracking-wider text-emerald-300/70">
            {curStage ? STAGE_LABEL[curStage] ?? curStage : '公共牌'}
          </div>
          <div className="flex min-h-[56px] items-center gap-1.5">
            {Array.from({ length: 5 }).map((_, i) => {
              const c = community[i]
              return c ? (
                <CardView key={i} card={c} size="md" />
              ) : (
                <CardView key={i} empty size="md" />
              )
            })}
          </div>
          <div className="mt-1 flex items-center gap-4">
            <div className="text-sm text-slate-400">
              底池 <span className="font-mono text-lg font-bold text-amber-300">{pot.toLocaleString()}</span>
            </div>
          </div>
        </div>

        {/* 下方玩家(P0) */}
        <PlayerSeat
          idx={0}
          name={names[0]}
          chips={chips?.[0] ?? snap.final_chips?.[0] ?? snap.initial_chips?.[0] ?? 20000}
          cards={snap.hole_cards?.[0] || []}
          showCards={showHole}
          isSB={sbIdx === 0}
          isBB={bbIdx === 0}
          actingIdx={lastAction?.player_idx}
          highlightWinner={snap.settle?.winner_idx === 0 && atHandEnd}
        />

        {atHandEnd && snap.settle && <SettleBanner snap={snap} names={names} />}

        {/* 控制条 */}
        <div className="mt-5 border-t border-slate-700 pt-4">
          <div className="mb-3 flex items-center justify-between gap-2">
            <span className="text-sm text-slate-300">
              第 <span className="font-mono font-bold text-amber-300">{snap.hand}</span> 手 ·
              动作 <span className="font-mono">{stepIdx}</span> / {snap.actions.length}
            </span>
            <span className="text-xs text-slate-500">共 {snaps.length} 手</span>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <CtrlBtn onClick={props.onPrevHand} title="上一手">⏮</CtrlBtn>
            <CtrlBtn onClick={props.onPrevStep} title="上一步">◀</CtrlBtn>
            <button
              onClick={props.onToggleAuto}
              className={`rounded-md px-4 py-1.5 text-sm font-bold transition ${
                auto
                  ? 'bg-rose-500 text-white hover:bg-rose-400'
                  : 'bg-amber-400 text-slate-900 hover:bg-amber-300'
              }`}
            >
              {auto ? '❚❚ 暂停' : '▶ 自动播放'}
            </button>
            <CtrlBtn onClick={props.onNextStep} title="下一步">▶</CtrlBtn>
            <CtrlBtn onClick={props.onNextHand} title="下一手">⏭</CtrlBtn>
            <select
              value={autoSpeed}
              onChange={(e) => props.onSpeedChange(Number(e.target.value))}
              className="ml-1 rounded border border-slate-600 bg-slate-800 px-2 py-1.5 text-xs text-slate-200"
              title="自动播放速度"
            >
              <option value={1200}>慢</option>
              <option value={700}>中</option>
              <option value={350}>快</option>
            </select>
          </div>
          {/* 手内步进进度条 */}
          <input
            type="range"
            min={0}
            max={snap.actions.length}
            value={stepIdx}
            onChange={(e) => props.onSeekStep(Number(e.target.value))}
            className="mt-3 w-full accent-amber-400"
          />
        </div>
      </section>

      {/* 右侧:本手动作 + 手导航 */}
      <aside className="flex flex-col gap-3">
        <div className="rounded-xl border border-slate-700 bg-slate-900/80">
          <div className="px-4 py-2 text-sm font-semibold text-slate-200">
            本手动作 ({visibleActions.length} / {snap.actions.length})
          </div>
          <div className="max-h-[40vh] overflow-y-auto px-3 pb-3 font-mono text-xs">
            {visibleActions.length === 0 ? (
              <div className="px-2 py-3 text-center text-slate-500">尚未行动</div>
            ) : (
              visibleActions.map((a, i) => (
                <ActionRow key={i} a={a} names={names} cur={i === visibleActions.length - 1} />
              ))
            )}
            <div ref={lastActionRef} />
          </div>
        </div>

        <div className="rounded-xl border border-slate-700 bg-slate-900/80 p-3">
          <div className="mb-2 text-sm font-semibold text-slate-200">手导航</div>
          <div className="flex max-h-[35vh] flex-wrap gap-1 overflow-y-auto">
            {snaps.map((s, i) => {
              const isWinnerHere = s.settle && s.settle.winner_idx != null
              return (
                <button
                  key={i}
                  onClick={() => props.onJumpHand(i)}
                  className={`h-7 min-w-[2rem] rounded px-1.5 font-mono text-xs transition ${
                    i === handIdx
                      ? 'bg-amber-400 font-bold text-slate-900'
                      : 'bg-slate-800 text-slate-300 hover:bg-slate-700'
                  }`}
                  title={`第 ${s.hand} 手`}
                >
                  {s.hand}
                  {isWinnerHere && <span className="ml-0.5 text-emerald-400">·</span>}
                </button>
              )
            })}
          </div>
        </div>
      </aside>
    </div>
  )
}

/* 玩家位 */
function PlayerSeat({
  idx,
  name,
  chips,
  cards,
  showCards,
  isSB,
  isBB,
  actingIdx,
  highlightWinner,
}: {
  idx: number
  name: string
  chips: number
  cards: unknown[]
  showCards: boolean
  isSB: boolean
  isBB: boolean
  actingIdx?: number
  highlightWinner?: boolean
}) {
  const acting = actingIdx === idx
  return (
    <div
      className={`flex flex-col items-center gap-1 rounded-xl border p-4 transition ${
        highlightWinner
          ? 'border-amber-400 bg-amber-400/15 shadow-[0_0_24px_rgba(251,191,36,0.35)]'
          : acting
            ? 'border-amber-400/70 bg-amber-400/5'
            : 'border-slate-700 bg-slate-800/60'
      }`}
    >
      <div className="flex items-center gap-1.5">
        {isSB && <Tag color="sky">SB</Tag>}
        {isBB && <Tag color="violet">BB</Tag>}
        <span className="text-lg font-semibold text-slate-100">{name}</span>
        {acting && <Tag color="amber">行动中</Tag>}
      </div>
      <div className="mt-1 flex gap-1">
        {cards.length === 0 ? (
          <>
            <CardView hidden />
            <CardView hidden />
          </>
        ) : (
          cards.slice(0, 2).map((c, i) => (
            <CardView key={i} card={showCards ? c : undefined} hidden={!showCards} highlight={highlightWinner} />
          ))
        )}
      </div>
      <div className="mt-1 text-sm">
        <span className="text-slate-400">筹码 </span>
        <span className="font-mono font-bold text-emerald-300">{chips.toLocaleString()}</span>
      </div>
    </div>
  )
}

function Tag({ children, color }: { children: ReactNode; color: 'sky' | 'violet' | 'amber' }) {
  const cls = {
    sky: 'bg-sky-500/20 text-sky-300',
    violet: 'bg-violet-500/20 text-violet-300',
    amber: 'bg-amber-400 text-slate-900',
  }[color]
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${cls}`}>{children}</span>
}

function CtrlBtn({
  onClick,
  title,
  children,
}: {
  onClick: () => void
  title: string
  children: ReactNode
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className="h-8 w-9 rounded-md border border-slate-600 bg-slate-800 text-slate-200 transition hover:bg-slate-700"
    >
      {children}
    </button>
  )
}

function ActionRow({ a, names, cur }: { a: Action; names: string[]; cur: boolean }) {
  const name = names[a.player_idx] ?? `P${a.player_idx}`
  return (
    <div
      className={`border-b border-slate-800 py-1 ${cur ? 'rounded -mx-1 bg-amber-400/10 px-1' : ''} ${
        a.player_idx === 0 ? 'text-sky-300' : 'text-fuchsia-300'
      }`}
    >
      <span className="text-slate-500">[{STAGE_LABEL[a.stage] ?? a.stage}]</span>{' '}
      <span className="font-semibold">{name}</span>{' '}
      <span className="text-slate-100">{actionLabel(a)}</span>
      {a.pot != null && <span className="text-slate-500"> · 底池 {a.pot.toLocaleString()}</span>}
    </div>
  )
}

function SettleBanner({ snap, names }: { snap: HandSnapshot; names: string[] }) {
  const s = snap.settle!
  const winnerName = s.winner_idx == null ? null : names[s.winner_idx] ?? `P${s.winner_idx}`
  return (
    <div className="mt-3 rounded-lg border border-slate-600 bg-slate-800/80 p-3 text-sm">
      <div className="font-semibold text-slate-200">
        第 {snap.hand} 手结算:
        {winnerName == null ? '平局' : `${winnerName} 赢得底池`}
        <span className="ml-2 font-mono text-amber-300">{(s.pot ?? 0).toLocaleString()}</span>
      </div>
      {s.is_showdown && <div className="mt-1 text-xs text-slate-400">摊牌(showdown)</div>}
      {snap.final_chips && (
        <div className="mt-1 font-mono text-xs text-slate-400">
          终筹:[{snap.final_chips[0]?.toLocaleString()}, {snap.final_chips[1]?.toLocaleString()}]
        </div>
      )}
    </div>
  )
}

/* ══════════════════════════════════════════════════════════
 * 推导当前状态(基于 stepIdx)
 * ══════════════════════════════════════════════════════════
 * snapshot.actions 是本手全部动作(时间序)。stage/community 由
 * build_hand_snapshots 累积进 community(全量)。我们据 stepIdx 推断:
 *  - 当前阶段:最后一个可见动作的 stage(preflop 兜底)
 *  - 公共牌:按阶段截断(preflop=0, flop=3, turn=4, river/showdown=5)
 *  - 当前筹码:最后一个可见动作的 chips_after
 */
function deriveStage(snap: HandSnapshot, stepIdx: number): string {
  const actions = snap.actions.slice(0, stepIdx)
  if (actions.length === 0) return 'preflop'
  return actions[actions.length - 1].stage
}

function deriveChips(snap: HandSnapshot, stepIdx: number): number[] | null {
  const actions = snap.actions.slice(0, stepIdx)
  for (let i = actions.length - 1; i >= 0; i--) {
    const c = actions[i].chips_after
    if (c) return c
  }
  if (stepIdx >= snap.actions.length) return snap.final_chips
  return snap.initial_chips
}

function deriveCommunity(snap: HandSnapshot, stepIdx: number): unknown[] {
  const all = snap.community || []
  const stage = deriveStage(snap, stepIdx)
  const n = stage === 'preflop' ? 0 : stage === 'flop' ? 3 : stage === 'turn' ? 4 : 5
  return all.slice(0, Math.min(n, all.length))
}
