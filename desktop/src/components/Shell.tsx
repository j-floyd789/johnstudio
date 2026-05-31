import React from "react";
import { NavLink, useNavigate } from "react-router-dom";
import {
  Home,
  Layers,
  Brain,
  GitBranch,
  Shield,
  Cpu,
  Settings,
  Network,
} from "lucide-react";
import { listProjects } from "../api/client";
import type { Project } from "../lib/types";
import { Badge } from "./ui";

const nav = [
  { to: "/", label: "Home", icon: Home },
  { to: "/skills", label: "Skills", icon: Layers },
  { to: "/agents", label: "Agents", icon: Cpu },
  { to: "/safety", label: "Safety", icon: Shield },
  { to: "/settings", label: "Settings", icon: Settings },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const [projects, setProjects] = React.useState<Project[]>([]);
  const navigate = useNavigate();

  React.useEffect(() => {
    listProjects().then(setProjects).catch(() => setProjects([]));
  }, []);

  return (
    <div className="h-screen grid" style={{ gridTemplateColumns: "240px 1fr" }}>
      <aside className="border-r border-line bg-bg-1 p-4 flex flex-col gap-4 overflow-y-auto">
        <div>
          <div className="text-ink-0 font-semibold tracking-tight">JohnStudio</div>
          <div className="text-xs text-ink-3">local AI dev-team</div>
        </div>

        <nav className="flex flex-col gap-1">
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-2 px-2 py-1.5 rounded text-sm ${
                  isActive
                    ? "bg-bg-3 text-ink-0"
                    : "text-ink-2 hover:text-ink-0 hover:bg-bg-3/60"
                }`
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-2">
          <div className="text-xs uppercase tracking-wide text-ink-3 mb-2">Projects</div>
          <div className="flex flex-col gap-1">
            {projects.length === 0 && (
              <div className="text-xs text-ink-3 px-2">none yet</div>
            )}
            {projects.map((p) => (
              <button
                key={p.id}
                onClick={() => navigate(`/p/${p.id}`)}
                className="text-left px-2 py-1.5 rounded text-sm text-ink-1 hover:text-ink-0 hover:bg-bg-3/60 flex items-center justify-between"
              >
                <span className="truncate">{p.name}</span>
                <Badge tone="neutral">{p.base_branch}</Badge>
              </button>
            ))}
          </div>
        </div>

        <div className="mt-auto text-[10px] text-ink-3">
          API: 127.0.0.1:8765 · v0.1.0
        </div>
      </aside>
      <main className="overflow-y-auto">{children}</main>
    </div>
  );
}
