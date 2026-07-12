import { HashRouter, Routes, Route, NavLink, Link } from 'react-router-dom'
import Admin from './pages/Admin'
import ArenaTable from './pages/ArenaTable'
import History from './pages/History'
import Leaderboard from './pages/Leaderboard'
import Login from './pages/Login'
import MatchDetail from './pages/MatchDetail'
import UserProfile from './pages/UserProfile'

export default function App() {
  const cls = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-1 rounded text-sm ${isActive ? 'bg-amber-400 font-bold text-slate-900' : 'text-slate-300 hover:bg-slate-700'}`
  return (
    <HashRouter>
      <header className="sticky top-0 z-10 flex flex-wrap items-center gap-2 border-b border-slate-700 bg-slate-900/90 px-4 py-2 backdrop-blur">
        <Link to="/" className="mr-2 text-lg font-bold text-slate-100">pok-arena</Link>
        <NavLink to="/" end className={cls}>观赛</NavLink>
        <NavLink to="/leaderboard" className={cls}>天梯</NavLink>
        <NavLink to="/history" className={cls}>历史</NavLink>
        <NavLink to="/admin" className={cls}>管理</NavLink>
      </header>
      <Routes>
        <Route path="/" element={<ArenaTable />} />
        <Route path="/leaderboard" element={<Leaderboard />} />
        <Route path="/user/:name" element={<UserProfile />} />
        <Route path="/history" element={<History />} />
        <Route path="/match/:id" element={<MatchDetail />} />
        <Route path="/admin" element={<Admin />} />
        <Route path="/login" element={<Login />} />
      </Routes>
    </HashRouter>
  )
}
