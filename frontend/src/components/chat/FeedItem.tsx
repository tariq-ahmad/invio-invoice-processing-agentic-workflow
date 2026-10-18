import type { ReactNode } from "react";
import { usePipelineStore } from "../../store/usePipelineStore";
import { STAGES, type StageId } from "../../store/stageMap";
import { foldSummary } from "../../lib/foldSummary";
import { formatLatency } from "../../lib/duration";
import { cn } from "../../lib/utils";

/**
 * Wraps one feed card so a finished step can fold to a single summary line and
 * expand on click. The card stays mounted while folded (hidden, inert) so its
 * local state — a half-filled approval, an open evidence pane — is never lost.
 * Folding animates with a grid-rows transition (no height measuring).
 */
export function FeedItem({
  id,
  stage,
  folded,
  onToggle,
  children,
}: {
  id: string;
  stage: StageId | null;
  folded: boolean;
  onToggle: (nextFolded: boolean) => void;
  children: ReactNode;
}) {
  const state = usePipelineStore((s) => s.state);
  const step = usePipelineStore((s) => (stage ? s.stepNumbers[stage] : undefined));
  const def = stage ? STAGES[stage] : null;
  const summary = stage ? foldSummary(stage, state) : null;
  const foldable = stage != null;

  return (
    <div
      id={`feed-${id}`}
      data-feed-item
      data-stage={stage ?? undefined}
      data-folded={folded || undefined}
      className="group/feed relative shrink-0 scroll-mt-16"
    >
      {foldable && def && summary && (
        <button
          type="button"
          data-fold-row
          onClick={() => onToggle(false)}
          aria-expanded={!folded}
          aria-label={`${def.label}: ${summary.text}. Expand`}
          tabIndex={folded ? 0 : -1}
          className={cn(
            "card flex w-full items-center gap-3 rounded-xl px-4 text-left transition-[opacity,max-height,padding,margin] duration-300 hover:border-line-bold hover:bg-wash",
            folded ? "max-h-14 py-2.5 opacity-100" : "pointer-events-none max-h-0 overflow-hidden border-transparent py-0 opacity-0 shadow-none",
          )}
        >
          <span className="grid h-5 w-5 shrink-0 place-items-center rounded-full bg-done-500/15 text-done-400">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 13l4 4L19 7" />
            </svg>
          </span>
          <span className="shrink-0 text-sm font-semibold text-slate-100">
            {step != null ? `${step} · ${def.label}` : def.label}
          </span>
          <span className="min-w-0 flex-1 truncate text-sm text-slate-400">{summary.text}</span>
          {(summary.ms != null || summary.model) && (
            <span className="hidden shrink-0 items-center gap-1.5 font-mono text-[11px] text-slate-500 sm:flex">
              {summary.ms != null && <span>{formatLatency(summary.ms)}</span>}
              {summary.ms != null && summary.model && <span aria-hidden>·</span>}
              {summary.model && <span className="text-brand-300">{summary.model}</span>}
            </span>
          )}
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 text-slate-500" aria-hidden>
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      )}

      <div
        data-fold-body
        className={cn(
          "grid transition-[grid-template-rows] duration-300 ease-out",
          folded ? "grid-rows-[0fr]" : "grid-rows-[1fr]",
        )}
      >
        <div data-fold-inner className={cn("min-h-0", folded ? "overflow-hidden" : "overflow-visible")} inert={folded}>
          {children}
        </div>
      </div>

      {foldable && !folded && (
        <button
          type="button"
          data-fold-toggle
          onClick={() => onToggle(true)}
          aria-label={`Fold ${def?.label ?? "step"}`}
          title="Fold this step"
          className="no-print absolute -top-2 right-3 z-10 grid h-6 w-6 place-items-center rounded-full border border-line-strong bg-ink-850 text-slate-400 opacity-0 shadow-sm transition hover:text-slate-100 focus-visible:opacity-100 group-hover/feed:opacity-100"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
            <path d="M6 15l6-6 6 6" />
          </svg>
        </button>
      )}
    </div>
  );
}
