import React from "react";
import {
  addSkillSource,
  disableSkill,
  enableSkill,
  getSkill,
  listSkillSources,
  listSkills,
  scanSkillSources,
} from "../api/client";
import type { SkillDetail, SkillRow, SkillSource } from "../lib/types";
import { Badge, Button, Card, CodeBlock, Empty, Input, useToast } from "../components/ui";
import { Markdown } from "../components/Markdown";

export function SkillsPage() {
  const toast = useToast();
  const [rows, setRows] = React.useState<SkillRow[]>([]);
  const [sources, setSources] = React.useState<SkillSource[]>([]);
  const [filter, setFilter] = React.useState("");
  const [enabledOnly, setEnabledOnly] = React.useState(false);
  const [category, setCategory] = React.useState<string>("");
  const [trust, setTrust] = React.useState<string>("");

  const [selected, setSelected] = React.useState<string | null>(null);
  const [detail, setDetail] = React.useState<SkillDetail | null>(null);
  const [newSource, setNewSource] = React.useState("");

  async function refresh() {
    const all = await listSkills();
    setRows(all);
    setSources(await listSkillSources());
  }
  React.useEffect(() => {
    refresh();
  }, []);

  React.useEffect(() => {
    if (!selected) return;
    getSkill(selected).then(setDetail).catch(() => setDetail(null));
  }, [selected]);

  const categories = Array.from(new Set(rows.map((r) => r.category).filter(Boolean))) as string[];
  const trusts = Array.from(new Set(rows.map((r) => r.trust_level)));

  const visible = rows.filter((r) => {
    if (enabledOnly && !r.enabled) return false;
    if (category && r.category !== category) return false;
    if (trust && r.trust_level !== trust) return false;
    if (!filter) return true;
    const f = filter.toLowerCase();
    return (
      r.skill_id.toLowerCase().includes(f) ||
      (r.name || "").toLowerCase().includes(f) ||
      (r.description || "").toLowerCase().includes(f) ||
      (r.category || "").toLowerCase().includes(f)
    );
  });

  return (
    <div className="p-6">
      <h1 className="text-2xl font-semibold tracking-tight mb-1">Skills</h1>
      <div className="text-sm text-ink-3 mb-4">
        {rows.length} known · {rows.filter((r) => r.enabled).length} enabled
      </div>

      <Card className="mb-4">
        <div className="font-medium mb-2">Skill sources</div>
        <div className="flex gap-2 mb-3">
          <Input
            placeholder="local path or repo URL"
            value={newSource}
            onChange={(e) => setNewSource(e.target.value)}
          />
          <Button
            onClick={async () => {
              if (!newSource) return;
              await addSkillSource(newSource);
              setNewSource("");
              toast.push("ok", "Source added");
              refresh();
            }}
          >
            add
          </Button>
          <Button
            onClick={async () => {
              const r = await scanSkillSources();
              const total = r.reduce((s, x) => s + (x.imported || 0), 0);
              toast.push("ok", `Imported ${total} skills`);
              refresh();
            }}
          >
            scan
          </Button>
        </div>
        {sources.length === 0 ? (
          <div className="text-sm text-ink-3">no sources yet</div>
        ) : (
          <div className="text-sm space-y-1">
            {sources.map((s) => (
              <div key={s.id} className="flex items-center justify-between border-t border-line/40 py-1">
                <span className="font-mono text-xs">
                  {s.local_path || s.repo_url} <Badge>{s.status}</Badge>
                </span>
                <span className="text-xs text-ink-3">{s.last_scanned_at || "never scanned"}</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <div className="flex flex-wrap gap-2 mb-3">
        <Input
          className="max-w-sm"
          placeholder="filter by name, tag, category…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <select
          className="input max-w-[200px]"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        >
          <option value="">all categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <select
          className="input max-w-[200px]"
          value={trust}
          onChange={(e) => setTrust(e.target.value)}
        >
          <option value="">all trust levels</option>
          {trusts.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <label className="text-sm flex items-center gap-2 text-ink-1">
          <input
            type="checkbox"
            checked={enabledOnly}
            onChange={(e) => setEnabledOnly(e.target.checked)}
          />
          enabled only
        </label>
      </div>

      <div className="grid gap-4" style={{ gridTemplateColumns: "1.1fr 1fr" }}>
        <Card className="p-0 overflow-hidden max-h-[70vh] overflow-y-auto">
          {visible.length === 0 ? (
            <Empty title="No skills match." />
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-ink-3 border-b border-line sticky top-0 bg-bg-2">
                <tr>
                  <th className="px-3 py-2">skill</th>
                  <th className="px-3 py-2">category</th>
                  <th className="px-3 py-2">on</th>
                  <th className="px-3 py-2">trust</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((s) => (
                  <tr
                    key={s.skill_id}
                    className={`border-b border-line/40 cursor-pointer ${
                      selected === s.skill_id ? "bg-bg-3" : "hover:bg-bg-3/50"
                    }`}
                    onClick={() => setSelected(s.skill_id)}
                  >
                    <td className="px-3 py-2">
                      <div className="font-medium">{s.skill_id}</div>
                      <div className="text-xs text-ink-3 truncate max-w-[40ch]">
                        {s.description || ""}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-ink-2">{s.category}</td>
                    <td className="px-3 py-2">
                      <Badge tone={s.enabled ? "ok" : "neutral"}>
                        {s.enabled ? "on" : "off"}
                      </Badge>
                    </td>
                    <td className="px-3 py-2">
                      <Badge tone={s.trust_level === "local-curated" ? "accent" : "warn"}>
                        {s.trust_level}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card className="max-h-[70vh] overflow-y-auto">
          {!detail ? (
            <Empty title="Select a skill" body="to view its summary and distilled body." />
          ) : (
            <div>
              <div className="flex items-center justify-between mb-3">
                <div>
                  <div className="font-medium">{detail.skill_id}</div>
                  <div className="text-xs text-ink-3">{detail.category}</div>
                </div>
                <div className="flex gap-2">
                  <Button
                    onClick={async () => {
                      if (detail.enabled) {
                        await disableSkill(detail.skill_id);
                      } else {
                        await enableSkill(detail.skill_id);
                      }
                      refresh();
                      const fresh = await getSkill(detail.skill_id);
                      setDetail(fresh);
                    }}
                  >
                    {detail.enabled ? "disable" : "enable"}
                  </Button>
                </div>
              </div>
              <div className="space-y-3">
                <section>
                  <div className="text-xs uppercase text-ink-3 mb-1">Summary</div>
                  <div className="text-sm text-ink-1 whitespace-pre-wrap">
                    {detail.files?.summary_md || "(no summary)"}
                  </div>
                </section>
                <section>
                  <div className="text-xs uppercase text-ink-3 mb-1">Distilled</div>
                  <Markdown text={detail.files?.distilled_md || ""} />
                </section>
                <section>
                  <div className="text-xs uppercase text-ink-3 mb-1">Metadata</div>
                  <CodeBlock>{detail.files?.metadata_yaml || ""}</CodeBlock>
                </section>
              </div>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
