import { Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { ToastProvider } from "./components/ui";
import { HomePage } from "./pages/HomePage";
import { ProjectPage } from "./pages/ProjectPage";
import { TaskPage } from "./pages/TaskPage";
import { ChainPage } from "./pages/ChainPage";
import { GraphPage } from "./pages/GraphPage";
import { TeamPage } from "./pages/TeamPage";
import { SkillsPage } from "./pages/SkillsPage";
import { AgentsPage } from "./pages/AgentsPage";
import { SafetyPage } from "./pages/SafetyPage";
import { SettingsPage } from "./pages/SettingsPage";

export default function App() {
  return (
    <ToastProvider>
      <Shell>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/p/:id" element={<ProjectPage />} />
          <Route path="/p/:id/t/:n" element={<TaskPage />} />
          <Route path="/p/:id/c/:n" element={<ChainPage />} />
          <Route path="/p/:id/graph" element={<GraphPage />} />
          <Route path="/p/:id/team/:n" element={<TeamPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/safety" element={<SafetyPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </Shell>
    </ToastProvider>
  );
}
