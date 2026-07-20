import { Link } from 'react-router-dom'

export default function Wiki() {
  return (
    <div className="mx-auto max-w-3xl p-4">
      <h1 className="mb-1 text-2xl font-bold text-amber-300">Wiki · 平台说明</h1>
      <p className="mb-6 text-sm text-slate-400">如何在 pok-arena 上对战你的德州扑克 bot</p>

      <Section title="① 注册账号">
        <p>
          先去 <Link to="/register" className="text-amber-300 hover:underline">注册</Link> 一个账号(用户名
          ≥3 字符,密码 ≥8 位)。注册后即可上传 bot、发起对战、看排行。
        </p>
      </Section>

      <Section title="② 上传 Bot">
        <p>
          在 <Link to="/my-bots" className="text-amber-300 hover:underline">我的 Bot</Link> 页面点
          「上传新 Bot」。需要提供:
        </p>
        <ul className="ml-4 list-disc space-y-1 text-slate-300">
          <li><code className="text-amber-300">.zip</code> 源码包(根目录含入口文件)</li>
          <li>bot 名(同一用户内唯一)</li>
          <li>协议:<code className="text-amber-300">json</code>(stdin/stdout)或 <code className="text-amber-300">tcp</code>(socket)</li>
          <li>入口文件名(如 <code className="text-amber-300">main.py</code>)</li>
        </ul>
        <p className="mt-2">
          上传后会自动 docker build 出镜像。失败会回滚(bot 记录删除)。构建成功后即可发起对战。
        </p>
      </Section>

      <Section title="③ 发起对战">
        <p>
          去 <Link to="/challenge" className="text-amber-300 hover:underline">发起对战</Link>,
          选你的 bot + 选一个公开 bot(含内置),点「开打」。开打后自动跳转到该对局的实时观赛页。
        </p>
      </Section>

      <Section title="④ 实时观赛 / 回放">
        <ul className="ml-4 list-disc space-y-1 text-slate-300">
          <li><Link to="/" className="text-amber-300 hover:underline">观赛大厅</Link>:订阅进行中比赛,实时 SSE 推送</li>
          <li><Link to="/match/:id" className="text-amber-300 hover:underline">对局详情</Link>:图形化回放器,逐手逐步推进,自动播放</li>
        </ul>
        <p className="mt-2 text-xs text-slate-500">
          回放器:⏮◀▶⏭ 控制手/步,滑块拖动定位,自动播放可调速度。摊牌时显示对手手牌。
        </p>
      </Section>

      <Section title="⑤ 排行榜">
        <p>
          <Link to="/leaderboard" className="text-amber-300 hover:underline">排行榜</Link> 用 Glicko-2 评分
          (rating 越高越强,RD 越小越可信),可切到「净筹码榜」按净筹码排序。
        </p>
      </Section>

      <Section title="协议规范(json 模式)">
        <p className="mb-2">每手引擎通过 stdin 发送 JSON,bot 通过 stdout 返回 JSON 动作。</p>
        <Pre>{`// 引擎 → bot(每手每动作点)
{ "type": "new_hand", "hand": 1, "sb": 0, "bb": 1,
  "my_chips": 19990, "opp_chips": 20010,
  "hole_cards": ["<0,12>", "<1,12>"] }

{ "type": "stage", "stage": "flop",
  "community": ["<2,5>", "<3,8>", "<0,0>"] }

{ "type": "action_request", "to_call": 0, "min_raise": 20,
  "pot": 30, "deadline_ms": 1000 }

// bot → 引擎
{ "action": "call" }     // fold | check | call | raise <amt> | allin
{ "action": "raise", "amount": 60 }`}</Pre>
        <p className="mt-2 text-xs text-slate-500">
          卡牌格式:<code className="text-amber-300">&lt;suit,rank&gt;</code>(suit 0-3=♠♥♦♣,rank 0-12=2-A)。
          超时 / 非法动作 → 自动弃牌。
        </p>
      </Section>

      <Section title="API 参考">
        <ul className="ml-4 list-disc space-y-1 font-mono text-xs text-slate-300">
          <li>POST /api/auth/register · login · logout</li>
          <li>GET /api/auth/me(需登录)</li>
          <li>POST /api/bots(multipart 上传)· GET /api/bots?scope=mine|public</li>
          <li>POST /api/matches/challenge</li>
          <li>GET /api/matches · /api/matches/:id · /api/matches/:id/replay</li>
          <li>GET /api/matches/:id/events(SSE 观赛)</li>
          <li>GET /api/leaderboard · /api/leaderboard/by-chips</li>
        </ul>
        <p className="mt-2 text-xs text-slate-500">
          鉴权:登录后存 <code className="text-amber-300">arena_user_token</code>,请求带
          <code className="text-amber-300"> Authorization: Bearer</code>,或同源 cookie。
        </p>
      </Section>

      <div className="mt-8 rounded-xl border border-slate-700 bg-slate-800/40 p-4 text-center text-xs text-slate-500">
        pok-arena · Web 50280 / TCP 50101 · 后端 FastAPI
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mb-5 rounded-xl border border-slate-700 bg-slate-800/60 p-4">
      <h2 className="mb-2 text-lg font-bold text-slate-100">{title}</h2>
      <div className="text-sm text-slate-300">{children}</div>
    </section>
  )
}

function Pre({ children }: { children: React.ReactNode }) {
  return (
    <pre className="overflow-x-auto rounded-lg bg-slate-950/70 p-3 font-mono text-xs text-slate-300">
      {children}
    </pre>
  )
}
