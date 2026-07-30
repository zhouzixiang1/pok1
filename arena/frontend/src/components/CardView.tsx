/** 单张扑克牌渲染组件。
 *
 * 支持多种输入(向前兼容):
 * - 字符串协议格式 ``"<suit,rank>"``(suit0-3=♠♥♦♣,rank0-12=2-A)
 * - 字符串可读格式(如 ``"♠A"``、``"♥10"``)
 * - 后端回放 dict ``{card, text, suit, rank}``(优先用 text/suit)
 * - 整数对 ``[suit, rank]``
 * - 整数(单值 0-51)
 *
 * 红色(♥♦)用 error-600,黑色(♠♣)用 gray-900(浅色牌面)。
 */

export interface CardData {
  card?: string
  text?: string
  suit?: number | null
  rank?: number | null
}

const SUIT_SYMBOL: Record<number, string> = { 0: '♠', 1: '♥', 2: '♦', 3: '♣' }
const RANK_NAME: Record<number, string> = {
  0: '2', 1: '3', 2: '4', 3: '5', 4: '6', 5: '7', 6: '8',
  7: '9', 8: '10', 9: 'J', 10: 'Q', 11: 'K', 12: 'A',
}

export interface ParsedCard {
  suit: number
  rank: number
  symbol: string
  rankName: string
  red: boolean
}

/** 把任意卡牌输入解析成渲染用的字段。无法解析返回 null。 */
export function parseCard(input: unknown): ParsedCard | null {
  if (input == null || input === '') return null

  // dict(后端回放快照格式)
  if (typeof input === 'object' && !Array.isArray(input)) {
    const c = input as CardData
    // 优先用 text 反查(更可靠);失败退回 suit/rank
    if (c.text) {
      const p = parseReadable(c.text)
      if (p) return p
    }
    if (typeof c.suit === 'number' && typeof c.rank === 'number') {
      return makeCard(c.suit, c.rank)
    }
    if (c.card) return parseCard(c.card)
    return null
  }

  if (typeof input === 'string') {
    const s = input.trim()
    // <suit,rank>
    if (s.startsWith('<') && s.endsWith('>') && s.includes(',')) {
      const inner = s.slice(1, -1).trim()
      const [a, b] = inner.split(',').map((x) => parseInt(x.trim(), 10))
      if (!Number.isNaN(a) && !Number.isNaN(b)) return makeCard(a, b)
    }
    // 可读格式 ♠A / ♥10
    return parseReadable(s)
  }

  // [suit, rank]
  if (Array.isArray(input) && input.length === 2) {
    const [a, b] = input.map((x) => parseInt(x, 10))
    if (!Number.isNaN(a) && !Number.isNaN(b)) return makeCard(a, b)
  }

  // 单整数(0-51)
  if (typeof input === 'number') {
    if (input >= 0 && input < 52) return makeCard(Math.floor(input / 13), input % 13)
  }

  return null
}

function parseReadable(s: string): ParsedCard | null {
  for (const [sid, sym] of Object.entries(SUIT_SYMBOL)) {
    if (s.startsWith(sym)) {
      const rest = s.slice(sym.length)
      for (const [rid, rname] of Object.entries(RANK_NAME)) {
        if (rname === rest) {
          return makeCard(Number(sid), Number(rid))
        }
      }
    }
  }
  return null
}

function makeCard(suit: number, rank: number): ParsedCard | null {
  if (!(suit in SUIT_SYMBOL) || !(rank in RANK_NAME)) return null
  return {
    suit,
    rank,
    symbol: SUIT_SYMBOL[suit],
    rankName: RANK_NAME[rank],
    red: suit === 1 || suit === 2,
  }
}

interface CardViewProps {
  /** 卡牌:支持 <suit,rank> / ♠A / {card,text,suit,rank} / [suit,rank] / int */
  card?: unknown
  /** 显示牌背(优先级最高)。 */
  hidden?: boolean
  /** 空槽位(占位)。 */
  empty?: boolean
  /** 尺寸:sm=小(回放器公共牌),md=默认(玩家手牌)。 */
  size?: 'sm' | 'md' | 'lg'
  /** 高亮(当前赢家的牌 / 摊牌)。 */
  highlight?: boolean
  className?: string
}

export default function CardView({
  card,
  hidden,
  empty,
  size = 'md',
  highlight,
  className = '',
}: CardViewProps) {
  const dims =
    size === 'sm'
      ? 'h-10 w-7 text-xs'
      : size === 'lg'
        ? 'h-20 w-14 text-lg'
        : 'h-14 w-10 text-sm'

  if (hidden) {
    return (
      <span
        className={`inline-flex ${dims} items-center justify-center rounded-md border border-gray-400 bg-gradient-to-b from-gray-200 to-gray-100 shadow ${className}`}
      >
        <span className="text-gray-500">🂠</span>
      </span>
    )
  }
  if (empty) {
    return (
      <span
        className={`inline-flex ${dims} items-center justify-center rounded-md border border-dashed border-gray-300/60 text-gray-400 ${className}`}
      >
        —
      </span>
    )
  }
  const c = parseCard(card)
  if (!c) {
    return (
      <span
        className={`inline-flex ${dims} items-center justify-center rounded-md border border-dashed border-gray-300 text-gray-500 ${className}`}
        title={String(card ?? '?')}
      >
        ?
      </span>
    )
  }
  return (
    <span
      className={`inline-flex ${dims} flex-col items-center justify-center rounded-md border bg-white font-bold shadow transition ${
        highlight ? 'border-brand-300 ring-2 ring-brand-300/50' : 'border-gray-300'
      } ${c.red ? 'text-error-600' : 'text-gray-900'} ${className}`}
    >
      <span className="leading-none">{c.rankName}</span>
      <span className="text-base leading-none">{c.symbol}</span>
    </span>
  )
}
