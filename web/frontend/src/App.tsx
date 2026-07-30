import { BrowserRouter as Router, Routes, Route, Navigate } from "react-router";
import { DataProvider } from "./context/DataProvider";
import { ScrollToTop } from "./components/common/ScrollToTop";
import AppLayout from "./layout/AppLayout";
import Overview from "./pages/Overview";
import MatchReplay from "./pages/MatchReplay";
import RatingTrends from "./pages/RatingTrends";
import MatchMatrix from "./pages/MatchMatrix";
import Logs from "./pages/Logs";
import ControlPanel from "./pages/ControlPanel";
import BotManager from "./pages/BotManager";
import PromptEditor from "./pages/PromptEditor";
import NationalArena from "./pages/NationalArena";
import PipelineMap from "./pages/PipelineMap";
import AgentActivity from "./pages/AgentActivity";
import EvidenceGates from "./pages/EvidenceGates";
import FailuresRecovery from "./pages/FailuresRecovery";
import BackgroundStrength from "./pages/BackgroundStrength";
import LlmMetrics from "./pages/LlmMetrics";

/**
 * IA (2026-07 dashboard redesign pass):
 *   /              Overview (slim + PhaseA strip + pipeline link)
 *   /pipeline      sole full stepper + handoff eight-step
 *   /agents        sole research SSE
 *   /bots          Inventory + Manager merge (?v= expand)
 *   /control       start/stop/abandon/async/daemon
 *   /evolution     → /agents
 *   /bots-inventory → /bots
 */
export default function App() {
  return (
    <DataProvider>
      <Router>
        <ScrollToTop />
        <Routes>
          <Route element={<AppLayout />}>
            <Route index path="/" element={<Overview />} />
            <Route path="/pipeline" element={<PipelineMap />} />
            <Route path="/agents" element={<AgentActivity />} />
            <Route path="/evidence" element={<EvidenceGates />} />
            <Route path="/bots-inventory" element={<Navigate to="/bots" replace />} />
            <Route path="/failures" element={<FailuresRecovery />} />
            <Route path="/strength" element={<BackgroundStrength />} />
            <Route path="/llm-metrics" element={<LlmMetrics />} />
            <Route path="/evolution" element={<Navigate to="/agents" replace />} />
            <Route path="/matches" element={<MatchReplay />} />
            <Route path="/arena" element={<NationalArena />} />
            <Route path="/rating-trends" element={<RatingTrends />} />
            <Route path="/match-matrix" element={<MatchMatrix />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/control" element={<ControlPanel />} />
            <Route path="/bots" element={<BotManager />} />
            <Route path="/prompts" element={<PromptEditor />} />
          </Route>
        </Routes>
      </Router>
    </DataProvider>
  );
}
