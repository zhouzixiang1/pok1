import { BrowserRouter as Router, Routes, Route } from "react-router";
import AppLayout from "./layout/AppLayout";
import Overview from "./pages/Overview";
import EvolutionMonitor from "./pages/EvolutionMonitor";
import RatingTrends from "./pages/RatingTrends";
import MatchMatrix from "./pages/MatchMatrix";
import Logs from "./pages/Logs";

export default function App() {
  return (
    <Router>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index path="/" element={<Overview />} />
          <Route path="/evolution" element={<EvolutionMonitor />} />
          <Route path="/rating-trends" element={<RatingTrends />} />
          <Route path="/match-matrix" element={<MatchMatrix />} />
          <Route path="/logs" element={<Logs />} />
        </Route>
      </Routes>
    </Router>
  );
}
