// Unit tests for the live-tree extractors.
//
// These are pure functions over the worker_event log, so they're easy to
// exercise with hand-built event fixtures that mirror the stream-json shapes
// the backend stores under raw_json.

import { describe, expect, it } from "vitest";

import {
  NEUTRAL_PALETTE,
  buildSubagentTree,
  countToolCalls,
  extractSubagents,
  extractTaskToolSubagents,
  mergeSubagents,
  paletteForVp,
  pickActivitySummary,
} from "../extractors";
import type { Subagent, WorkerEvent } from "../types";

let nextId = 1;
function ev(partial: Partial<WorkerEvent> & { kind: string }): WorkerEvent {
  const id = partial.id ?? nextId++;
  return {
    id,
    run_id: partial.run_id ?? 1,
    task_id: partial.task_id ?? null,
    phase_id: partial.phase_id ?? null,
    seq: partial.seq ?? id,
    ts: partial.ts ?? `2026-05-30T00:00:${String(id).padStart(2, "0")}Z`,
    kind: partial.kind,
    summary: partial.summary ?? "",
    raw_json: partial.raw_json,
  };
}

function spawnEvent(
  toolUseId: string,
  subagentType: string,
  brief: string,
  parentToolUseId?: string,
): WorkerEvent {
  return ev({
    kind: "spawn:subagent",
    summary: `${subagentType} · ${brief}`,
    raw_json: JSON.stringify({
      type: "assistant",
      ...(parentToolUseId ? { parent_tool_use_id: parentToolUseId } : {}),
      message: {
        content: [
          {
            type: "tool_use",
            name: "Task",
            id: toolUseId,
            input: { subagent_type: subagentType, prompt: brief },
          },
        ],
      },
    }),
  });
}

function resultEvent(toolUseId: string, body: string, isError = false): WorkerEvent {
  return ev({
    kind: "tool_result",
    summary: body.slice(0, 40),
    raw_json: JSON.stringify({
      type: "user",
      message: {
        content: [
          { type: "tool_result", tool_use_id: toolUseId, content: body, is_error: isError },
        ],
      },
    }),
  });
}

describe("paletteForVp", () => {
  it("returns the neutral palette for falsy / unknown VPs", () => {
    expect(paletteForVp(null)).toBe(NEUTRAL_PALETTE);
    expect(paletteForVp(undefined)).toBe(NEUTRAL_PALETTE);
    expect(paletteForVp("")).toBe(NEUTRAL_PALETTE);
    expect(paletteForVp("nope_vp")).toBe(NEUTRAL_PALETTE);
  });

  it("returns a distinct stripe per known VP", () => {
    const claude = paletteForVp("claude_vp");
    const codex = paletteForVp("codex_vp");
    const gemini = paletteForVp("gemini_vp");
    expect(claude.stripe).not.toBe(NEUTRAL_PALETTE.stripe);
    expect(new Set([claude.stripe, codex.stripe, gemini.stripe]).size).toBe(3);
  });
});

describe("extractSubagents", () => {
  it("recovers a spawn and matches its result by tool_use_id", () => {
    const events = [
      spawnEvent("tu_1", "reviewer", "Review the diff"),
      resultEvent("tu_1", "Looks good", false),
    ];
    const subs = extractSubagents(events);
    expect(subs).toHaveLength(1);
    const s = subs[0];
    expect(s.tool_use_id).toBe("tu_1");
    expect(s.subagent_type).toBe("reviewer");
    expect(s.brief).toBe("Review the diff");
    expect(s.result_content).toBe("Looks good");
    expect(s.result_is_error).toBe(false);
    expect(s.children).toEqual([]);
  });

  it("leaves result fields unset while the subagent is still running", () => {
    const subs = extractSubagents([spawnEvent("tu_2", "impl", "Do work")]);
    expect(subs).toHaveLength(1);
    expect(subs[0].result_event_id).toBeUndefined();
  });

  it("flags error results", () => {
    const subs = extractSubagents([
      spawnEvent("tu_3", "impl", "Do work"),
      resultEvent("tu_3", "boom", true),
    ]);
    expect(subs[0].result_is_error).toBe(true);
  });

  it("ignores tool_result frames for non-Task tools", () => {
    const subs = extractSubagents([
      spawnEvent("tu_4", "impl", "Do work"),
      resultEvent("other_tool", "unrelated"),
    ]);
    expect(subs[0].result_event_id).toBeUndefined();
  });
});

describe("extractTaskToolSubagents", () => {
  it("recovers subagents from raw tool:Task events", () => {
    const e = ev({
      kind: "tool:Task",
      raw_json: JSON.stringify({
        message: {
          content: [
            {
              type: "tool_use",
              name: "Task",
              id: "tu_fallback",
              input: { subagent_type: "scout", description: "Scout the repo" },
            },
          ],
        },
      }),
    });
    const subs = extractTaskToolSubagents([e]);
    expect(subs).toHaveLength(1);
    expect(subs[0].subagent_type).toBe("scout");
    expect(subs[0].brief).toBe("Scout the repo");
  });
});

describe("mergeSubagents", () => {
  it("dedupes by tool_use_id, preferring the first list", () => {
    const canonical = extractSubagents([spawnEvent("dup", "a", "from spawn")]);
    const fallback = extractTaskToolSubagents([
      ev({
        kind: "tool:Task",
        raw_json: JSON.stringify({
          message: {
            content: [{ type: "tool_use", name: "Task", id: "dup", input: { subagent_type: "a" } }],
          },
        }),
      }),
    ]);
    const merged = mergeSubagents(canonical, fallback);
    expect(merged).toHaveLength(1);
    expect(merged[0].brief).toBe("from spawn");
  });
});

describe("buildSubagentTree", () => {
  it("nests children by parent_tool_use_id", () => {
    const events = [
      spawnEvent("parent", "lead", "Coordinate"),
      spawnEvent("child", "worker", "Sub-task", "parent"),
    ];
    const flat = extractSubagents(events);
    const forest = buildSubagentTree(flat);
    expect(forest).toHaveLength(1);
    expect(forest[0].tool_use_id).toBe("parent");
    expect(forest[0].children).toHaveLength(1);
    expect(forest[0].children[0].tool_use_id).toBe("child");
  });

  it("treats a dangling parent ref as a root (drops nothing)", () => {
    const orphan: Subagent = {
      spawn_event_id: 99,
      tool_use_id: "lonely",
      parent_tool_use_id: "missing",
      subagent_type: "x",
      brief: "",
      spawn_ts: "",
      children: [],
    };
    const forest = buildSubagentTree([orphan]);
    expect(forest).toHaveLength(1);
  });
});

describe("countToolCalls", () => {
  it("counts tool:* events and ignores spawns / results / text", () => {
    const events = [
      ev({ kind: "tool:Bash" }),
      ev({ kind: "tool:Read" }),
      ev({ kind: "spawn:subagent" }),
      ev({ kind: "tool_result" }),
      ev({ kind: "assistant_text" }),
    ];
    expect(countToolCalls(events)).toBe(2);
  });
});

describe("pickActivitySummary", () => {
  it("returns the most recent non-empty summary", () => {
    const events = [
      ev({ kind: "assistant_text", summary: "older", seq: 1 }),
      ev({ kind: "tool:Bash", summary: "newest", seq: 3 }),
      ev({ kind: "tool_result", summary: "", seq: 2 }),
    ];
    expect(pickActivitySummary(events)).toBe("newest");
  });

  it("returns empty string for no events", () => {
    expect(pickActivitySummary([])).toBe("");
  });
});
