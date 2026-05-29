import { BrowserRouter as Router, Routes, Route } from "react-router";
import AppLayout from "./layout/AppLayout";
import Overview from "./pages/Overview";
import EvolutionMonitor from "./pages/EvolutionMonitor";
import MatchReplay from "./pages/MatchReplay";
import RatingTrends from "./pages/RatingTrends";
import MatchMatrix from "./pages/MatchMatrix";
import Logs from "./pages/Logs";
import ControlPanel from "./pages/ControlPanel";
import BotManager from "./pages/BotManager";
import ExperiencePool from "./pages/ExperiencePool";
import PromptEditor from "./pages/PromptEditor";

export default function App() {
  return (
    <Router>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index path="/" element={<Overview />} />
          <Route path="/evolution" element={<EvolutionMonitor />} />
          <Route path="/matches" element={<MatchReplay />} />
          <Route path="/rating-trends" element={<RatingTrends />} />
          <Route path="/match-matrix" element={<MatchMatrix />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/control" element={<ControlPanel />} />
          <Route path="/bots" element={<BotManager />} />
          <Route path="/experience" element={<ExperiencePool />} />
          <Route path="/prompts" element={<PromptEditor />} />
        </Route>
      </Routes>
    </Router>
  );
}
