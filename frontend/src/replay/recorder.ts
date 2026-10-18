/**
 * Records the raw event stream of the most recent live run (every segment: the
 * initial stream plus one per human decision) so it can be replayed later.
 * Events are appended without notifying subscribers — only structural changes
 * (a new run / segment) do — so recording costs nothing per event.
 */
import { create } from "zustand";
import type { AdkEvent } from "../api/types";
import { RECORDING_SCHEMA, totalEvents, type Recording, type RecordingMeta } from "./format";

interface RecorderStore {
  current: Recording | null;
  /** Start a fresh recording (a new live run). */
  begin: () => void;
  /** Start a new segment (the stream after a human decision). */
  startSegment: () => void;
  push: (ev: AdkEvent) => void;
  /** Notify subscribers that events were appended (call when a stream settles). */
  commit: () => void;
  /** The recording with display metadata attached, or null if it has no events. */
  snapshot: (meta?: RecordingMeta) => Recording | null;
}

export const useRecorder = create<RecorderStore>((set, get) => ({
  current: null,

  begin() {
    set({
      current: {
        schema: RECORDING_SCHEMA,
        recordedAt: new Date().toISOString(),
        segments: [{ kind: "run", events: [] }],
      },
    });
  },

  startSegment() {
    const cur = get().current;
    if (!cur) return;
    set({ current: { ...cur, segments: [...cur.segments, { kind: "resume", events: [] }] } });
  },

  push(ev) {
    const cur = get().current;
    if (!cur) return;
    cur.segments[cur.segments.length - 1].events.push(ev); // intentional in-place append
  },

  commit() {
    const cur = get().current;
    if (cur) set({ current: { ...cur } });
  },

  snapshot(meta) {
    const cur = get().current;
    if (!cur || totalEvents(cur) === 0) return null;
    return { ...cur, meta: meta ?? cur.meta };
  },
}));

/** Selector: does a replayable recording exist? (Boolean, so it re-renders rarely.) */
export const selectHasRecording = (s: RecorderStore): boolean =>
  !!s.current && s.current.segments.some((seg) => seg.events.length > 0);
