/**
 * Replay controller. Feeds a recorded run back through the SAME event loop the
 * live app uses (usePipelineStore's consume), so a replay renders exactly what
 * the real run did — cards, graph, costs, human-review pauses — with no backend.
 *
 * Playback model:
 *  - `position` = number of recorded events fully processed.
 *  - Seeking re-runs from the start with pacing scaled to 0 (instant) up to the
 *    target position, then continues at the chosen speed (or pauses).
 *  - A recorded run that stopped for a human decision pauses at the boundary;
 *    "Continue" plays the next segment, i.e. the decision that was recorded.
 */
import { create } from "zustand";
import type { AdkEvent } from "../api/types";
import { usePipelineStore } from "../store/usePipelineStore";
import { toast } from "../store/useToasts";
import { pacing, speedToScale, type Speed } from "../lib/pacing";
import { replayHooks } from "./hooks";
import {
  parseRecording,
  segmentEnds,
  totalEvents,
  type RecordedSegment,
  type Recording,
} from "./format";

interface ReplayStore {
  active: boolean;
  recording: Recording | null;
  position: number;
  total: number;
  segmentEnds: number[];
  playing: boolean;
  speed: Speed;
  finished: boolean;
  /** Paused at a human-decision boundary, waiting for "Continue". */
  awaitingContinue: boolean;
}

const INITIAL: ReplayStore = {
  active: false,
  recording: null,
  position: 0,
  total: 0,
  segmentEnds: [],
  playing: false,
  speed: 1,
  finished: false,
  awaitingContinue: false,
};

export const useReplay = create<ReplayStore>(() => INITIAL);

let session = 0; // bumped on every (re)start / exit; stale drivers bail out

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Begin replaying a recording from the start (autoplay). */
export function startReplay(recording: Recording): void {
  replayHooks.active = true;
  useReplay.setState({
    ...INITIAL,
    active: true,
    recording,
    total: totalEvents(recording),
    segmentEnds: segmentEnds(recording),
    speed: useReplay.getState().speed,
  });
  void drive(0, true);
}

/** Jump to an event position, then continue playing or hold paused. */
export function seekTo(position: number, resumePlaying: boolean): void {
  const { total } = useReplay.getState();
  void drive(Math.max(0, Math.min(total, Math.round(position))), resumePlaying);
}

export function restartReplay(): void {
  void drive(0, true);
}

export function setPlaying(playing: boolean): void {
  const s = useReplay.getState();
  if (!s.active) return;
  if (playing && s.finished) return restartReplay();
  useReplay.setState({ playing });
}

export function setSpeed(speed: Speed): void {
  useReplay.setState({ speed });
  if (!fastForwarding) pacing.scale = speedToScale(speed);
}

/** Play the recorded human decision and carry on. */
export function continueReplay(): void {
  usePipelineStore.setState({ status: "streaming", currentInterrupt: null });
  replayHooks.resume?.();
}

export function exitReplay(): void {
  session++;
  replayHooks.resume?.(); // release a driver parked at a decision boundary
  replayHooks.resume = null;
  usePipelineStore.getState().cancelRun();
  replayHooks.active = false;
  fastForwarding = false;
  pacing.scale = 1;
  useReplay.setState(INITIAL);
  usePipelineStore.getState().resetLocal();
}

/** Read a user-picked recording file and start replaying it. */
export async function loadRecordingFile(file: File): Promise<void> {
  try {
    const rec = parseRecording(JSON.parse(await file.text()));
    startReplay(rec);
    toast(`Replaying ${totalEvents(rec)} recorded events`, "success");
  } catch (e) {
    toast(e instanceof SyntaxError ? "That file isn't valid JSON." : (e as Error).message, "error");
  }
}

// ── driver ─────────────────────────────────────────────────────────────────

let fastForwarding = false;

async function drive(target: number, autoplay: boolean): Promise<void> {
  const my = ++session;
  const rec = useReplay.getState().recording;
  if (!rec) return;

  replayHooks.resume?.(); // release any previous driver parked at a boundary
  replayHooks.resume = null;
  const pipeline = usePipelineStore.getState();
  pipeline.cancelRun();
  pipeline.replayBegin();

  let pos = 0;
  const userScale = () => speedToScale(useReplay.getState().speed);

  const enter = (fast: boolean) => {
    fastForwarding = fast;
    pacing.scale = fast ? 0 : userScale();
  };
  enter(target > 0);
  useReplay.setState({
    position: 0,
    finished: false,
    awaitingContinue: false,
    playing: target > 0 ? true : autoplay,
  });

  async function* events(seg: RecordedSegment): AsyncGenerator<AdkEvent> {
    for (const ev of seg.events) {
      // Hold here while paused (only once we've reached the target position).
      while (session === my && pos >= target && !useReplay.getState().playing) await wait(60);
      if (session !== my) return;
      yield ev;
      if (session !== my) return;
      pos++;
      useReplay.setState({ position: pos });
      if (pos === target && fastForwarding) {
        enter(false);
        useReplay.setState({ playing: autoplay });
      }
    }
  }

  const last = rec.segments.length - 1;
  for (let i = 0; i <= last; i++) {
    await pipeline.replayConsume(events(rec.segments[i]));
    if (session !== my) return;
    if (i === last) break;

    // The recorded stream stopped for a human decision.
    if (pos < target) {
      // Fast-forwarding past it: apply the recorded decision automatically.
      usePipelineStore.setState({ status: "streaming", currentInterrupt: null });
      continue;
    }
    useReplay.setState({ awaitingContinue: true, playing: false });
    await new Promise<void>((resolve) => {
      replayHooks.resume = () => {
        replayHooks.resume = null;
        resolve();
      };
    });
    if (session !== my) return;
    useReplay.setState({ awaitingContinue: false, playing: true });
  }

  enter(false);
  useReplay.setState({ finished: true, playing: false, position: pos });
}
