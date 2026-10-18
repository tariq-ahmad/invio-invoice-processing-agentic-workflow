import { useCallback, useEffect, useRef, useState } from "react";
import { BackgroundFx } from "./components/shell/BackgroundFx";
import { TopBar } from "./components/shell/TopBar";
import { PipelineTimeline } from "./components/timeline/PipelineTimeline";
import { ConversationPane } from "./components/chat/ConversationPane";
import { useInvoiceEvents } from "./hooks/useInvoiceEvents";
import { useAttentionAlert } from "./hooks/useAttentionAlert";
import { usePrintTheme } from "./hooks/usePrintTheme";
import { ReplayBar } from "./components/replay/ReplayBar";
import { Toaster } from "./components/shell/Toaster";

const MIN_PCT = 24;
const MAX_PCT = 72;
const STORAGE_KEY = "layout:left-pct";

function AppInner() {
  useInvoiceEvents();
  useAttentionAlert();
  usePrintTheme();

  // Draggable split between the pipeline graph and the A2UI cards. Persisted so
  // the chosen layout survives reloads.
  const mainRef = useRef<HTMLElement>(null);
  const asideRef = useRef<HTMLElement>(null);
  const livePct = useRef(40);
  const frame = useRef(0);
  const [leftPct, setLeftPct] = useState(() => {
    const saved = Number(localStorage.getItem(STORAGE_KEY));
    return saved >= MIN_PCT && saved <= MAX_PCT ? saved : 40;
  });
  const [dragging, setDragging] = useState(false);

  const leftPctRef = useRef(leftPct);
  useEffect(() => {
    leftPctRef.current = leftPct;
    localStorage.setItem(STORAGE_KEY, String(Math.round(leftPct)));
  }, [leftPct]);

  // While dragging, write the width straight to the DOM on rAF — no React
  // re-render per pointermove. The final value is committed to state on release.
  const onPointerMove = useCallback((e: PointerEvent) => {
    const el = mainRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const pct = ((e.clientX - rect.left) / rect.width) * 100;
    livePct.current = Math.min(MAX_PCT, Math.max(MIN_PCT, pct));
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      if (asideRef.current) asideRef.current.style.width = `${livePct.current}%`;
    });
  }, []);

  const stopDrag = useCallback(() => {
    if (frame.current) {
      cancelAnimationFrame(frame.current);
      frame.current = 0;
    }
    setLeftPct(livePct.current);
    setDragging(false);
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", stopDrag);
    document.body.style.userSelect = "";
    document.body.style.cursor = "";
  }, [onPointerMove]);

  const startDrag = useCallback(() => {
    livePct.current = leftPctRef.current;
    setDragging(true);
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", stopDrag);
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
  }, [onPointerMove, stopDrag]);

  // Double-click the handle to reset to the default split.
  const resetSplit = useCallback(() => setLeftPct(40), []);

  return (
    <div className="app-shell flex h-screen flex-col">
      <BackgroundFx />
      <TopBar />

      {/* Resizable 2-column: pipeline graph | A2UI cards */}
      <main ref={mainRef} className="app-main flex min-h-0 flex-1 overflow-hidden px-5 pb-5 pt-4 xl:px-7">
        <aside
          ref={asideRef}
          className="app-aside scroll-slim flex min-h-0 flex-col overflow-y-auto"
          style={{ width: `${leftPct}%` }}
        >
          <PipelineTimeline />
        </aside>

        {/* Drag handle */}
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize panels"
          onPointerDown={startDrag}
          onDoubleClick={resetSplit}
          title="Drag to resize · double-click to reset"
          className="no-print group relative mx-1 flex w-3 shrink-0 cursor-col-resize touch-none items-center justify-center"
        >
          <span
            className={`h-14 w-1 rounded-full transition-[background-color,height] duration-200 ${
              dragging ? "h-20 bg-brand-400" : "bg-wash-bold group-hover:h-20 group-hover:bg-brand-400/60"
            }`}
          />
        </div>

        <section className="app-pane flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-line bg-wash p-4">
          <ConversationPane />
          <ReplayBar />
        </section>
      </main>
      <Toaster />
    </div>
  );
}

export default function App() {
  return <AppInner />;
}
