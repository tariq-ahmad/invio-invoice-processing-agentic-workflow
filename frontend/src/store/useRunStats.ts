/**
 * Processing stats, read live from Firestore through GET /stats (the backend
 * totals the `run_outcomes` documents it writes when each run finishes). Shared
 * by everyone using the agent — not a per-browser tally.
 */
import { create } from "zustand";
import type { RunStatsResponse } from "../lib/statsSummary";

/** Window in days; 0 = every recorded run. */
export type RangeDays = 1 | 7 | 30 | 0;
export type StatsLoad = "idle" | "loading" | "ok" | "error";

interface RunStats {
  data: RunStatsResponse | null;
  load: StatsLoad;
  error: string | null;
  fetchedAt: number | null;
  range: RangeDays;
  setRange: (r: RangeDays) => void;
  refresh: () => Promise<void>;
}

let seq = 0; // ignore a slow response that a newer request has overtaken

function describe(status: number): string {
  if (status === 503) return "Firestore is temporarily unavailable.";
  if (status === 404) return "The stats endpoint isn't available on this server.";
  return `The server returned ${status}.`;
}

export const useRunStats = create<RunStats>((set, get) => ({
  data: null,
  load: "idle",
  error: null,
  fetchedAt: null,
  range: 0,

  setRange(range) {
    if (range === get().range) return;
    set({ range });
    void get().refresh();
  },

  async refresh() {
    const my = ++seq;
    // Keep showing the last numbers while refreshing; only the first load blanks.
    set((s) => ({ load: s.data ? s.load : "loading", error: null }));
    try {
      const res = await fetch(`/stats?days=${get().range}`, { cache: "no-store" });
      if (my !== seq) return;
      if (!res.ok) {
        set({ load: "error", error: describe(res.status) });
        return;
      }
      const data = (await res.json()) as RunStatsResponse;
      if (my !== seq) return;
      set({ data, load: "ok", error: null, fetchedAt: Date.now() });
    } catch {
      if (my !== seq) return;
      set({ load: "error", error: "Can't reach the server." });
    }
  },
}));
