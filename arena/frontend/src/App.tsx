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
import VerifyEmail from './pages/VerifyEmail'
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
    `px-3 py-2 rounded-lg text-sm font-medium transition ${
      isActive
        ? 'bg-brand-50 text-brand-500'
        : 'text-gray-600 hover:bg-gray-100 hover:text-gray-900'
    }`

  const { user, isLoggedIn, loading, logout } = useAuth()
  const nav = useNavigate()
  useLocation()

  const onLogout = async () => {
    await logout()
    nav('/')
  }

  return (
    <div className="min-h-screen bg-gray-50 font-[family-name:var(--font-outfit)] text-gray-800">
      {/* TailAdmin 风格顶栏：白底 + 细边框 + 轻阴影 */}
      <header className="sticky top-0 z-50 border-b border-gray-200 bg-white shadow-theme-sm">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-2 px-4 py-3 lg:px-6">
          <Link to="/" className="mr-2 flex items-center gap-2 text-lg font-semibold text-gray-900">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-500 text-base text-white">
              ♠
            </span>
            <span>pok-arena</span>
          </Link>
          <nav className="flex flex-wrap items-center gap-1">
            <NavLink to="/" end className={cls}>观赛</NavLink>
            <NavLink to="/challenge" className={cls}>发起对战</NavLink>
            <NavLink to="/leaderboard" className={cls}>排行榜</NavLink>
            <NavLink to="/history" className={cls}>对局历史</NavLink>
            <NavLink to="/my-bots" className={cls}>我的 Bot</NavLink>
            <NavLink to="/wiki" className={cls}>使用说明</NavLink>
            {user?.role === 'admin' && (
              <NavLink to="/admin" className={cls}>管理</NavLink>
            )}
          </nav>
          <div className="ml-auto flex items-center gap-2">
            {loading ? (
              <span className="text-xs text-gray-400">…</span>
            ) : isLoggedIn && user ? (
              <>
                <Link
                  to={`/user/${encodeURIComponent(user.username)}`}
                  className="rounded-lg px-3 py-2 text-sm text-gray-700 hover:bg-gray-100"
                >
                  <span className="font-medium text-brand-500">
                    {user.display_name || user.username}
                  </span>
                  {user.role === 'admin' && (
                    <span className="ml-1 rounded-md bg-error-50 px-1.5 py-0.5 text-[10px] font-medium text-error-600">
                      admin
                    </span>
                  )}
                </Link>
                <button
                  onClick={onLogout}
                  className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50"
                >
                  登出
                </button>
              </>
            ) : (
              <>
                <NavLink to="/login" className={cls}>登录</NavLink>
                <Link
                  to="/register"
                  className="rounded-lg bg-brand-500 px-3 py-2 text-sm font-medium text-white shadow-theme-xs hover:bg-brand-600"
                >
                  注册
                </Link>
              </>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto min-h-[calc(100vh-8rem)] max-w-7xl">
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
          <Route path="/verify-email" element={<VerifyEmail />} />
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/admin" element={<Admin />} />
        </Routes>
      </main>
      <footer className="border-t border-gray-200 bg-white px-4 py-4 text-center text-xs text-gray-400">
        pok-arena · 德州扑克对战平台
      </footer>
    </div>
  )
}
