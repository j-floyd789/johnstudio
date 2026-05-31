import React from "react";
import { getDoctor } from "../api/client";
import type { DoctorResponse } from "../lib/types";
import { Card, CodeBlock } from "../components/ui";

export function SettingsPage() {
  const [doctor, setDoctor] = React.useState<DoctorResponse | null>(null);
  React.useEffect(() => {
    getDoctor().then(setDoctor).catch(() => setDoctor(null));
  }, []);

  return (
    <div className="p-6 space-y-4 max-w-3xl">
      <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
      <Card>
        <div className="font-medium mb-2">Backend</div>
        {!doctor ? (
          <div className="text-sm text-ink-3">loading…</div>
        ) : (
          <CodeBlock>
            {`JOHNSTUDIO_HOME: ${doctor.home}\n` +
              `config: ${doctor.config_path}\n` +
              `db: ${doctor.db_path}\n` +
              `FTS5: ${doctor.fts5}\n` +
              `tools:\n${Object.entries(doctor.tools)
                .map(([k, v]) => `  ${k}: ${v ? "ok" : "missing"}`)
                .join("\n")}`}
          </CodeBlock>
        )}
        <div className="text-xs text-ink-3 mt-2">
          To change <code>JOHNSTUDIO_HOME</code>, restart the backend with the env
          var set. Settings editing in-UI is coming soon.
        </div>
      </Card>
      <Card>
        <div className="font-medium mb-2">Coming soon</div>
        <ul className="text-sm text-ink-2 list-disc pl-6 space-y-1">
          <li>edit default max agents / auto-skills behavior in-place</li>
          <li>edit safety lists in-place</li>
          <li>local API token authentication</li>
          <li>Tauri desktop bundle</li>
        </ul>
      </Card>
    </div>
  );
}
