import { usePipelineStore } from "../../store/usePipelineStore";
import { useFeedUi, type FeedMode } from "../../store/useFeedUi";
import { runStatusChip, type ChipTone } from "../../lib/runStatus";
import { cn } from "../../lib/utils";
import { ExportMenu } from "./ExportMenu";

const CHIP: Record<ChipTone, string> = {
  neutral: "border-line-strong bg-wash text-slate-300",
  brand: "border-brand-400/30 bg-brand-500/10 text-brand-200",
  amber: "border-amber-400/35 bg-amber-400/10 text-amber-200",
  green: "border-done-500/35 bg-done-500/10 text-done-300",
  rose: "border-rose-400/35 bg-rose-500/10 text-rose-200",
};

const MODES: { id: FeedMode; label: string; hint: string }[] = [
  { id: "auto", label: "Auto-fold", hint: "Fold finished steps into one-line summaries" },
  { id: "all", label: "Expand all", hint: "Show every step in full" },
  { id: "focus", label: "Focus", hint: "Show only the current step" },
];

const fv = (f?: { value?: string | null }) => (f?.value && String(f.value).trim() ? String(f.value).trim() : "");

function ModeSwitch() {
  const mode = useFeedUi((s) => s.mode);
  const setMode = useFeedUi((s) => s.setMode);
  return (
    <div role="radiogroup" aria-label="How to show steps" className="no-print flex rounded-xl bg-wash p-0.5">
      {MODES.map((m) => (
        <button
          key={m.id}
          type="button"
          role="radio"
          aria-checked={mode === m.id}
          title={m.hint}
          onClick={() => setMode(m.id)}
          className={cn(
            "rounded-[10px] px-2.5 py-1 text-xs font-medium transition-colors",
            mode === m.id ? "bg-brand-500/20 text-brand-200" : "text-slate-400 hover:text-slate-100",
          )}
        >
          {m.label}
        </button>
      ))}
    </div>
  );
}

/** Sticky strip pinned to the top of the feed: which invoice this is and where
 * it stands, so context survives scrolling. Also hosts the view/export controls. */
export function PaneHeader() {
  const rec = usePipelineStore((s) => s.state.invoice_record);
  const status = usePipelineStore((s) => s.status);
  const state = usePipelineStore((s) => s.state);
  const currentStage = usePipelineStore((s) => s.currentStage);
  const interrupt = usePipelineStore((s) => s.currentInterrupt);

  const chip = runStatusChip({ status, state, currentStage, interrupt });
  const vendor = fv(rec?.vendor);
  const invoiceNo = fv(rec?.invoice_no);
  const total = fv(rec?.total);
  const amount = total ? `${fv(rec?.currency)} ${total}`.trim() : "";
  const hasInvoice = !!(vendor || invoiceNo || amount);

  return (
    <div className="no-print glass-float sticky top-1 z-20 mb-1 flex items-center justify-between gap-3 rounded-2xl py-2 pl-2.5 pr-2">
      <div className="flex min-w-0 items-center gap-3">
        <span
          aria-hidden
          className="grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-brand-500/12 text-brand-300 ring-1 ring-brand-400/20"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 3h7l5 5v13H7z" />
            <path d="M14 3v5h5M10 13h6M10 17h6" />
          </svg>
        </span>
        {hasInvoice ? (
          <p className="min-w-0 truncate text-sm text-slate-300" title={[vendor, invoiceNo, amount].filter(Boolean).join(" · ")}>
            <span className="font-semibold text-slate-50">{vendor || "Unknown vendor"}</span>
            {invoiceNo && <span className="text-slate-400"> · {invoiceNo}</span>}
            {amount && <span className="font-semibold text-slate-100"> · {amount}</span>}
          </p>
        ) : (
          <p className="text-sm text-slate-500">{status === "idle" ? "No invoice yet" : "Reading the invoice…"}</p>
        )}
        {chip && (
          <span
            role="status"
            className={cn(
              "shrink-0 whitespace-nowrap rounded-full border px-2.5 py-0.5 text-[11px] font-semibold",
              CHIP[chip.tone],
            )}
          >
            {chip.label}
          </span>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <ModeSwitch />
        <ExportMenu />
      </div>
    </div>
  );
}
