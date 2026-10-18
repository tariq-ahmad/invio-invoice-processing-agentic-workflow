// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AdkEvent } from "../api/types";
import { usePipelineStore } from "../store/usePipelineStore";
import { pacing } from "../lib/pacing";
import { replayHooks } from "./hooks";
import { RECORDING_SCHEMA, parseRecording } from "./format";
import { continueReplay, exitReplay, seekTo, setPlaying, startReplay, useReplay } from "./controller";

const AUTHOR = "invoice_processing_workflow";
const ev = (node: string, delta?: Record<string, unknown>, extra: Partial<AdkEvent> = {}): AdkEvent => ({
  author: AUTHOR,
  nodeInfo: { path: `${AUTHOR}@1/${node}@1` },
  invocationId: "inv-1",
  ...(delta ? { actions: { stateDelta: delta } } : {}),
  ...extra,
});

// A run that pauses at the approval gate, then continues after the (recorded) decision.
const SEGMENT_1: AdkEvent[] = [
  ev("fetch_invoice_email", { invoice_message_id: "m1" }),
  ev("screen_email_signal", { screening_route: "invoice" }),
  ev("route_extraction_strategy", { extraction_strategy: "inline", extraction_recommended_model: "flash" }),
  ev("emit_extraction_card", {
    extraction_model: "gemini-3.8-flash",
    invoice_record: {
      vendor: { value: "Acme" },
      invoice_no: { value: "INV-1" },
      total: { value: "120.00" },
      currency: { value: "USD" },
    },
  }),
  ev("validate_invoice", { validation_route: "human_review", validation_flags: ["Over the limit"] }),
  ev("human_approval_gate", undefined, {
    content: { role: "model", parts: [{ functionCall: { name: "adk_request_input", id: "int-1", args: { message: "Approve?" } } }] },
  }),
];
const SEGMENT_2: AdkEvent[] = [
  ev("post_to_erp", { posting_result: { system: "ERP", document_id: "D-1" } }),
  ev("pipeline_complete", { pipeline_status: "posted_erp" }),
];

const recording = () =>
  parseRecording({
    schema: RECORDING_SCHEMA,
    recordedAt: "2026-10-15T00:00:00Z",
    segments: [
      { kind: "run", events: SEGMENT_1 },
      { kind: "resume", events: SEGMENT_2 },
    ],
  });

const replay = () => useReplay.getState();
const pipeline = () => usePipelineStore.getState();
const settle = { timeout: 8000, interval: 15 };

beforeEach(() => {
  exitReplay();
  useReplay.setState({ speed: 0 }); // "Max": no pacing waits
});
afterEach(() => exitReplay());

describe("replay controller (end to end through the real run loop)", () => {
  it("plays to the human-decision pause, then continues with the recorded outcome", async () => {
    startReplay(recording());
    await vi.waitFor(() => expect(replay().awaitingContinue).toBe(true), settle);

    expect(replay().position).toBe(SEGMENT_1.length);
    expect(pipeline().status).toBe("awaiting_input");
    expect(pipeline().currentInterrupt?.kind).toBe("approval");
    expect(pipeline().items.some((i) => i.kind === "surface" && i.surfaceId === "approval_gate")).toBe(true);
    expect(pipeline().state.invoice_record?.vendor?.value).toBe("Acme");

    continueReplay();
    await vi.waitFor(() => expect(replay().finished).toBe(true), settle);

    expect(pipeline().state.pipeline_status).toBe("posted_erp");
    expect(pipeline().status).toBe("complete");
    expect(replay().position).toBe(SEGMENT_1.length + SEGMENT_2.length);
  });

  it("seeks to an exact event and holds there, paused", async () => {
    startReplay(recording());
    await vi.waitFor(() => expect(replay().awaitingContinue).toBe(true), settle);

    seekTo(2, false);
    await vi.waitFor(() => expect(replay().position).toBe(2), settle);
    await vi.waitFor(() => expect(replay().playing).toBe(false), settle);

    // Events 1–2 are applied, nothing later has leaked in.
    expect(pipeline().state.screening_route).toBe("invoice");
    expect(pipeline().state.invoice_record).toBeUndefined();
    expect(pipeline().items.some((i) => i.kind === "surface" && i.surfaceId === "approval_gate")).toBe(false);
    expect(replay().awaitingContinue).toBe(false);

    // Resuming play carries on to the decision point.
    setPlaying(true);
    await vi.waitFor(() => expect(replay().awaitingContinue).toBe(true), settle);
  });

  it("seeking past a decision point applies the recorded decision automatically", async () => {
    startReplay(recording());
    await vi.waitFor(() => expect(replay().awaitingContinue).toBe(true), settle);

    seekTo(SEGMENT_1.length + 1, false); // one event into the post-decision segment
    await vi.waitFor(() => expect(replay().position).toBe(SEGMENT_1.length + 1), settle);
    await vi.waitFor(() => expect(replay().playing).toBe(false), settle);

    expect(replay().awaitingContinue).toBe(false);
    expect(pipeline().state.posting_result?.document_id).toBe("D-1");
    expect(pipeline().state.pipeline_status).toBeUndefined();
    expect(pipeline().currentInterrupt).toBeNull();
  });

  it("a seek issued mid-playback leaves no stale writes and no duplicate cards", async () => {
    startReplay(recording());
    seekTo(3, false); // supersede immediately, while the first pass is still running
    await vi.waitFor(() => expect(replay().position).toBe(3), settle);
    await vi.waitFor(() => expect(replay().playing).toBe(false), settle);

    const ids = pipeline().items.map((i) => i.id);
    expect(new Set(ids).size).toBe(ids.length);
    const surfaces = pipeline().items.filter((i) => i.kind === "surface").map((i) => (i.kind === "surface" ? i.surfaceId : ""));
    expect(new Set(surfaces).size).toBe(surfaces.length);
    expect(pipeline().state.extraction_model).toBeUndefined(); // event 4 hasn't happened yet
  });

  it("exiting restores a clean live view", async () => {
    startReplay(recording());
    await vi.waitFor(() => expect(replay().awaitingContinue).toBe(true), settle);

    exitReplay();
    expect(replay().active).toBe(false);
    expect(replayHooks.active).toBe(false);
    expect(pacing.scale).toBe(1);
    expect(pipeline().status).toBe("idle");
    expect(pipeline().items).toHaveLength(0);
    expect(pipeline().state.invoice_record).toBeUndefined();
  });

  it("restarts from the beginning once finished", async () => {
    startReplay(recording());
    await vi.waitFor(() => expect(replay().awaitingContinue).toBe(true), settle);
    continueReplay();
    await vi.waitFor(() => expect(replay().finished).toBe(true), settle);

    setPlaying(true); // "play again"
    await vi.waitFor(() => expect(replay().awaitingContinue).toBe(true), settle);
    expect(replay().finished).toBe(false);
    expect(pipeline().state.pipeline_status).toBeUndefined();
  });
});
