import { BrowserRouter as Router, Routes, Route } from "react-router";
import { DataProvider } from "./context/DataProvider";
import { ScrollToTop } from "./components/common/ScrollToTop";
import AppLayout from "./layout/AppLayout";
import Overview from "./pages/Overview";
import EvolutionMonitor from "./pages/EvolutionMonitor";
import MatchReplay from "./pages/MatchReplay";
import RatingTrends from "./pages/RatingTrends";
import MatchMatrix from "./pages/MatchMatrix";
import Logs from "./pages/Logs";
import ControlPanel from "./pages/ControlPanel";
import BotManager from "./pages/BotManager";
import PromptEditor from "./pages/PromptEditor";
import NationalArena from "./pages/NationalArena";
// New structured views from the dashboard redesign.  Each consumes the shared
// normalization layer (lib/, domain/) and the paired /api/control/health
// observation; none re-derives a stage, route, or identity from a single
// field.  Legacy routes (/evolution, /bots) remain for backward compatibility
// and because test_frontend_contract_closure.py guards their content.
import PipelineMap from "./pages/PipelineMap";
import AgentActivity from "./pages/AgentActivity";
import EvidenceGates from "./pages/EvidenceGates";
import BotInventory from "./pages/BotInventory";
import FailuresRecovery from "./pages/FailuresRecovery";
import BackgroundStrength from "./pages/BackgroundStrength";
import LlmMetrics from "./pages/LlmMetrics";

export default function App() {
  return (
    <DataProvider>
      <Router>
        <ScrollToTop />
        <Routes>
          <Route element={<AppLayout />}>
            {/* Command Center / overview (enhanced, retains guarded strings) */}
            <Route index path="/" element={<Overview />} />
            {/* New structured views */}
            <Route path="/pipeline" element={<PipelineMap />} />
            <Route path="/agents" element={<AgentActivity />} />
            <Route path="/evidence" element={<EvidenceGates />} />
            <Route path="/bots-inventory" element={<BotInventory />} />
            <Route path="/failures" element={<FailuresRecovery />} />
            <Route path="/strength" element={<BackgroundStrength />} />
            <Route path="/llm-metrics" element={<LlmMetrics />} />
            {/* Legacy / compatibility routes (guarded by contract tests) */}
            <Route path="/evolution" element={<EvolutionMonitor />} />
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
