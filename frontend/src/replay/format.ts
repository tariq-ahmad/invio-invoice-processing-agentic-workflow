import type { AdkEvent } from "../api/types";

export const RECORDING_SCHEMA = "invoice-agent/recording@1";

export interface RecordedSegment {
  /** "run" = the initial stream; "resume" = the stream after a human decision. */
  kind: "run" | "resume";
  events: AdkEvent[];
}

export interface RecordingMeta {
  vendor?: string;
  invoiceNo?: string;
  total?: string;
  outcome?: string;
}

export interface Recording {
  schema: typeof RECORDING_SCHEMA;
  recordedAt: string;
  meta?: RecordingMeta;
  segments: RecordedSegment[];
}

export const totalEvents = (rec: Recording): number =>
  rec.segments.reduce((n, s) => n + s.events.length, 0);

/** Cumulative event count at the end of each segment, e.g. [18, 31]. */
export function segmentEnds(rec: Recording): number[] {
  let n = 0;
  return rec.segments.map((s) => (n += s.events.length));
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** Validate untrusted JSON (a user-picked file) into a Recording. Throws an
 * Error with a user-readable message when it isn't one. */
export function parseRecording(json: unknown): Recording {
  if (!isObject(json) || json.schema !== RECORDING_SCHEMA) {
    throw new Error("This file isn't an Invoice Agent recording.");
  }
  const segs = json.segments;
  if (!Array.isArray(segs) || segs.length === 0) {
    throw new Error("The recording has no segments.");
  }
  const segments: RecordedSegment[] = segs.map((s, i) => {
    if (!isObject(s) || !Array.isArray(s.events)) {
      throw new Error(`Segment ${i + 1} is malformed.`);
    }
    const kind = s.kind === "resume" ? "resume" : "run";
    if (!s.events.every(isObject)) throw new Error(`Segment ${i + 1} has invalid events.`);
    return { kind, events: s.events as unknown as AdkEvent[] };
  });
  if (segments[0].kind !== "run") throw new Error("The first segment must be the initial run.");
  if (segments.every((s) => s.events.length === 0)) throw new Error("The recording has no events.");
  return {
    schema: RECORDING_SCHEMA,
    recordedAt: typeof json.recordedAt === "string" ? json.recordedAt : new Date().toISOString(),
    meta: isObject(json.meta) ? (json.meta as RecordingMeta) : undefined,
    segments,
  };
}
