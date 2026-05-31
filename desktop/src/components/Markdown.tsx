// Minimal markdown renderer — headings, lists, bold/italic/code, code fences, tables.
// Avoids pulling in a full markdown lib for the MVP. Not for rendering untrusted HTML.
import React from "react";

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]!));
}

function inlineFormat(s: string): string {
  let out = escapeHtml(s);
  // wiki-links [[X]] → bold accent (UI badge)
  out = out.replace(/\[\[([^\]]+)\]\]/g, '<span class="text-accent">[[$1]]</span>');
  out = out.replace(/`([^`]+)`/g, "<code class=\"px-1 py-0.5 rounded bg-bg-1 text-ink-0\">$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  return out;
}

export function Markdown({ text }: { text: string }) {
  const lines = text.split("\n");
  const html: string[] = [];
  let inCode = false;
  let inList = false;
  let codeBuf: string[] = [];

  function closeList() {
    if (inList) {
      html.push("</ul>");
      inList = false;
    }
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("```")) {
      if (!inCode) {
        closeList();
        inCode = true;
        codeBuf = [];
      } else {
        inCode = false;
        html.push(
          `<pre class="card bg-bg-1 overflow-x-auto text-xs leading-relaxed whitespace-pre"><code>${escapeHtml(
            codeBuf.join("\n"),
          )}</code></pre>`,
        );
      }
      continue;
    }
    if (inCode) {
      codeBuf.push(line);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeList();
      const level = h[1].length;
      const size =
        level === 1 ? "text-2xl" : level === 2 ? "text-xl" : level === 3 ? "text-lg" : "text-base";
      html.push(
        `<h${level} class="${size} font-semibold mt-4 mb-2 text-ink-0">${inlineFormat(h[2])}</h${level}>`,
      );
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      if (!inList) {
        html.push('<ul class="list-disc pl-6 my-2 text-ink-1 space-y-1">');
        inList = true;
      }
      html.push(`<li>${inlineFormat(line.replace(/^[-*]\s+/, ""))}</li>`);
      continue;
    }
    closeList();
    if (!line.trim()) {
      html.push("<div class='h-2'></div>");
      continue;
    }
    if (line.startsWith("|")) {
      // crude table
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      // detect separator row → skip
      if (cells.every((c) => /^[-:\s]+$/.test(c))) continue;
      html.push(
        `<div class="grid gap-2 text-sm text-ink-1 my-1" style="grid-template-columns: repeat(${cells.length}, minmax(0,1fr))">${cells.map((c) => `<div>${inlineFormat(c)}</div>`).join("")}</div>`,
      );
      continue;
    }
    html.push(`<p class="text-ink-1 leading-relaxed my-1">${inlineFormat(line)}</p>`);
  }
  closeList();

  return (
    <div
      className="prose-johnstudio"
      dangerouslySetInnerHTML={{ __html: html.join("\n") }}
    />
  );
}
