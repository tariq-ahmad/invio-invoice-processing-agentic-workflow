import { create } from "zustand";
import { AdkClient } from "../api/client";
import { pollSse } from "../api/pollParser";
import {
  getA2uiMessages,
  getError,
  getInterrupt,
  getStage,
  getStateDelta,
  getText,
  nodeName,
} from "../api/eventInterpreter";
import type { AdkEvent, InterruptInfo, PipelineState } from "../api/types";
import { actionToResume, type A2uiClientAction } from "../a2ui/actionBridge";
import { createA2uiEngine, type A2uiEngine, type AnySurface } from "../a2ui/processor";
import { nextId } from "../lib/utils";
import { isArmorStage, type ArmorStageId, type StageId } from "./stageMap";
import { armorVerdict } from "../lib/armor";
import { sleep } from "../lib/pacing";
import { replayHooks } from "../replay/hooks";
import { useRecorder } from "../replay/recorder";

export type RunStatus = "idle" | "streaming" | "awaiting_input" | "complete" | "error";
export type StageStatus = "pending" | "active" | "done";
export type StepKind =
  | "classify"
  | "posting"
  | "escalation"
  | "screening_decision"
  | "routing"
  // One Model Armor card per scan point (see ArmorCard); only pushed when it is enabled.
  | ArmorStageId;

export interface TextItem {
  kind: "text";
  id: string;
  text: string;
  /** Stage this message belongs to, if any — drives the shared step header
   * (number + Deterministic/Agent badge + narrative) in MessageBubble. */
  stageId?: StageId;
}
export interface SurfaceItem {
  kind: "surface";
  id: string;
  surfaceId: string;
}
export interface StepItem {
  kind: "step";
  id: string;
  step: StepKind;
}
export type ConversationItem = TextItem | SurfaceItem | StepItem;

interface PipelineStore {
  status: RunStatus;
  items: ConversationItem[];
  stageStatus: Partial<Record<StageId, StageStatus>>;
  /** Contiguous, run-order step number assigned the first time each stage is
   * displayed — the single source of numbering for every step header, text
   * or card, so the visible sequence never has gaps or reused numbers. */
  stepNumbers: Partial<Record<StageId, number>>;
  currentStage: StageId | null;
  /** Wall-clock Date.now() when the current run started/ended, for the
   * TopBar's live "Elapsed" pill. Null start = no run yet; null end = still
   * running (including awaiting_input, since HITL wait counts as elapsed). */
  runStartedAt: number | null;
  runEndedAt: number | null;
  /** Wall-clock Date.now() each stage was (re-)activated. Powers both the
   * MIN_AGENT_STAGE_MS dwell check and useWorkingLabel's elapsed-based
   * rotating copy — single source of truth for "how long has this stage
   * been running". */
  stageActivatedAt: Partial<Record<StageId, number>>;
  /** True when this run was triggered by a specific AgentMail message ID (a real
   * push-triggered run) rather than a generic "search latest invoice" fallback
   * — lets useWorkingLabel describe Intake accurately (fetch vs. search). */
  intakeHasExactMessage: boolean;
  state: PipelineState;
  currentInterrupt: InterruptInfo | null;
  invocationId?: string;
  error: string | null;
  workingLabel: string;
  seen: Partial<Record<StepKind, boolean>>;
  engine: A2uiEngine;
  surfaces: Record<string, AnySurface>;

  startPipeline: (text?: string) => Promise<void>;
  autoStart: (messageId?: string) => void;
  submitAction: (action: A2uiClientAction) => Promise<void>;
  submitResume: (interruptId: string, value: unknown) => Promise<void>;
  reset: () => void;
  /** Reset the UI only — no backend session call (used when leaving a replay). */
  resetLocal: () => void;
  /** Abort whichever event loop is currently consuming (seek / exit replay). */
  cancelRun: () => void;
  /** Prepare a fresh, streaming run view that a replay will feed events into. */
  replayBegin: () => void;
  /** Run the normal event loop over a recorded source instead of the network. */
  replayConsume: (source: AsyncIterable<AdkEvent>) => Promise<void>;
}

const client = new AdkClient();

// These nodes are represented by surface cards (extraction / approval / email),
// so their text/markdown is never rendered as a chat bubble.
const SUPPRESS_TEXT_NODES = new Set([
  "screening_agent",
  "screen_email_signal",
  "extraction_agent",
  "draft_rejection_email",
  "emit_extraction_card",
  "human_approval_gate",
  "review_email_draft",
  "recall_precedents",
]);
const INTERNAL_SIGNALS = new Set(["NO_INVOICE_FOUND"]);

// Canonical forward order — used so the timeline only ever advances forward and
// so we can light up the *next* stage optimistically (with its "working" label)
// while the agent is still producing it. This is what makes a slow real node
// (extraction via Gemini) show "Reading the PDF…" during its actual wait,
// instead of the previous stage's label.
const STAGE_ORDER: StageId[] = [
  "intake",
  "armor_intake",
  "screening",
  "screening_decision",
  "routing",
  "extraction",
  "classify",
  "validation",
  "memory",
  "approval",
  "escalate",
  "draft_email",
  "review_email",
  "notify",
  "post_erp",
  "complete",
];
const idx = (s: StageId | null) => (s ? STAGE_ORDER.indexOf(s) : -1);

function nextExpected(stage: StageId, state: PipelineState): StageId | null {
  // Model Armor stopped the invoice after extraction: validation hands it straight
  // to the terminal step, whose summary card shows the details. Without this the
  // look-ahead below lights up a stage that never runs (Post to ERP after
  // validation) and the final "complete" then flips it to "done". (An intake-time
  // quarantine goes through its armor_intake scan first, below.)
  if (stage === "validation" && state.armor_quarantine) return "complete";
  switch (stage) {
    case "intake":
      // Model Armor screens the email before any agent sees it, whenever it is on.
      return state.armor_intake ? "armor_intake" : "screening";
    case "armor_intake":
      // A caught email never reaches Screening; it goes straight to the terminal step.
      return state.armor_quarantine ? "complete" : "screening";
    case "screening":
      // The LLM assessment is always followed by the deterministic decision node.
      return "screening_decision";
    case "screening_decision":
      // Read the backend's authoritative route; default optimistically to
      // routing until the decision arrives.
      return state.screening_route === "not_invoice" ? null : "routing";
    case "routing":
      return "extraction";
    case "extraction":
      // The PO lookup (spend classification) now runs right after extraction,
      // before validation, so the checks and the approver both see the PO.
      return "classify";
    case "classify":
      return "validation";
    case "validation":
      if (state.validation_route === "duplicate_po") return "escalate";
      if (state.validation_route === "human_review") return "memory";
      return "post_erp";
    case "memory":
      return "approval";
    case "draft_email":
      return "review_email";
    case "post_erp":
    case "escalate":
    case "notify":
      return "complete";
    // approval / review_email depend on the human decision (resolved on resume).
    default:
      return null;
  }
}

// Demo pacing per stage (ms). Deterministic nodes run instantly, so we space the
// transitions to read like real work. The two LLM-agent stages already have real
// latency; their pacing is small.
const STAGE_DELAY: Record<StageId, number> = {
  intake: 900,
  // Long enough for the scan animation to tick through its checklist.
  armor_intake: 1900,
  screening: 500,
  screening_decision: 500,
  routing: 400,
  extraction: 500,
  validation: 1300,
  memory: 700,
  approval: 500,
  classify: 1400,
  post_erp: 1500,
  escalate: 1400,
  draft_email: 700,
  review_email: 500,
  notify: 1300,
  complete: 800,
};
export const WORKING_LABEL: Record<StageId, string> = {
  intake: "Fetching the latest invoice from AgentMail…",
  armor_intake: "Model Armor is scanning the email and attachment…",
  screening: "Screening email for invoice signals…",
  screening_decision: "Applying the proceed/discard policy…",
  routing: "Choosing PDF transport and model tier…",
  extraction: "Reading the invoice PDF with Gemini…",
  validation: "Applying AP business rules…",
  memory: "Recalling prior AP decisions for this vendor…",
  approval: "Preparing your approval…",
  classify: "Looking up the PO in the PO ledger…",
  post_erp: "Posting Invoice to ERP…",
  escalate: "Raising a ServiceNow ticket…",
  draft_email: "Drafting the rejection email…",
  review_email: "Preparing the email for review…",
  notify: "Sending the email to the vendor…",
  complete: "Finalizing…",
};

// Agent-backed stages run a real LLM call and must stay visible long enough for
// the user to register them. If the backend buffers all events and sends them in
// one burst, this minimum prevents extraction from blinking past in ~500 ms.
const AGENT_STAGES = new Set<StageId>(["extraction", "draft_email"]);
const MIN_AGENT_STAGE_MS = 2500;

function freshState() {
  return {
    status: "idle" as RunStatus,
    items: [] as ConversationItem[],
    stageStatus: {} as Partial<Record<StageId, StageStatus>>,
    stepNumbers: {} as Partial<Record<StageId, number>>,
    currentStage: null as StageId | null,
    runStartedAt: null as number | null,
    runEndedAt: null as number | null,
    stageActivatedAt: {} as Partial<Record<StageId, number>>,
    intakeHasExactMessage: false,
    state: {} as PipelineState,
    currentInterrupt: null as InterruptInfo | null,
    invocationId: undefined as string | undefined,
    error: null as string | null,
    workingLabel: "Agent is working…",
    seen: {} as Partial<Record<StepKind, boolean>>,
    surfaces: {} as Record<string, AnySurface>,
  };
}

export const usePipelineStore = create<PipelineStore>((set, get) => {
  // Generation counter for the event loop: starting a new loop (or cancelRun)
  // invalidates any loop still waiting in a pacing sleep, so it can never write
  // stale items into a freshly reset view (replay seek / exit).
  let runGen = 0;

  // A fresh MessageProcessor per run avoids A2UI surface-id collisions.
  function buildEngine(): A2uiEngine {
    return createA2uiEngine(
      (action) => void get().submitAction(action),
      (surface) => set((s) => ({ surfaces: { ...s.surfaces, [surface.id]: surface } })),
    );
  }

  // Mark `stage` active (and everything before it done), with its working label.
  function activate(stage: StageId) {
    const activatedAt = Date.now();
    set((s) => {
      const next = { ...s.stageStatus };
      for (const id of STAGE_ORDER) {
        if (idx(id) < idx(stage) && next[id] === "active") next[id] = "done";
      }
      if (s.currentStage && s.currentStage !== stage && idx(s.currentStage) < idx(stage)) {
        next[s.currentStage] = "done";
      }
      next[stage] = stage === "complete" ? "done" : "active";
      return {
        stageStatus: next,
        currentStage: stage,
        workingLabel: WORKING_LABEL[stage],
        stageActivatedAt: { ...s.stageActivatedAt, [stage]: activatedAt },
      };
    });
    // A Model Armor scan gets its own card the moment it starts, so the centre pane
    // shows it running and then resolves it to passed / caught in place.
    if (isArmorStage(stage)) pushStepOnce(stage, stage);
  }

  function markDone(stage: StageId) {
    set((s) => ({ stageStatus: { ...s.stageStatus, [stage]: "done" } }));
  }

  // Revert a stage to "not reached" — used when a node executed but produced
  // no meaningful output (e.g. Agent Memory found no precedent for this
  // vendor/flag), so the rail shows it skipped instead of falsely "done".
  // Also clears currentStage if it still points at this stage, so the
  // immediately-following activate(next) doesn't re-mark it "done" via its
  // own "close the previous currentStage" logic.
  function skipStage(stage: StageId) {
    set((s) => {
      const next = { ...s.stageStatus };
      delete next[stage];
      return { stageStatus: next, currentStage: s.currentStage === stage ? null : s.currentStage };
    });
  }

  // Assign the next contiguous step number the first time a stage is shown.
  // Idempotent — a stage that fires multiple events (or loops, e.g. the email
  // revise cycle) keeps its first-assigned number rather than incrementing
  // again, so the visible sequence never skips or reuses a number.
  function assignStepNumber(stage: StageId): number {
    const existing = get().stepNumbers[stage];
    if (existing != null) return existing;
    const next = Object.keys(get().stepNumbers).length + 1;
    set((s) => ({ stepNumbers: { ...s.stepNumbers, [stage]: next } }));
    return next;
  }

  // Push a numbered "step" card exactly once per run (idempotent via `seen`).
  // Shared by the reactive pushes below and by one-shot event handlers (e.g.
  // screen_email_signal) whose backing state arrives on a specific event
  // rather than being reactively polled every tick.
  function pushStepOnce(step: StepKind, stageId: StageId) {
    if (get().seen[step]) return;
    assignStepNumber(stageId);
    set((s) => ({
      seen: { ...s.seen, [step]: true },
      items: [...s.items, { kind: "step" as const, id: nextId("step"), step }],
    }));
  }

  // Push inline result cards for deterministic nodes that carry no content msg,
  // so every significant node's output is visible on the UI. The card itself
  // renders the shared step header (number + badge + narrative) — no separate
  // text bubble needed.
  function emitSteps() {
    const { state } = get();
    type Push = { key: keyof PipelineState; step: StepKind; stageId: StageId };
    const pushes: Push[] = [
      { key: "po_match", step: "classify", stageId: "classify" },
      { key: "posting_result", step: "posting", stageId: "post_erp" },
      { key: "escalation_result", step: "escalation", stageId: "escalate" },
    ];
    for (const { key, step, stageId } of pushes) {
      if (state[key]) pushStepOnce(step, stageId);
    }
  }

  // Add a surface card exactly once (idempotent on surfaceId). Surface items are
  // driven by reliable node/interrupt signals (NOT by decoding A2UI parts), so
  // they render even on backends/browsers where the A2UI inline parts don't.
  function pushSurfaceOnce(surfaceId: string) {
    set((s) =>
      s.items.some((it) => it.kind === "surface" && it.surfaceId === surfaceId)
        ? s
        : { items: [...s.items, { kind: "surface", id: nextId("surface"), surfaceId }] },
    );
  }

  // Timestamp of the last item pushed to the conversation. Used to pace rapid
  // bursts so every card is visible long enough for the user to register it.
  let lastItemPushedAt = 0;
  const MIN_ITEM_GAP_MS = 700;

  async function renderContent(ev: Parameters<typeof getText>[0], stale: () => boolean) {
    const a2uiMessages = getA2uiMessages(ev);
    if (a2uiMessages.length > 0) {
      // Consume the A2UI protocol client-side (validation), guarded so a
      // renderer-lib hiccup is non-fatal. Display is native (see A2uiHost), so
      // we never depend on this for rendering. The accompanying markdown for
      // surface-backed nodes is suppressed below.
      try {
        get().engine.processMessages(a2uiMessages);
      } catch (e) {
        console.warn("A2UI engine processMessages failed (non-fatal):", e);
      }
      return;
    }
    const stageId = getStage(ev);
    if (stageId !== "complete") {
      const text = getText(ev).trim();
      const node = nodeName(ev);
      if (text && !INTERNAL_SIGNALS.has(text) && !(node && SUPPRESS_TEXT_NODES.has(node))) {
        // Enforce a minimum gap between items so fast deterministic nodes don't
        // burst all their cards onto the screen at once.
        if (get().status === "streaming") {
          const wait = MIN_ITEM_GAP_MS - (Date.now() - lastItemPushedAt);
          if (wait > 0) await sleep(wait);
          if (stale()) return;
        }
        lastItemPushedAt = Date.now();
        if (stageId) assignStepNumber(stageId);
        set((s) => ({
          items: [...s.items, { kind: "text", id: nextId("msg"), text, stageId: stageId ?? undefined }],
        }));
      }
    }
  }

  async function consume(source: AsyncIterable<AdkEvent>) {
    const myRun = ++runGen;
    const stale = () => myRun !== runGen;
    try {
      for await (const ev of source) {
        if (stale()) return;
        // Live runs are recorded for replay/export; a replay never re-records.
        if (!replayHooks.active) useRecorder.getState().push(ev);
        const err = getError(ev);
        if (err) {
          console.warn("Pipeline error frame:", err);
          set({ status: "error", error: err });
          return;
        }

        // Merge state first so branch decisions / step cards see fresh data.
        const delta = getStateDelta(ev);
        if (delta) set((s) => ({ state: { ...s.state, ...delta } }));

        // Mount ExtractionCard as soon as extraction_model lands in state
        // (written by _extraction_after callback at the end of the main
        // extraction_agent call). Shows "Waiting…" skeleton while the
        // line-items pass runs; fills in when emit_extraction_card fires and
        // invoice_record enters state. pushSurfaceOnce is idempotent — the
        // second call from the emit_extraction_card node event is a no-op.
        if (delta?.extraction_model) {
          pushSurfaceOnce("invoice_extraction");
        }

        const stage = getStage(ev);
        const interrupt = getInterrupt(ev);

        // A newly-reached forward stage: show it working, pause, then render.
        if (stage && idx(stage) > idx(get().currentStage)) {
          // Agent Memory has no event at all when recall_precedents finds no
          // precedent (it returns None — nothing to advance past). Its only
          // trace is the optimistic look-ahead activation left "active" from
          // when validation routed to human_review. Without this, activate()'s
          // own "close out any still-active earlier stage" logic would
          // silently flip it to "done" here, even though nothing was ever
          // found or shown.
          if (
            get().stageStatus.memory === "active" &&
            !get().items.some((it) => it.kind === "surface" && it.surfaceId === "agent_memory")
          ) {
            skipStage("memory");
          }
          activate(stage);
          if (get().status === "streaming") await sleep(STAGE_DELAY[stage]);
          if (stale()) return;
        }

        await renderContent(ev, stale);
        if (stale()) return;
        emitSteps();

        // Surface cards are driven by reliable signals (node identity + the
        // interrupt), independent of A2UI part decoding.

        // The screening agent's assessment card — shown on every run so its
        // rationale, detected signals, and per-signal evidence are visible up
        // front (and so a non-invoice is explained rather than silently discarded).
        if (nodeName(ev) === "screen_email_signal") {
          assignStepNumber("screening");
          pushSurfaceOnce("invoice_screening");
          pushStepOnce("screening_decision", "screening_decision");
        }

        // The deterministic size/complexity router — shown so the PDF
        // transport and recommended model tier it chose are visible before
        // extraction runs (a decision that otherwise has no other trace).
        if (nodeName(ev) === "route_extraction_strategy") {
          pushStepOnce("routing", "routing");
        }

        if (nodeName(ev) === "emit_extraction_card") {
          // The card itself renders the shared step header (number + badge +
          // narrative) — no separate text bubble needed.
          assignStepNumber("extraction");
          pushSurfaceOnce("invoice_extraction");
        }

        if (interrupt) {
          if (interrupt.kind === "approval") {
            assignStepNumber("approval");
            pushSurfaceOnce("approval_gate");
          } else if (interrupt.kind === "email_review") {
            assignStepNumber("review_email");
            pushSurfaceOnce(interrupt.interruptId);
          }
          set({
            currentInterrupt: interrupt,
            status: "awaiting_input",
            invocationId: interrupt.invocationId ?? get().invocationId,
          });
          continue; // stays active + awaiting; spinner hidden
        }

        // Optimistically light up the next expected stage so its "working"
        // label shows during the wait for its event, and pace it so fast
        // deterministic nodes don't blink past.
        if (stage) {
          const nxt = nextExpected(stage, get().state);
          if (nxt && idx(nxt) > idx(get().currentStage)) {
            // LLM-agent stages must be visible long enough so that a burst of
            // buffered SSE events doesn't make extraction look instantaneous.
            if (AGENT_STAGES.has(stage) && get().status === "streaming") {
              const elapsed = Date.now() - (get().stageActivatedAt[stage] ?? Date.now());
              const remaining = MIN_AGENT_STAGE_MS - elapsed;
              if (remaining > 0) await sleep(remaining);
              if (stale()) return;
            }
            // Agent Memory is genuinely optional — if this run's recall found
            // no precedent (no card was emitted for it), show it as skipped
            // rather than falsely "done", matching untaken branches like
            // Escalation/Post Invoice to ERP.
            if (stage === "memory" && getA2uiMessages(ev).length === 0) {
              skipStage(stage);
            } else {
              markDone(stage);
            }
            activate(nxt);
            if (get().status === "streaming") await sleep(STAGE_DELAY[nxt]);
            if (stale()) return;
            // A scan whose verdict is already in hand has played out: light the stage
            // behind it now, so the wait for that stage's own event (the screening
            // model, say) shows its working label instead of a lingering scan.
            const gate = get().currentStage;
            if (isArmorStage(gate) && armorVerdict(gate, get().state)) {
              const after = nextExpected(gate, get().state);
              if (after && idx(after) > idx(gate)) {
                markDone(gate);
                activate(after);
                if (get().status === "streaming") await sleep(STAGE_DELAY[after]);
                if (stale()) return;
              }
            }
          }
        }
      }
    } catch (e) {
      if (stale()) return;
      set({ status: "error", error: e instanceof Error ? e.message : String(e), runEndedAt: Date.now() });
      return;
    }
    if (stale()) return;
    useRecorder.getState().commit();
    set((s) => {
      if (s.status === "error" || s.status === "awaiting_input") return s;
      if (s.state.pipeline_status || s.currentStage === "complete") {
        return { status: "complete", runEndedAt: Date.now() };
      }
      return { status: s.status === "streaming" ? "idle" : s.status, runEndedAt: Date.now() };
    });
  }

  return {
    ...freshState(),
    engine: buildEngine(),

    async startPipeline(text = "Fetch and process the latest invoice.") {
      await client.reset(); // fresh agent session per invoice
      const hasExactMessage = /MSG_ID=/.test(text);
      set({
        ...freshState(),
        engine: buildEngine(),
        status: "streaming",
        runStartedAt: Date.now(),
        intakeHasExactMessage: hasExactMessage,
      });
      // Seed the first stage as active immediately. The `intake` node (AgentMail
      // fetch) runs server-side before its first SSE event arrives, so without
      // this the graph would sit all-dim ("Agent is working…") during that real
      // wait, then blink intake+extraction green at once when events cascade in.
      activate("intake");
      useRecorder.getState().begin();
      const pollToken = await client.run(text);
      await consume(pollSse(pollToken));
    },

    autoStart(messageId?: string) {
      // Idle guard: never interrupt an in-progress or completed run.
      if (get().status !== "idle") return;
      const text = messageId
        ? `Fetch and process the latest invoice. MSG_ID=${encodeURIComponent(messageId)}`
        : undefined;
      void get().startPipeline(text);
    },

    async submitAction(action: A2uiClientAction) {
      const intent = actionToResume(action, get().currentInterrupt?.interruptId);
      if (!intent) return;
      await get().submitResume(intent.interruptId, intent.value);
    },

    async submitResume(interruptId: string, value: unknown) {
      set({ status: "streaming", currentInterrupt: null });
      // In a replay the "decision" is the recorded one: the controller, which is
      // waiting at this boundary, plays the next recorded segment.
      if (replayHooks.active) {
        replayHooks.resume?.();
        return;
      }
      const { invocationId } = get();
      useRecorder.getState().startSegment();
      const pollToken = await client.resume(interruptId, value, invocationId);
      await consume(pollSse(pollToken));
    },

    reset() {
      void client.reset();
      set({ ...freshState(), engine: buildEngine() });
    },

    resetLocal() {
      set({ ...freshState(), engine: buildEngine() });
    },

    cancelRun() {
      runGen++;
    },

    replayBegin() {
      runGen++;
      lastItemPushedAt = 0;
      set({
        ...freshState(),
        engine: buildEngine(),
        status: "streaming",
        runStartedAt: Date.now(),
      });
      activate("intake");
    },

    async replayConsume(source: AsyncIterable<AdkEvent>) {
      await consume(source);
    },
  };
});

// In dev, force a full page reload when this module changes so the singleton
// store / A2UI engine never get left in a stale hot-reloaded state.
if (import.meta.hot) import.meta.hot.accept(() => window.location.reload());
