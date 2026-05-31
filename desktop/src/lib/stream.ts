// SSE client that supports a Bearer header.
//
// Browser EventSource can't set custom headers, so we use fetch() with a
// ReadableStream and a tiny SSE parser. The parser handles:
//   - `event: <name>` lines
//   - `data: <json>` lines (possibly multi-line per event)
//   - blank-line event terminators
//   - heartbeat comments (`: heartbeat`)
//
// `onEvent` is called once per fully assembled event. The handler signals
// shutdown by aborting the AbortController returned from `connect()`.

import { API_BASE } from "../api/client";

const API_TOKEN: string | undefined =
  (import.meta as any).env?.VITE_JOHNSTUDIO_TOKEN || undefined;

export type StreamEvent = {
  event: string;
  data: any;
};

export function connectStream(
  path: string,
  onEvent: (e: StreamEvent) => void,
  onError?: (err: any) => void,
): AbortController {
  const ac = new AbortController();
  const headers: Record<string, string> = { accept: "text/event-stream" };
  if (API_TOKEN) headers.authorization = `Bearer ${API_TOKEN}`;

  (async () => {
    try {
      const res = await fetch(`${API_BASE}${path}`, {
        headers,
        signal: ac.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`stream HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let currentEvent = "message";
      let dataLines: string[] = [];

      const flush = () => {
        if (dataLines.length === 0) {
          currentEvent = "message";
          return;
        }
        const raw = dataLines.join("\n");
        let parsed: any = raw;
        try {
          parsed = JSON.parse(raw);
        } catch {
          // pass raw string through
        }
        onEvent({ event: currentEvent, data: parsed });
        currentEvent = "message";
        dataLines = [];
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nl: number;
        while ((nl = buf.indexOf("\n")) !== -1) {
          const line = buf.slice(0, nl).replace(/\r$/, "");
          buf = buf.slice(nl + 1);
          if (line === "") {
            flush();
          } else if (line.startsWith(":")) {
            // comment / heartbeat — ignore
          } else if (line.startsWith("event:")) {
            currentEvent = line.slice(6).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).replace(/^ /, ""));
          }
        }
      }
      flush();
    } catch (err) {
      if ((err as any)?.name === "AbortError") return;
      onError?.(err);
    }
  })();

  return ac;
}
