import { describe, expect, it } from "vitest";
import { RECORDING_SCHEMA, parseRecording, segmentEnds, totalEvents } from "./format";

const ev = (author: string) => ({ author });

describe("parseRecording", () => {
  const good = {
    schema: RECORDING_SCHEMA,
    recordedAt: "2026-10-15T10:00:00.000Z",
    segments: [
      { kind: "run", events: [ev("a"), ev("b"), ev("c")] },
      { kind: "resume", events: [ev("d"), ev("e")] },
    ],
  };

  it("accepts a valid recording and computes totals and boundaries", () => {
    const rec = parseRecording(good);
    expect(totalEvents(rec)).toBe(5);
    expect(segmentEnds(rec)).toEqual([3, 5]);
    expect(rec.segments[1].kind).toBe("resume");
  });

  it("rejects things that are not recordings, with readable messages", () => {
    expect(() => parseRecording(null)).toThrow("isn't an Invoice Agent recording");
    expect(() => parseRecording({ schema: "something-else" })).toThrow("isn't an Invoice Agent recording");
    expect(() => parseRecording({ schema: RECORDING_SCHEMA, segments: [] })).toThrow("no segments");
    expect(() => parseRecording({ schema: RECORDING_SCHEMA, segments: [{ kind: "run", events: "nope" }] })).toThrow("Segment 1 is malformed");
    expect(() => parseRecording({ schema: RECORDING_SCHEMA, segments: [{ kind: "run", events: [1, 2] }] })).toThrow("invalid events");
  });

  it("requires the first segment to be the initial run and at least one event", () => {
    expect(() => parseRecording({ schema: RECORDING_SCHEMA, segments: [{ kind: "resume", events: [ev("a")] }] })).toThrow("initial run");
    expect(() => parseRecording({ schema: RECORDING_SCHEMA, segments: [{ kind: "run", events: [] }] })).toThrow("no events");
  });

  it("round-trips through JSON", () => {
    const rec = parseRecording(JSON.parse(JSON.stringify(good)));
    expect(totalEvents(rec)).toBe(5);
  });
});
