import type { AdkEvent } from "./types";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Poll `/run_sse/poll` for pipeline progress instead of reading `/run_sse`'s
 * response body directly. Cloud Run's front end fully buffers that stream
 * over HTTP/2 (every browser client) until the connection closes, so direct
 * reading shows nothing until the whole run finishes — confirmed by capturing
 * real chunk-arrival timestamps inside the browser's own fetch/ReadableStream.
 * A small, complete JSON poll response isn't subject to that buffering.
 *
 * Same `AsyncGenerator<AdkEvent>` shape as `parseSse`, so callers don't care
 * which source produced the events.
 */
export async function* pollSse(
  token: string,
  signal?: AbortSignal,
  intervalMs = 1000,
): AsyncGenerator<AdkEvent> {
  let cursor = 0;
  while (true) {
    if (signal?.aborted) return;
    let data: { lines: string[]; cursor: number; done: boolean };
    try {
      const res = await fetch(`/run_sse/poll?token=${encodeURIComponent(token)}&cursor=${cursor}`);
      if (!res.ok) {
        await sleep(intervalMs);
        continue;
      }
      data = await res.json();
    } catch {
      await sleep(intervalMs);
      continue;
    }
    cursor = data.cursor;
    for (const line of data.lines) {
      const payload = line.startsWith("data:") ? line.slice(5).trimStart() : line;
      try {
        yield JSON.parse(payload) as AdkEvent;
      } catch {
        // Skip unparseable frames.
      }
    }
    if (data.done && data.lines.length === 0) return;
    if (signal?.aborted) return;
    await sleep(intervalMs);
  }
}
