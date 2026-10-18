// Formats a run's elapsed wall-clock time for the TopBar's live "Elapsed"
// pill. Distinct from the per-model-call fmtLatency helpers in
// A2uiHost.tsx/PipelineTimeline.tsx, which only handle sub-minute latencies.

/** "1.8s" for >=1s, else "940ms" — a single call's latency (a posting write,
 * a model call). Same format as the per-card helpers in A2uiHost.tsx. */
export function formatLatency(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

/** "12.3s" below 1 minute, "2m 14s" at/above 1 minute. Caps display at
 * 59m 59s — invoice runs aren't expected to run for hours. */
export function formatElapsed(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) {
    return `${(ms / 1000).toFixed(1)}s`;
  }
  const minutes = Math.min(59, Math.floor(totalSec / 60));
  const seconds = totalSec % 60;
  return `${minutes}m ${seconds}s`;
}
