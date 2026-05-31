// Status node for the live tree.
//
// Design (post owner-feedback):
//  - LEFT STRIPE color = VP (Claude=amber, Codex=emerald, Gemini=sky).
//    Project/task root nodes stay neutral (gray) so the user can spot the
//    boundary between scaffolding and AI work.
//  - STATUS conveyed by a small dot beside the role label:
//      running  → pulsing blue dot
//      done     → solid green dot
//      failed   → red dot with red ring
//      pending  → solid amber dot
//      idle     → gray dot
//  - ONE-line activity summary in `sub` (caller fills via pickActivitySummary).
//  - Optional "🔧 N" badge counting tool calls — clickable opens detail.
//  - Rounded corners (8px), subtle hover state.

import React, { useState } from "react";
import { Handle, Position } from "reactflow";
import { paletteForVp, NEUTRAL_PALETTE, type VpPalette } from "./extractors";

type StatusDot = { color: string; ring?: string; pulse?: boolean; label: string };

const STATUS_DOTS: Record<string, StatusDot> = {
  running:   { color: "#3b82f6", pulse: true,  label: "running" },
  launched:  { color: "#3b82f6", pulse: true,  label: "launching" },
  completed: { color: "#16a34a",                label: "done" },
  merged:    { color: "#16a34a",                label: "merged" },
  done:      { color: "#16a34a",                label: "done" },
  failed:    { color: "#dc2626", ring: "#7f1d1d", label: "failed" },
  rejected:  { color: "#dc2626", ring: "#7f1d1d", label: "rejected" },
  error:     { color: "#dc2626", ring: "#7f1d1d", label: "error" },
  pending:   { color: "#f59e0b",                label: "pending" },
  waiting:   { color: "#f59e0b",                label: "waiting" },
  pending_merge:        { color: "#f59e0b",     label: "pending merge" },
  rfc_pending_approval: { color: "#f59e0b",     label: "needs approval" },
  conflict:  { color: "#f59e0b",                label: "conflict" },
  stopped:   { color: "#6b7280",                label: "stopped" },
  idle:      { color: "#52525b",                label: "idle" },
};

function dotFor(status: string): StatusDot {
  return STATUS_DOTS[status] || STATUS_DOTS.idle;
}

export type StatusNodeData = {
  label: string;
  sub: string;
  status: string;
  vp?: string | null;            // claude_vp / codex_vp / gemini_vp; falsy = neutral
  isRoot?: boolean;
  isGate?: boolean;
  toolCount?: number;            // shown as "🔧 N" badge; clickable
  onClick?: () => void;
  onToolBadgeClick?: () => void;
};

export function StatusNode({ data }: { data: StatusNodeData }) {
  // Root nodes (project, task) are intentionally neutral so they read as
  // scaffolding, not AI agents. Everything else colors by VP.
  const palette: VpPalette = data.isRoot ? NEUTRAL_PALETTE : paletteForVp(data.vp);
  const dot = dotFor(data.status);
  const [hover, setHover] = useState(false);
  return (
    <div
      onClick={data.onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        background: hover ? palette.cardBgHover : palette.cardBg,
        border: "1px solid #27272a",
        borderLeft: `4px solid ${palette.stripe}`,
        borderRadius: 8,
        padding: data.isRoot ? "10px 14px" : "8px 12px",
        minWidth: 180,
        maxWidth: 240,
        cursor: data.onClick ? "pointer" : "default",
        boxShadow: hover ? "0 2px 8px rgba(0,0,0,.5)" : "0 1px 2px rgba(0,0,0,.4)",
        fontFamily: "system-ui, sans-serif",
        transition: "background 120ms ease, box-shadow 120ms ease",
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div
        style={{
          color: "#e4e4e7",
          fontSize: data.isRoot ? 13 : 12,
          fontWeight: 600,
          letterSpacing: 0.1,
          display: "flex",
          alignItems: "center",
          gap: 6,
          minWidth: 0,
        }}
      >
        {data.isGate && (
          <span style={{ fontSize: 11, opacity: 0.85 }} title="human gate">⏸</span>
        )}
        <span
          aria-label={dot.label}
          title={dot.label}
          style={{
            display: "inline-block",
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: dot.color,
            boxShadow: dot.ring ? `0 0 0 2px ${dot.ring}` : "none",
            flexShrink: 0,
            animation: dot.pulse ? "lt-pulse 1.4s ease-in-out infinite" : "none",
          }}
        />
        <span
          style={{
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            flex: 1,
          }}
        >
          {data.label}
        </span>
      </div>
      {data.sub && (
        <div
          style={{
            marginTop: 4,
            color: "#a1a1aa",
            fontSize: 11,
            lineHeight: 1.3,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={data.sub}
        >
          {data.sub}
        </div>
      )}
      {typeof data.toolCount === "number" && data.toolCount > 0 && (
        <div
          onClick={(ev) => {
            ev.stopPropagation();
            if (data.onToolBadgeClick) data.onToolBadgeClick();
            else if (data.onClick) data.onClick();
          }}
          style={{
            marginTop: 6,
            display: "inline-block",
            fontSize: 10,
            color: "#a1a1aa",
            background: "#0a0a0a",
            border: "1px solid #27272a",
            borderRadius: 10,
            padding: "1px 8px",
            cursor: "pointer",
          }}
          title="Open tool detail"
        >
          🔧 {data.toolCount}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

// Kept for backwards compatibility with any caller that still references it.
// The new graph does NOT render tool-call leaf nodes — these icons are no
// longer used by buildGraph.ts.
export const TOOL_ICONS: Record<string, string> = {
  Bash: "▶",
  Edit: "✎",
  Read: "👁",
  Write: "📄",
  Task: "🌿",
  Grep: "🔎",
  Glob: "🔎",
  WebFetch: "🌐",
  WebSearch: "🌐",
};

export function toolIcon(name: string): string {
  return TOOL_ICONS[name] || "•";
}
