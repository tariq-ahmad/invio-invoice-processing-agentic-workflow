import { lazy, Suspense, useEffect, useRef } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { usePipelineStore } from "../../store/usePipelineStore";
import { useWorkingLabel } from "../../hooks/useWorkingLabel";
import { classifyError } from "../../api/eventInterpreter";
import { ResultSummaryCard } from "../results/ResultSummaryCard";
import { StepCard } from "../results/StepCard";
import { ArmorCard } from "../results/ArmorCard";
import { ARMOR_STAGES, type ArmorStageId } from "../../store/stageMap";
import { useFeedUi } from "../../store/useFeedUi";
import { findItemForStage, stageOfItem } from "../../lib/stageCards";
import { flashElement } from "../../lib/feedNav";
import { loadRecordingFile } from "../../replay/controller";
import { FeedItem } from "./FeedItem";
import { PaneHeader } from "./PaneHeader";
import { PrintHeader } from "./PrintHeader";
import { SkeletonCard, SKELETON_EXCLUDED } from "./SkeletonCard";

// The markdown renderer (react-markdown + remark-gfm) is the heaviest piece of
// the chat feed. Load it as its own chunk and prefetch it once the browser is
// idle, so it is off the critical path yet ready before the first card arrives.
// The A2UI surfaces (extraction / approval gate / email review) follow the same pattern.
const loadMessageBubble = () => import("./MessageBubble");
const loadA2uiHost = () => import("../../a2ui/A2uiHost");
const MessageBubble = lazy(() => loadMessageBubble().then((m) => ({ default: m.MessageBubble })));
const A2uiHost = lazy(() => loadA2uiHost().then((m) => ({ default: m.A2uiHost })));
if (typeof window !== "undefined") {
  const prefetch = () => {
    void loadMessageBubble();
    void loadA2uiHost();
  };
  const idle = (window as Window & { requestIdleCallback?: (cb: () => void) => number }).requestIdleCallback;
  if (idle) idle(prefetch);
  else setTimeout(prefetch, 1500);
}

export function ConversationPane() {
  const items = usePipelineStore((s) => s.items);
  const status = usePipelineStore((s) => s.status);
  const error = usePipelineStore((s) => s.error);
  const interrupt = usePipelineStore((s) => s.currentInterrupt);
  const pipelineStatus = usePipelineStore((s) => s.state.pipeline_status);
  const quarantineStage = usePipelineStore((s) => s.state.armor_quarantine?.stage);
  const state = usePipelineStore((s) => s.state);
  const stageStatus = usePipelineStore((s) => s.stageStatus);
  const currentStage = usePipelineStore((s) => s.currentStage);

  const mode = useFeedUi((s) => s.mode);
  const overrides = useFeedUi((s) => s.overrides);
  const setFolded = useFeedUi((s) => s.setFolded);
  const pin = useFeedUi((s) => s.pin);
  const flash = useFeedUi((s) => s.flash);

  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [items.length, status, pipelineStatus]);

  // Jump from the pipeline graph: scroll to the card and flash it.
  useEffect(() => {
    if (flash) flashElement(`feed-${flash.id}`);
  }, [flash]);

  // A new run starts from a clean slate of per-card fold choices.
  useEffect(() => {
    if (items.length === 0) useFeedUi.getState().clear();
  }, [items.length]);

  const empty = items.length === 0 && status === "idle";
  const runFinished = !!pipelineStatus || status === "complete" || status === "error";
  const lastId = items.length > 0 ? items[items.length - 1].id : null;

  // Focus mode shows a single card: the one the user jumped to, else the latest.
  const focusId = pin && items.some((it) => it.id === pin) ? pin : lastId;
  const visible = mode === "focus" ? items.filter((it) => it.id === focusId) : items;

  // Finished steps fold to a summary line; the active/latest step stays open.
  const isFolded = (id: string, stage: ReturnType<typeof stageOfItem>): boolean => {
    const choice = overrides[id];
    if (choice !== undefined) return !choice;
    if (mode !== "auto" || !stage) return false;
    if (stageStatus[stage] !== "done") return false;
    if (id === lastId && !runFinished) return false;
    return true;
  };

  // A placeholder for the step running right now, until its real card arrives.
  const skeletonStage =
    status === "streaming" &&
    currentStage &&
    !SKELETON_EXCLUDED.has(currentStage) &&
    !findItemForStage(items, currentStage, state)
      ? currentStage
      : null;

  return (
    <div className="app-feed scroll-slim flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-1 pb-6">
      <PaneHeader />
      <PrintHeader />
      {empty && <EmptyState />}

      <AnimatePresence initial={false}>
        {visible.map((item) => {
          const stage = stageOfItem(item, state);
          const card =
            item.kind === "text" ? (
              <Suspense fallback={null}>
                <MessageBubble text={item.text} stageId={item.stageId} />
              </Suspense>
            ) : item.kind === "step" ? (
              ARMOR_STAGES.includes(item.step as ArmorStageId) ? (
                <ArmorCard stage={item.step as ArmorStageId} />
              ) : (
                <StepCard step={item.step} />
              )
            ) : (
              <Suspense fallback={null}>
                <A2uiHost surfaceId={item.surfaceId} />
              </Suspense>
            );
          return (
            <FeedItem
              key={item.id}
              id={item.id}
              stage={stage}
              folded={isFolded(item.id, stage)}
              onToggle={(nextFolded) => setFolded(item.id, nextFolded)}
            >
              {card}
            </FeedItem>
          );
        })}
      </AnimatePresence>

      {interrupt && status === "awaiting_input" && <AwaitingBanner kind={interrupt.kind} />}

      {skeletonStage ? <SkeletonCard stage={skeletonStage} /> : status === "streaming" && <Thinking />}

      {error && <ErrorCard raw={error} />}

      {/* A caught email ends at the Model Armor card's Verdict, which already says what
          happened. Only an extraction-time quarantine has no such card, so it keeps this one. */}
      {pipelineStatus && status !== "streaming" && !(pipelineStatus === "quarantined" && quarantineStage !== "extraction") && (
        <ResultSummaryCard />
      )}

      <div ref={bottomRef} />
    </div>
  );
}

function EmptyState() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="grid flex-1 place-items-center py-20 text-center"
    >
      <div className="max-w-sm">
        {/* Accent inbox orb with a single slow "listening" breath */}
        <div className="relative mx-auto mb-6 grid h-20 w-20 place-items-center">
          <span className="pointer-events-none absolute inset-1 rounded-[42%] border-2 border-brand-400/40 anim-breathe" />
          <div className="relative grid h-16 w-16 place-items-center rounded-[42%] bg-gradient-to-br from-brand-400 to-brand-600 shadow-[0_10px_30px_-10px_var(--color-brand-500)] ring-1 ring-white/20">
            <span className="absolute left-3 top-3 h-3 w-3 rounded-full bg-white/35" />
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.8">
              <rect x="3" y="5" width="18" height="14" rx="2" />
              <path d="m3 7 9 6 9-6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
        </div>
        <h3 className="text-gradient text-lg font-semibold tracking-tight">Monitoring inbox, ready to process</h3>
        <p className="mt-2 text-sm leading-relaxed text-slate-400">
          Connected to AgentMail. A new invoice email starts the pipeline
          automatically, no manual trigger needed. Send an invoice to watch a
          full run.
        </p>
        <label className="mt-4 inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-line-strong bg-wash px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-line-bold hover:text-slate-50">
          Or load a recorded run to replay
          <input
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) void loadRecordingFile(f);
            }}
          />
        </label>
      </div>
    </motion.div>
  );
}

function AwaitingBanner({ kind }: { kind: string }) {
  const label = kind === "email_review" ? "Review the rejection email above" : "Your decision is needed above";
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="flex items-center gap-2 self-start rounded-full border border-amber-300/30 bg-amber-400/10 px-3.5 py-1.5 text-xs font-medium text-amber-100"
    >
      <span className="relative flex h-2 w-2">
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-300 opacity-70" />
        <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-300" />
      </span>
      {label}
    </motion.div>
  );
}

function ErrorCard({ raw }: { raw: string }) {
  const startPipeline = usePipelineStore((s) => s.startPipeline);
  const status = usePipelineStore((s) => s.status);
  const { message, hint, retryable } = classifyError(raw);
  const busy = status === "streaming";

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="rounded-xl border border-rose-500/30 bg-rose-500/[0.08] px-4 py-3 text-sm text-rose-200"
    >
      <div className="flex items-start gap-2.5">
        <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-md bg-rose-500/20 text-[11px] font-bold text-rose-100 ring-1 ring-rose-400/30">
          !
        </span>
        <div className="min-w-0 flex-1">
          <p className="font-medium text-rose-100">{message}</p>
          {hint && <p className="mt-0.5 text-xs text-rose-200/70">{hint}</p>}
          {retryable && (
            <button
              onClick={() => void startPipeline()}
              disabled={busy}
              className="mt-2.5 rounded-lg border border-rose-400/40 bg-rose-500/15 px-3 py-1.5 text-xs font-semibold text-rose-100 transition hover:bg-rose-500/25 disabled:opacity-50"
            >
              {busy ? "Retrying…" : "Try again"}
            </button>
          )}
        </div>
      </div>
    </motion.div>
  );
}

function Thinking() {
  const label = useWorkingLabel();
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="card flex items-center gap-2.5 self-start rounded-2xl px-4 py-2.5"
    >
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className="h-2 w-2 rounded-full bg-gradient-to-br from-brand-400 to-brand-600"
          animate={{ opacity: [0.3, 1, 0.3], y: [0, -3, 0] }}
          transition={{ duration: 1, repeat: Infinity, delay: i * 0.18 }}
        />
      ))}
      <motion.span
        key={label}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="ml-1 text-xs text-slate-400"
      >
        {label}
      </motion.span>
    </motion.div>
  );
}
