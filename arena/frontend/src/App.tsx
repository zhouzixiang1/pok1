import { HashRouter, Routes, Route, NavLink, Link, useNavigate, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from './components/useAuth'
import Admin from './pages/Admin'
import ArenaTable from './pages/ArenaTable'
import Challenge from './pages/Challenge'
import History from './pages/History'
import Leaderboard from './pages/Leaderboard'
import Login from './pages/Login'
import MatchDetail from './pages/MatchDetail'
import MyBots from './pages/MyBots'
import Register from './pages/Register'
import ResetPassword from './pages/ResetPassword'
import UserProfile from './pages/UserProfile'
import Wiki from './pages/Wiki'

export default function App() {
  return (
    <AuthProvider>
      <HashRouter>
        <Shell />
      </HashRouter>
    </AuthProvider>
  )
}

function Shell() {
  const cls = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-1.5 rounded-md text-sm transition ${
      isActive
        ? 'bg-amber-400 font-bold text-slate-900'
        : 'text-slate-300 hover:bg-slate-700/60'
    }`

  const { user, isLoggedIn, loading, logout } = useAuth()
  const nav = useNavigate()
  useLocation() // 触发组件在路由变化时重渲染(刷新用户状态显示)

  const onLogout = async () => {
    await logout()
    nav('/')
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="sticky top-0 z-20 flex flex-wrap items-center gap-2 border-b border-slate-700 bg-slate-900/90 px-4 py-2.5 backdrop-blur">
        <Link to="/" className="mr-3 flex items-center gap-2 text-lg font-bold text-amber-300">
          <span className="text-xl">♠</span>
          <span>pok-arena</span>
        </Link>
        <nav className="flex flex-wrap items-center gap-1">
          <NavLink to="/" end className={cls}>观赛</NavLink>
          <NavLink to="/challenge" className={cls}>发起对战</NavLink>
          <NavLink to="/leaderboard" className={cls}>排行榜</NavLink>
          <NavLink to="/history" className={cls}>对局历史</NavLink>
          <NavLink to="/my-bots" className={cls}>我的Bot</NavLink>
          <NavLink to="/wiki" className={cls}>Wiki</NavLink>
          {user?.role === 'admin' && (
            <NavLink to="/admin" className={cls}>管理</NavLink>
          )}
        </nav>
        <div className="ml-auto flex items-center gap-2">
          {loading ? (
            <span className="text-xs text-slate-500">…</span>
          ) : isLoggedIn && user ? (
            <>
              <Link
                to={`/user/${encodeURIComponent(user.username)}`}
                className="rounded-md px-2 py-1 text-sm text-slate-200 hover:bg-slate-700/60"
              >
                <span className="text-amber-300">{user.display_name || user.username}</span>
                {user.role === 'admin' && (
                  <span className="ml-1 rounded bg-rose-500/30 px-1 text-[10px] text-rose-300">admin</span>
                )}
              </Link>
              <button
                onClick={onLogout}
                className="rounded-md border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
              >
                登出
              </button>
            </>
          ) : (
            <>
              <NavLink to="/login" className={cls}>登录</NavLink>
              <NavLink to="/register" className={cls}>注册</NavLink>
            </>
          )}
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<ArenaTable />} />
          <Route path="/leaderboard" element={<Leaderboard />} />
          <Route path="/history" element={<History />} />
          <Route path="/my-bots" element={<MyBots />} />
          <Route path="/challenge" element={<Challenge />} />
          <Route path="/user/:name" element={<UserProfile />} />
          <Route path="/match/:id" element={<MatchDetail />} />
          <Route path="/wiki" element={<Wiki />} />
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/admin" element={<Admin />} />
        </Routes>
      </main>
      <footer className="border-t border-slate-800 px-4 py-3 text-center text-xs text-slate-600">
        pok-arena · 德州扑克对战平台 · Web 50280 / TCP 50101
      </footer>
    </div>
  )
}
