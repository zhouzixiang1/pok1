import { BrowserRouter as Router, Routes, Route, Navigate } from "react-router";
import { DataProvider } from "./context/DataProvider";
import { ScrollToTop } from "./components/common/ScrollToTop";
import AppLayout from "./layout/AppLayout";
import Overview from "./pages/Overview";
import Generation from "./pages/Generation";
import Bots from "./pages/Bots";
import LlmMetrics from "./pages/LlmMetrics";
import ControlPanel from "./pages/ControlPanel";

/**
 * Dashboard redesign (2026-08): 15 pages condensed into 5 user-facing core
 * pages. All prior routes redirect to their merged successor so existing
 * bookmarks and deep links keep working.
 *
 *   /              Overview — 系统健康 + 最新代次进度 + LLM 用量 + 强度卡片
 *   /generation    当代进度 — 完整 stepper + 国赛认证 + LLM 实时流 + handoff 八步
 *   /bots          Bot 强度与回放 — Glicko-2 排行 + H2H 矩阵 + 比赛回放
 *   /llm           LLM 使用分析 — 调用日志 + 输入输出详情 + 按角色聚合
 *   /control       控制面板 — 启停/放弃/daemon 配置/epoch 权威/异步认证队列
 *
 * Legacy → new redirects (compatibility only; sidebar exposes just the 5 above):
 *   /pipeline, /agents, /evidence, /strength   → /generation
 *   /bots-inventory, /matches, /match-matrix,
 *     /arena, /rating-trends                   → /bots
 *   /llm-metrics, /prompts                     → /llm
 *   /logs                                      → /generation
 *   /failures                                  → /control
 *   /evolution                                 → /generation
 */
export default function App() {
  return (
    <DataProvider>
      <Router>
        <ScrollToTop />
        <Routes>
          <Route element={<AppLayout />}>
            {/* 5 core pages */}
            <Route index path="/" element={<Overview />} />
            <Route path="/generation" element={<Generation />} />
            <Route path="/bots" element={<Bots />} />
            <Route path="/llm" element={<LlmMetrics />} />
            <Route path="/control" element={<ControlPanel />} />

            {/* Legacy redirects → merged successors */}
            <Route path="/pipeline" element={<Navigate to="/generation" replace />} />
            <Route path="/agents" element={<Navigate to="/generation" replace />} />
            <Route path="/evolution" element={<Navigate to="/generation" replace />} />
            <Route path="/evidence" element={<Navigate to="/generation" replace />} />
            <Route path="/strength" element={<Navigate to="/generation" replace />} />
            <Route path="/bots-inventory" element={<Navigate to="/bots" replace />} />
            <Route path="/matches" element={<Navigate to="/bots" replace />} />
            <Route path="/match-matrix" element={<Navigate to="/bots" replace />} />
            <Route path="/arena" element={<Navigate to="/bots" replace />} />
            <Route path="/rating-trends" element={<Navigate to="/bots" replace />} />
            <Route path="/llm-metrics" element={<Navigate to="/llm" replace />} />
            <Route path="/logs" element={<Navigate to="/generation" replace />} />
            <Route path="/prompts" element={<Navigate to="/llm" replace />} />
            <Route path="/failures" element={<Navigate to="/control" replace />} />

            {/* Fallback: any unknown path → overview */}
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </Router>
    </DataProvider>
  );
}
