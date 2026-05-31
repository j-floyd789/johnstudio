import React from "react";
import { getDoctor } from "../api/client";
import type { DoctorResponse } from "../lib/types";
import { Card, CodeBlock } from "../components/ui";

// The doctor endpoint already includes safety-relevant data via the global config
// (workers, tools, fts5). For the protected-paths and dangerous-commands lists we
// surface the canonical values that the backend enforces.
const PROTECTED_PATHS = [
  ".env", ".env.*", "**/*.pem", "**/*.key",
  "~/.ssh/**", "~/.aws/**", "~/.config/gcloud/**",
];
const DANGEROUS = [
  "rm -rf", "sudo", "curl | bash", "wget | bash", "git push --force", "chmod -R 777",
];
const APPROVAL = [
  "npm install", "pnpm install", "pip install", "brew install", "docker compose up", "git push",
];

export function SafetyPage() {
  const [doctor, setDoctor] = React.useState<DoctorResponse | null>(null);
  React.useEffect(() => {
    getDoctor().then(setDoctor).catch(() => setDoctor(null));
  }, []);

  return (
    <div className="p-6 space-y-4 max-w-4xl">
      <h1 className="text-2xl font-semibold tracking-tight">Safety</h1>
      <Card>
        <div className="font-medium mb-2">Protected paths</div>
        <CodeBlock>{PROTECTED_PATHS.join("\n")}</CodeBlock>
        <div className="text-xs text-ink-3 mt-2">
          Any change to a file matching these patterns is flagged by the collector and
          subtracts heavily from the worker's review score.
        </div>
      </Card>
      <Card>
        <div className="font-medium mb-2">Dangerous commands</div>
        <CodeBlock>{DANGEROUS.join("\n")}</CodeBlock>
        <div className="text-xs text-ink-3 mt-2">
          Their appearance in RESULT.md or in the diff triggers a safety flag and can
          block a merge recommendation.
        </div>
      </Card>
      <Card>
        <div className="font-medium mb-2">Approval-required commands</div>
        <CodeBlock>{APPROVAL.join("\n")}</CodeBlock>
        <div className="text-xs text-ink-3 mt-2">
          Flagged but non-blocking. Always confirm with a human before running.
        </div>
      </Card>
      {doctor && (
        <Card>
          <div className="font-medium mb-2">Backend environment</div>
          <CodeBlock>
            {`home: ${doctor.home}\nconfig: ${doctor.config_path}\ndb: ${doctor.db_path}\nFTS5: ${doctor.fts5}`}
          </CodeBlock>
        </Card>
      )}
    </div>
  );
}
