import React from "react";
import { listWorkers, testWorker } from "../api/client";
import type { WorkerInfo } from "../lib/types";
import { Badge, Button, Card, useToast } from "../components/ui";

const PROVIDER_TONE: Record<string, "ok" | "warn" | "neutral" | "accent"> = {
  terminal: "accent",
  claude: "ok",
  codex: "ok",
  gemini: "ok",
};

export function AgentsPage() {
  const toast = useToast();
  const [rows, setRows] = React.useState<WorkerInfo[]>([]);
  React.useEffect(() => {
    listWorkers().then(setRows).catch(() => setRows([]));
  }, []);

  async function onTest(name: string) {
    try {
      const r = await testWorker(name);
      if (!r.available) toast.push("warn", `${name}: ${r.note || "not available"}`);
      else if (r.tested && r.ok) toast.push("ok", `${name}: stub test passed`);
      else if (r.tested) toast.push("warn", `${name}: test failed`);
      else toast.push("ok", `${name}: available (${r.note})`);
    } catch (e: any) {
      toast.push("bad", `Test failed: ${e.detail || e.message}`);
    }
  }

  return (
    <div className="p-6">
      <h1 className="text-2xl font-semibold tracking-tight mb-1">Agents</h1>
      <div className="text-sm text-ink-3 mb-4">
        Locally available AI workers. Real CLIs require you to already be authenticated.
      </div>
      <div className="grid md:grid-cols-2 gap-3">
        {rows.map((w) => (
          <Card key={w.name}>
            <div className="flex items-start justify-between mb-2">
              <div>
                <div className="font-medium">{w.name}</div>
                <div className="text-xs text-ink-3">role: {w.role}</div>
              </div>
              <Badge tone={PROVIDER_TONE[w.provider]}>{w.provider}</Badge>
            </div>
            <div className="text-sm text-ink-1 space-y-1">
              <div>
                command:{" "}
                <code className="px-1 py-0.5 rounded bg-bg-1 text-xs">{w.command}</code>
              </div>
              <div className="flex gap-1.5">
                <Badge tone={w.is_available ? "ok" : "bad"}>
                  {w.is_available ? "available" : "missing"}
                </Badge>
                {w.can_edit && <Badge>edits files</Badge>}
                {w.worktree && <Badge>worktree</Badge>}
                {w.always_available && <Badge tone="accent">always on</Badge>}
              </div>
              {!w.is_available && (
                <div className="text-xs text-warn">
                  Install/authenticate the CLI to enable.
                </div>
              )}
            </div>
            <div className="mt-3">
              <Button onClick={() => onTest(w.name)}>test worker</Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
