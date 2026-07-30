import { Link } from 'react-router-dom'

/** 面向普通用户的平台使用说明(网页 Wiki)。 */
export default function Wiki() {
  return (
    <div className="mx-auto max-w-3xl p-4 pb-12">
      <header className="mb-6">
        <h1 className="mb-1 text-2xl font-bold text-brand-500">用户使用说明</h1>
        <p className="text-sm text-gray-500">
          pok-arena：上传德州扑克 bot、发起对战、实时观赛与天梯排行。下面按使用顺序说明。
        </p>
      </header>

      <nav className="mb-6 rounded-xl border border-gray-200 bg-white/60 p-4 text-sm text-gray-700">
        <p className="mb-2 font-semibold text-gray-900">目录</p>
        <ol className="ml-4 list-decimal space-y-1">
          <li><a href="#flow" className="text-brand-500 hover:underline">整体流程</a></li>
          <li><a href="#register" className="text-brand-500 hover:underline">注册与邮箱验证</a></li>
          <li><a href="#login" className="text-brand-500 hover:underline">登录与找回密码</a></li>
          <li><a href="#bot" className="text-brand-500 hover:underline">上传 Bot</a></li>
          <li><a href="#challenge" className="text-brand-500 hover:underline">发起对战</a></li>
          <li><a href="#watch" className="text-brand-500 hover:underline">观赛与回放</a></li>
          <li><a href="#rank" className="text-brand-500 hover:underline">排行榜与历史</a></li>
          <li><a href="#rules" className="text-brand-500 hover:underline">对局规则摘要</a></li>
          <li><a href="#dev" className="text-brand-500 hover:underline">Bot 协议速查</a></li>
          <li><a href="#faq" className="text-brand-500 hover:underline">常见问题</a></li>
        </ol>
      </nav>

      <Section id="flow" title="① 整体流程">
        <ol className="ml-4 list-decimal space-y-2 text-gray-700">
          <li>
            <Link to="/register" className="text-brand-500 hover:underline">注册</Link>
            （填图形验证码）→ 查收邮件 →
            <Link to="/verify-email" className="text-brand-500 hover:underline">验证邮箱</Link>
          </li>
          <li>
            <Link to="/login" className="text-brand-500 hover:underline">登录</Link>
            （再次填写图形验证码）
          </li>
          <li>
            在 <Link to="/my-bots" className="text-brand-500 hover:underline">我的 Bot</Link> 上传源码包并等待构建完成
          </li>
          <li>
            在 <Link to="/challenge" className="text-brand-500 hover:underline">发起对战</Link> 选对手开打
          </li>
          <li>
            在观赛页看实时牌局，结束后可在对局详情里回放；到
            <Link to="/leaderboard" className="ml-1 text-brand-500 hover:underline">排行榜</Link> 看评分
          </li>
        </ol>
      </Section>

      <Section id="register" title="② 注册与邮箱验证">
        <ol className="ml-4 list-decimal space-y-2">
          <li>
            打开 <Link to="/register" className="text-brand-500 hover:underline">注册页</Link>，填写：
            <ul className="mt-1 ml-4 list-disc space-y-1 text-gray-700">
              <li>用户名：3–32 字符，字母开头，仅字母/数字/下划线</li>
              <li>邮箱：用于收验证码与找回密码（请填真实可收信邮箱）</li>
              <li>密码：至少 8 位；可填昵称（支持中文）</li>
              <li>图形验证码：看图中字符或算术结果；看不清可点击图片刷新</li>
            </ul>
          </li>
          <li>提交后，系统会向邮箱发送 <strong className="text-gray-900">6 位数字验证码</strong>（请检查垃圾箱）。</li>
          <li>
            在 <Link to="/verify-email" className="text-brand-500 hover:underline">验证邮箱</Link> 页填入验证码完成验证。
            未验证账号<strong className="text-gray-900">无法登录</strong>。
          </li>
          <li>若未收到邮件：在验证页填写图形验证码后点「重发验证码」。</li>
        </ol>
      </Section>

      <Section id="login" title="③ 登录与找回密码">
        <ul className="ml-4 list-disc space-y-2">
          <li>
            <Link to="/login" className="text-brand-500 hover:underline">登录</Link>
            ：用户名 + 密码 + 图形验证码。登录后可上传 bot、发起对战。
          </li>
          <li>
            忘记密码：走
            <Link to="/reset-password" className="mx-1 text-brand-500 hover:underline">找回密码</Link>
            → 填用户名/邮箱与图形验证码 → 查收邮件中的重置码 → 设置新密码。
          </li>
          <li>右上角显示昵称可进入个人页；点「登出」结束会话。</li>
        </ul>
      </Section>

      <Section id="bot" title="④ 上传 Bot">
        <p className="mb-2">
          进入 <Link to="/my-bots" className="text-brand-500 hover:underline">我的 Bot</Link>，点击上传，准备：
        </p>
        <ul className="ml-4 list-disc space-y-1 text-gray-700">
          <li>
            <code className="text-brand-500">.zip</code> 源码包（建议根目录直接放入口文件，勿套多余顶层空目录）
          </li>
          <li>Bot 名称（同一账号内唯一）</li>
          <li>
            协议：
            <code className="text-brand-500">json</code>（stdin/stdout，推荐新写）或
            <code className="ml-1 text-brand-500">tcp</code>（国赛 socket，已有代码可零改上传）
          </li>
          <li>
            入口文件，例如 <code className="text-brand-500">main.py</code> /
            <code className="text-brand-500">national_bot.py</code>
          </li>
        </ul>
        <p className="mt-3 text-gray-700">
          上传后平台会在 Docker 沙箱中构建镜像。构建成功即可对战；失败会提示错误，可修改后重新上传版本。
        </p>
        <p className="mt-2 text-xs text-gray-500">
          限制：zip ≤ 50MB；解压后体积与文件数有上限；路径不允许穿越。运行时容器无外网、内存/CPU 受限。
        </p>
      </Section>

      <Section id="challenge" title="⑤ 发起对战">
        <ol className="ml-4 list-decimal space-y-2">
          <li>
            打开 <Link to="/challenge" className="text-brand-500 hover:underline">发起对战</Link>
          </li>
          <li>选择「我的 bot」与对手（他人公开 bot 或平台内置 national_v*）</li>
          <li>点开打后进入该局实时观赛；后台在 Docker 中跑双方程序</li>
        </ol>
        <p className="mt-3 text-xs text-gray-500">
          平台有并发对战上限。若提示「对战已满」，稍后再试。每场默认约 70 手。
        </p>
      </Section>

      <Section id="watch" title="⑥ 观赛与回放">
        <ul className="ml-4 list-disc space-y-2">
          <li>
            <Link to="/" className="text-brand-500 hover:underline">观赛大厅</Link>
            ：查看进行中的对局（SSE 实时推送）
          </li>
          <li>
            对局详情页：图形化回放，可逐手/逐步、拖动进度、自动播放；摊牌时显示双方手牌
          </li>
          <li>
            <Link to="/history" className="text-brand-500 hover:underline">对局历史</Link>
            ：按条件浏览已完成对局
          </li>
        </ul>
      </Section>

      <Section id="rank" title="⑦ 排行榜与个人页">
        <ul className="ml-4 list-disc space-y-2">
          <li>
            <Link to="/leaderboard" className="text-brand-500 hover:underline">排行榜</Link>
            ：默认 Glicko-2（rating 越高越强，RD 越小越可信）；可切「净筹码榜」
          </li>
          <li>点击 bot / 用户可进入详情，查看战绩与最近对局</li>
        </ul>
      </Section>

      <Section id="rules" title="⑧ 对局规则摘要">
        <ul className="ml-4 list-disc space-y-1 text-gray-700">
          <li>单挑德州；每手复位筹码；盲注与手数按平台配置（常见 70 手一场）</li>
          <li>每个动作有决策时限；超时或非法动作按弃牌处理</li>
          <li>程序崩溃 / 容器异常可能导致该方判负或对局中止，以对局详情中的结果为准</li>
        </ul>
      </Section>

      <Section id="dev" title="⑨ Bot 协议速查（开发者）">
        <p className="mb-2">JSON 模式：引擎经 stdin 下发，bot 经 stdout 回动作。</p>
        <Pre>{`// 引擎 → bot（示意）
{ "type": "action_request", "to_call": 0, "min_raise": 200,
  "pot": 150, "hole_cards": ["<0,12>", "<1,11>"] }

// bot → 引擎
{ "action": "call" }
{ "action": "raise", "amount": 400 }   // fold|check|call|raise|allin`}</Pre>
        <p className="mt-2 text-xs text-gray-500">
          卡牌 <code className="text-brand-500">&lt;suit,rank&gt;</code>：suit 0–3 = ♠♥♦♣，rank 0–12 = 2–A。
          TCP 国赛协议 bot 可直接上传，容器内由平台桥接，代码通常无需修改。完整规范见仓库
          <code className="mx-1 text-brand-500">docs/PLATFORM_WIKI.md</code>。
        </p>
      </Section>

      <Section id="faq" title="⑩ 常见问题">
        <Faq q="注册成功但登不进去？">
          多半是邮箱未验证。到「验证邮箱」页填邮件中的 6 位码；可点重发（需先过图形验证码）。
        </Faq>
        <Faq q="收不到邮件？">
          检查垃圾箱/广告箱；确认邮箱地址拼写；稍后重发。仍无则联系管理员查看发信记录。
        </Faq>
        <Faq q="图形验证码总是错？">
          算术题填运算结果（数字）；字母题不区分大小写。点图片刷新后再试。
        </Faq>
        <Faq q="Bot 构建失败？">
          确认 zip 内入口文件路径与填写一致；依赖需在 Dockerfile 可解析范围内；查看上传页错误信息后改包重传。
        </Faq>
        <Faq q="开打提示对战已满？">
          当前并发场次已达上限，等其他对局结束后再发起。
        </Faq>
        <Faq q="未登录能看什么？">
          可浏览观赛、历史、排行榜；上传 bot 与发起对战需要登录。
        </Faq>
      </Section>

      <div className="mt-8 rounded-xl border border-gray-200 bg-gray-50 p-4 text-center text-xs text-gray-500">
        pok-arena · 用户使用说明 · 如有问题请联系平台管理员
      </div>
    </div>
  )
}

function Section({
  id,
  title,
  children,
}: {
  id?: string
  title: string
  children: React.ReactNode
}) {
  return (
    <section id={id} className="mb-5 scroll-mt-20 rounded-xl border border-gray-200 bg-white p-4 shadow-theme-sm">
      <h2 className="mb-2 text-lg font-bold text-gray-900">{title}</h2>
      <div className="text-sm text-gray-700">{children}</div>
    </section>
  )
}

function Faq({ q, children }: { q: string; children: React.ReactNode }) {
  return (
    <div className="mb-3 border-b border-gray-200/80 pb-3 last:mb-0 last:border-0 last:pb-0">
      <p className="mb-1 font-semibold text-gray-900">{q}</p>
      <div className="text-gray-500">{children}</div>
    </div>
  )
}

function Pre({ children }: { children: React.ReactNode }) {
  return (
    <pre className="overflow-x-auto rounded-lg bg-gray-50/70 p-3 font-mono text-xs text-gray-700">
      {children}
    </pre>
  )
}
