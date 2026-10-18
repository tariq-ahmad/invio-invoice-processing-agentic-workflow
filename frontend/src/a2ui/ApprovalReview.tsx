/**
 * Human-in-the-loop review card (the "approval_gate" surface).
 *
 * One calm column, in the order a reviewer actually reads it:
 *  1. Summary  — a plain-language briefing (LLM-written by the backend,
 *     app/review_summary.py): what the invoice is, why it needs a human, what to check.
 *  2. Evidence — collapsed by default; the original document and the extracted
 *     data, with a button to open them in a large window.
 *  3. Agent Memory — what was decided before for this vendor.
 *  4. Reasoning — the per-rule checks (facts -> criterion -> result) and the
 *     extraction model's own notes, collapsed by default.
 *  Then the decision: Approve / Reject, one tap on a reason, optional note. The
 *  structured payload is recorded as `approval_audit` (app/nodes/approval.py).
 *
 * REASONS below mirrors app/nodes/approval.py REASON_CODES — keep in sync.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion } from "framer-motion";
import { usePipelineStore } from "../store/usePipelineStore";
import { invoicePdfUrl } from "../api/client";
import type {
  FieldValue,
  InvoiceRecord,
  POMatchResult,
  ReviewSummary,
  RubricRule,
  SourceGrounding,
} from "../api/types";
import { cn, persistedId } from "../lib/utils";
import { staggerItem, staggerParent } from "../lib/motion";
import { RunCostProjection } from "./RunCostProjection";
import { useReplay } from "../replay/controller";

// ---------------------------------------------------------------------------
// Structured reason codes (mirror app/nodes/approval.py REASON_CODES)
// ---------------------------------------------------------------------------

type Decision = "approve" | "reject";

interface Reason {
  code: string;
  label: string;
  hint: string;
}

const REASONS: Record<Decision, Reason[]> = {
  approve: [
    { code: "VERIFIED_AGAINST_SOURCE", label: "Verified against source", hint: "Extracted values match the original document" },
    { code: "WITHIN_DELEGATED_AUTHORITY", label: "Within my approval authority", hint: "The amount is inside my delegated limit" },
    { code: "PO_CONFIRMED_WITH_REQUESTER", label: "PO / receipt confirmed", hint: "Confirmed with the requisitioner" },
    { code: "KNOWN_RECURRING_CHARGE", label: "Known recurring charge", hint: "Matches this vendor's regular pattern" },
    { code: "OTHER", label: "Other", hint: "Explain in the note" },
  ],
  reject: [
    { code: "AMOUNT_INCORRECT", label: "Amount incorrect", hint: "Total doesn't match what was ordered or delivered" },
    { code: "PO_MISSING_OR_INVALID", label: "PO missing or invalid", hint: "No usable purchase-order reference" },
    { code: "SUSPECTED_DUPLICATE", label: "Suspected duplicate", hint: "Looks like a repeat billing" },
    { code: "VENDOR_NOT_RECOGNISED", label: "Vendor not recognised", hint: "Not an approved supplier" },
    { code: "DOCUMENT_UNREADABLE", label: "Document unreadable / extraction wrong", hint: "The agent misread the source" },
    { code: "OTHER", label: "Other", hint: "Explain in the note" },
  ],
};

/** Which extracted fields a rule is about — used to mark the evidence the
 * leading finding refers to, so the reviewer knows where to look. */
const TRIGGER_FIELDS: Record<string, string[]> = {
  duplicate: ["invoice_no", "po_ref"],
  required_fields: ["vendor", "invoice_no", "po_ref", "total"],
  valid_total: ["total"],
  within_limit: ["total"],
  po_status: ["po_ref"],
  po_vendor: ["vendor", "po_ref"],
  po_amount: ["total", "po_ref"],
  line_items_complete: ["total"],
  reconciles: ["total"],
  well_formed: ["invoice_no", "currency"],
  grounded: ["vendor", "invoice_no", "po_ref", "total"],
};

/** The routine over-the-auto-approval-limit trigger; approving past any OTHER
 * failed rule is logged as an override. Mirrors the backend. */
const ROUTINE_RULE = "within_limit";

const fv = (f?: FieldValue) => (f?.value && String(f.value).trim() ? String(f.value) : "");
const num = (s?: string | null) => {
  const n = Number(String(s ?? "").replace(/[^0-9.-]+/g, ""));
  return Number.isFinite(n) && String(s ?? "").trim() !== "" ? n : NaN;
};
/** "4.1s" for >=1s, else "940ms". */
const fmtLatency = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`);
const money = (n: number) => n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Pre-rubric sessions (or a backend without build_rubric) only have flag
 * strings: degrade to one "flag" rule per string so nothing is hidden. */
function rulesFromFlags(flags: string[]): RubricRule[] {
  return flags.map((f, i) => ({
    id: `other_${i}`,
    label: "Policy finding",
    headline: "Policy finding",
    criterion: "Policy check",
    observed: f,
    status: "flag" as const,
    finding: f,
    rank: i + 1,
  }));
}

type EvidenceView = "extracted" | "original";

// ---------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------

export function ApprovalReview() {
  const rec = usePipelineStore((s) => s.state.invoice_record) as InvoiceRecord | undefined;
  const flags = usePipelineStore((s) => s.state.validation_flags) ?? [];
  const rubric = usePipelineStore((s) => s.state.validation_rubric);
  const messageId = usePipelineStore((s) => s.state.invoice_message_id);
  const precedent = usePipelineStore((s) => s.state.decision_precedents);
  const memorySummary = usePipelineStore((s) => s.state.memory_summary);
  const summary = usePipelineStore((s) => s.state.review_summary);
  const poMatch = usePipelineStore((s) => s.state.po_match);
  const policySnapshot = usePipelineStore((s) => s.state.policy_snapshot);
  const thoughtText = usePipelineStore((s) => s.state.extraction_thought_text);
  const model = usePipelineStore((s) => s.state.extraction_model);
  const interrupt = usePipelineStore((s) => s.currentInterrupt);
  const submitResume = usePipelineStore((s) => s.submitResume);
  const status = usePipelineStore((s) => s.status);

  const [expanded, setExpanded] = useState(false);
  const [sourceOpened, setSourceOpened] = useState(false);
  const [choice, setChoice] = useState<Decision | null>(null);
  const [reasonCode, setReasonCode] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [receipt, setReceipt] = useState<Receipt | null>(null);

  // In a replay the decision is the recorded one (played via the replay bar), so the
  // controls are read-only — otherwise a click could contradict what then happens.
  const replaying = useReplay((s) => s.active);
  const awaiting = status === "awaiting_input" && interrupt?.kind === "approval" && !replaying;
  const sourceMissing = !messageId;

  const rules = useMemo<RubricRule[]>(
    () => (rubric && rubric.length > 0 ? rubric : rulesFromFlags(flags)),
    [rubric, flags],
  );
  const flagged = useMemo(
    () => rules.filter((r) => r.status === "flag").sort((a, b) => (a.rank ?? 99) - (b.rank ?? 99)),
    [rules],
  );
  const passed = useMemo(() => rules.filter((r) => r.status === "pass"), [rules]);
  const primary = flagged[0];
  const highlightFields = useMemo(() => {
    if (!primary) return new Set<string>();
    // "Required" is defined by the live policy (Firestore), not a fixed four.
    const policyFields =
      primary.id === "required_fields" || primary.id === "grounded" ? policySnapshot?.required_fields : undefined;
    return new Set(policyFields ?? TRIGGER_FIELDS[primary.id] ?? []);
  }, [primary, policySnapshot]);

  const overriddenFindings = flagged.filter((r) => r.id !== ROUTINE_RULE);
  const isOverride = choice === "approve" && overriddenFindings.length > 0;
  const canSubmit = awaiting && !!choice && !!reasonCode;

  const pickChoice = (d: Decision) => {
    if (d !== choice) setReasonCode(null);
    setChoice(d);
  };

  const submit = () => {
    if (!interrupt || !choice || !reasonCode || !canSubmit) return;
    const decidedAt = new Date().toISOString();
    const reason = REASONS[choice].find((r) => r.code === reasonCode);
    setReceipt({
      decision: choice,
      reasonLabel: reason?.label ?? reasonCode,
      reasonCode,
      note: note.trim(),
      trigger: primary?.headline ?? primary?.label ?? "Manual review",
      override: isOverride,
      at: decidedAt,
    });
    void submitResume(interrupt.interruptId, {
      decision: choice,
      reason_code: reasonCode,
      note: note.trim(),
      source_document_opened: sourceOpened,
      reviewer: persistedId("ap-copilot-user"),
      decided_at_client: decidedAt,
    });
  };

  const currency = fv(rec?.currency);
  const amount = `${currency} ${fv(rec?.total) || "—"}`.trim();
  const evidence = (view: EvidenceView, tall: boolean) =>
    view === "original" ? (
      <OriginalPane messageId={messageId} onOpened={() => setSourceOpened(true)} tall={tall} />
    ) : (
      <ExtractedPane rec={rec} highlight={highlightFields} primary={primary} />
    );

  return (
    <div className="@container space-y-3">
      {/* ── Header ───────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-slate-100">Review before this is posted</p>
          <p className="mt-0.5 text-xs text-slate-400">
            {fv(rec?.vendor) || "Unknown vendor"}
            {fv(rec?.invoice_no) ? ` · ${fv(rec?.invoice_no)}` : ""} ·{" "}
            <span className="font-semibold text-slate-200">{amount}</span>
          </p>
        </div>
        {awaiting && (
          <span className="shrink-0 whitespace-nowrap rounded-full border border-amber-400/40 bg-amber-400/[0.08] px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-amber-300">
            Awaiting you
          </span>
        )}
      </div>

      {/* ── 1 · Summary ─────────────────────────────────────────────────── */}
      <SummaryCard summary={summary} flagged={flagged} poMatch={poMatch} />

      {/* ── 2 · Evidence (collapsed; expands to a large window) ─────────── */}
      <Collapsible
        title="Evidence"
        subtitle={sourceMissing ? "Extracted data · original unavailable" : "Original document and extracted data"}
        right={
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="flex shrink-0 items-center gap-1.5 rounded-lg border border-line-strong bg-wash px-2.5 py-1 text-[11px] font-semibold text-slate-300 transition hover:border-line-bold hover:text-slate-100"
          >
            <ExpandIcon /> Expand
          </button>
        }
      >
        <EvidenceInline sourceMissing={sourceMissing} render={evidence} />
      </Collapsible>

      {/* ── 3 · Agent Memory ────────────────────────────────────────────── */}
      <MemoryCard precedent={precedent} summary={memorySummary} />

      {/* ── 4 · Reasoning (collapsed) ───────────────────────────────────── */}
      <Collapsible
        title="Reasoning"
        subtitle={`${flagged.length} flagged · ${passed.length} passed${thoughtText ? " · model notes" : ""}`}
      >
        <div className="space-y-2.5">
          <Rubric flagged={flagged} passed={passed} />
          {thoughtText && <ModelReasoning text={thoughtText} model={model} />}
        </div>
      </Collapsible>

      {/* ── Cost projection (whole invoice, same format as the agent cards) ─ */}
      <RunCostProjection />

      {/* ── Decision ────────────────────────────────────────────────────── */}
      <div className="space-y-3 rounded-xl border border-line-strong bg-wash p-3.5">
        {receipt ? (
          <DecisionReceipt receipt={receipt} status={status} />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2.5">
              <DecisionButton kind="approve" selected={choice === "approve"} disabled={!awaiting} onClick={() => pickChoice("approve")} />
              <DecisionButton kind="reject" selected={choice === "reject"} disabled={!awaiting} onClick={() => pickChoice("reject")} />
            </div>

            <AnimatePresence initial={false}>
              {choice && (
                <motion.div
                  key={choice}
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: "auto", opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                  transition={{ duration: 0.2 }}
                  className="overflow-hidden"
                >
                  <div className="space-y-2.5 pt-0.5">
                    <div role="radiogroup" aria-label="Decision reason" className="flex flex-wrap gap-1.5">
                      {REASONS[choice]
                        .filter((r) => !(sourceMissing && r.code === "VERIFIED_AGAINST_SOURCE"))
                        .map((r) => (
                          <button
                            key={r.code}
                            type="button"
                            role="radio"
                            aria-checked={reasonCode === r.code}
                            title={r.hint}
                            disabled={!awaiting}
                            onClick={() => setReasonCode(r.code)}
                            className={cn(
                              "rounded-full border px-3 py-1.5 text-xs font-medium transition disabled:opacity-50",
                              reasonCode === r.code
                                ? choice === "approve"
                                  ? "border-brand-400/70 bg-brand-500/25 text-brand-200 ring-1 ring-brand-400/30"
                                  : "border-rose-400/60 bg-rose-500/15 text-rose-200 ring-1 ring-rose-400/25"
                                : "border-line-strong bg-wash text-slate-300 hover:border-line-bold hover:text-slate-100",
                            )}
                          >
                            {r.label}
                          </button>
                        ))}
                    </div>
                    {isOverride && (
                      <p className="flex items-start gap-2 rounded-lg border border-amber-400/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
                        <WarnDot />
                        <span>
                          Approving past{" "}
                          <span className="font-semibold">{overriddenFindings.map((r) => r.headline ?? r.label).join("; ")}</span>{" "}
                          is logged as an override.
                        </span>
                      </p>
                    )}
                    <textarea
                      value={note}
                      onChange={(e) => setNote(e.target.value)}
                      disabled={!awaiting}
                      rows={2}
                      maxLength={1000}
                      aria-label="Note (optional)"
                      placeholder="Add a note (optional)"
                      className="glass w-full resize-y rounded-lg px-3 py-2 text-sm leading-relaxed text-slate-100 outline-none transition focus:border-brand-400/60 focus:ring-1 focus:ring-brand-400/30 disabled:opacity-60"
                    />
                  </div>
                </motion.div>
              )}
            </AnimatePresence>

            <button
              type="button"
              onClick={submit}
              disabled={!canSubmit}
              className={cn(
                "w-full rounded-xl px-4 py-2.5 text-sm font-semibold transition active:scale-[0.99] disabled:cursor-not-allowed disabled:active:scale-100",
                canSubmit
                  ? choice === "reject"
                    ? "bg-gradient-to-r from-rose-500 to-rose-600 text-white shadow-lg shadow-rose-500/25 hover:brightness-110"
                    : "bg-gradient-to-r from-live-500 to-live-600 text-white shadow-lg shadow-live-500/30 hover:brightness-110"
                  : "border border-line-strong bg-wash text-slate-500",
              )}
            >
              {!choice
                ? "Choose a decision"
                : !reasonCode
                  ? "Pick a reason to continue"
                  : choice === "approve"
                    ? isOverride
                      ? "Confirm override & post"
                      : "Confirm approval & post"
                    : "Confirm rejection & notify vendor"}
            </button>
          </>
        )}
        <p className="text-center text-[11px] text-slate-500">
          Recorded to the audit trail: decision · reason · note · reviewer · time
        </p>
      </div>

      {expanded && (
        <EvidenceModal
          onClose={() => setExpanded(false)}
          title={`${fv(rec?.vendor) || "Invoice"} · ${fv(rec?.invoice_no) || "evidence"}`}
        >
          <div className="grid min-h-0 flex-1 gap-3 overflow-auto p-4 @4xl:grid-cols-2">
            {evidence("original", true)}
            {evidence("extracted", true)}
          </div>
        </EvidenceModal>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Summary / memory / collapsible frame
// ---------------------------------------------------------------------------

function SummaryCard({
  summary,
  flagged,
  poMatch,
}: {
  summary?: ReviewSummary;
  flagged: RubricRule[];
  poMatch?: POMatchResult;
}) {
  const paras = (summary?.text ?? "")
    .split(/\n\s*\n/)
    .map((p) => p.trim())
    .filter(Boolean);
  return (
    <div className="rounded-xl border border-amber-400/25 bg-amber-400/[0.05] p-3.5">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-amber-300">
          <SparkIcon /> Summary
        </p>
        {summary?.source === "llm" && (
          <span className="rounded border border-line-strong bg-wash px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            AI-generated{summary.model ? ` · ${summary.model}` : ""}
            {summary.latency_ms != null ? ` · ${fmtLatency(summary.latency_ms)}` : ""}
          </span>
        )}
      </div>
      {summary ? (
        <div className="space-y-2 text-[13px] leading-relaxed text-slate-200">
          {paras.map((p, i) => (
            <p key={i}>
              <BoldText text={p} />
            </p>
          ))}
        </div>
      ) : (
        <p className="animate-pulse text-xs text-slate-400">Preparing a summary…</p>
      )}
      {(flagged.length > 0 || poMatch) && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-amber-400/15 pt-2.5">
          {flagged.map((r) => (
            <span
              key={r.id}
              className="inline-flex items-center gap-1 rounded-full border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 text-[11px] text-amber-200"
            >
              <FlagIcon size={9} /> {r.headline ?? r.label}
            </span>
          ))}
          {poMatch && (
            <span className="inline-flex items-center rounded-full border border-line-strong bg-wash px-2 py-0.5 font-mono text-[11px] text-slate-300">
              {poMatch.matched
                ? [
                    poMatch.po_number,
                    poMatch.po_amount != null ? `${poMatch.currency ?? ""} ${money(poMatch.po_amount)}`.trim() : null,
                    poMatch.po_status,
                  ]
                    .filter(Boolean)
                    .join(" · ")
                : "No PO match"}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/** Renders **bold** spans from the summary as <strong>; everything else is plain
 * text (never HTML), so model output can't inject markup. */
function BoldText({ text }: { text: string }) {
  return (
    <>
      {text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
        part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
          <strong key={i} className="font-semibold text-slate-50">
            {part.slice(2, -2)}
          </strong>
        ) : (
          <span key={i}>{part.replace(/\*\*/g, "")}</span>
        ),
      )}
    </>
  );
}

function MemoryCard({
  precedent,
  summary,
}: {
  precedent?: DecisionPrecedent;
  summary?: ReviewSummary;
}) {
  if (!precedent) return null;
  const has = precedent.total_count > 0;
  // The note a model wrote from the precedent tally; the tally's own template
  // sentence is what shows when there is no model note (or it fell back).
  const text = summary?.text?.trim() || precedent.narrative;
  return (
    <div
      className={cn(
        "rounded-xl border px-3.5 py-3",
        has ? "border-violet-400/25 bg-violet-500/[0.06]" : "border-line-strong bg-wash",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <p className={cn("text-[11px] font-semibold uppercase tracking-wider", has ? "text-violet-300" : "text-slate-400")}>
          Agent Memory
        </p>
        {has && summary?.source === "llm" && (
          <span className="rounded border border-line-strong bg-wash px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            AI-generated{summary.model ? ` · ${summary.model}` : ""}
            {summary.latency_ms != null ? ` · ${fmtLatency(summary.latency_ms)}` : ""}
          </span>
        )}
      </div>
      <p className={cn("mt-1 text-[13px] leading-relaxed", has ? "text-violet-100" : "text-slate-400")}>
        {has ? <BoldText text={text} /> : "No prior decisions on file for this vendor yet."}
      </p>
      {has && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
          <span className="rounded-full border border-violet-400/25 bg-violet-400/10 px-2 py-0.5 text-violet-200">
            {precedent.approve_count ?? 0} approved
          </span>
          <span className="rounded-full border border-violet-400/25 bg-violet-400/10 px-2 py-0.5 text-violet-200">
            {precedent.reject_count ?? 0} rejected
          </span>
          {precedent.recommendation && (
            <span className="rounded-full border border-violet-300/40 bg-violet-400/15 px-2 py-0.5 font-semibold text-violet-100">
              Memory leans {precedent.recommendation}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

/** The slice of decision_precedents the Agent Memory card reads. */
type DecisionPrecedent = {
  total_count: number;
  narrative: string;
  approve_count?: number;
  reject_count?: number;
  recommendation?: "approve" | "reject" | null;
};

function Collapsible({
  title,
  subtitle,
  right,
  children,
}: {
  title: string;
  subtitle?: string;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl border border-line-strong bg-wash">
      <div className="flex items-center gap-2 pr-3">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-2.5 px-3.5 py-3 text-left"
        >
          <Chevron open={open} />
          <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-300">{title}</span>
          {subtitle && <span className="min-w-0 truncate text-xs text-slate-500">{subtitle}</span>}
        </button>
        {right}
      </div>
      {open && <div className="border-t border-line p-3">{children}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Evidence
// ---------------------------------------------------------------------------

function EvidenceInline({
  sourceMissing,
  render,
}: {
  sourceMissing: boolean;
  render: (view: EvidenceView, tall: boolean) => React.ReactNode;
}) {
  const [view, setView] = useState<EvidenceView>("extracted");
  const opts: Array<[EvidenceView, string]> = [
    ["extracted", "Extracted data"],
    ["original", "Original document"],
  ];
  return (
    <div className="space-y-2.5">
      <div role="tablist" aria-label="Evidence view" className="inline-flex rounded-lg border border-line-strong bg-wash p-0.5">
        {opts.map(([v, label]) => (
          <button
            key={v}
            role="tab"
            aria-selected={view === v}
            onClick={() => setView(v)}
            className={cn(
              "rounded-md px-3 py-1 text-[11px] font-semibold transition",
              view === v ? "bg-brand-500/25 text-brand-200" : "text-slate-400 hover:text-slate-200",
            )}
          >
            {label}
          </button>
        ))}
      </div>
      {sourceMissing && view === "extracted" && (
        <p className="flex items-start gap-2 rounded-lg border border-amber-400/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          <WarnDot /> The original attachment can’t be retrieved for this run — check the source email before deciding.
        </p>
      )}
      {render(view, false)}
    </div>
  );
}

function EvidenceModal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const close = useCallback(onClose, [onClose]);
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [close]);
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm sm:p-8"
      onClick={close}
      role="dialog"
      aria-modal="true"
      aria-label="Evidence"
    >
      <motion.div
        initial={{ opacity: 0, scale: 0.97, y: 10 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={{ duration: 0.18 }}
        className="@container flex w-full max-w-[1400px] flex-col overflow-hidden rounded-2xl border border-line-strong bg-ink-950 shadow-2xl"
        style={{ height: "min(92vh, 1000px)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-line-strong px-4 py-2.5">
          <span className="truncate text-sm font-semibold text-slate-100">
            Evidence <span className="font-normal text-slate-400">· {title}</span>
          </span>
          <button
            onClick={close}
            className="rounded-md px-2 py-1 text-[11px] text-slate-400 transition hover:bg-wash-strong hover:text-slate-100"
          >
            esc · close
          </button>
        </div>
        {children}
      </motion.div>
    </div>,
    document.body,
  );
}

// ---------------------------------------------------------------------------
// Decision controls + receipt
// ---------------------------------------------------------------------------

interface Receipt {
  decision: Decision;
  reasonLabel: string;
  reasonCode: string;
  note: string;
  trigger: string;
  override: boolean;
  at: string;
}

function DecisionButton({
  kind,
  selected,
  disabled,
  onClick,
}: {
  kind: Decision;
  selected: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  const approve = kind === "approve";
  return (
    <motion.button
      type="button"
      aria-pressed={selected}
      whileTap={disabled ? undefined : { scale: 0.98 }}
      whileHover={disabled || selected ? undefined : { y: -1 }}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "rounded-xl border px-3.5 py-2.5 text-left transition disabled:opacity-50",
        selected
          ? approve
            ? "border-brand-400/70 bg-brand-500/15 ring-1 ring-brand-400/30"
            : "border-rose-400/60 bg-rose-500/10 ring-1 ring-rose-400/25"
          : "border-line-strong bg-wash hover:border-line-bold hover:bg-wash-strong",
      )}
    >
      <span className="block text-sm font-semibold text-slate-100">{approve ? "Approve & post" : "Reject & notify vendor"}</span>
      <span className="mt-0.5 block text-[11px] leading-snug text-slate-400">
        {approve ? "Continue to posting" : "Draft a rejection email for your review"}
      </span>
    </motion.button>
  );
}

function DecisionReceipt({ receipt, status }: { receipt: Receipt; status: string }) {
  const busy = status === "streaming";
  const approve = receipt.decision === "approve";
  const headline = approve
    ? busy
      ? receipt.override ? "Override recorded — posting to ERP" : "Approved — posting to ledger"
      : receipt.override ? "Override recorded & posted" : "Approved & posted"
    : busy
      ? "Rejecting — notifying vendor"
      : "Rejected — vendor notified";
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn("overflow-hidden rounded-xl ring-1", approve ? "bg-live-500/10 ring-live-500/30" : "bg-amber-500/10 ring-amber-400/30")}
    >
      <div className={cn("flex items-center gap-2 px-4 py-2.5 text-sm font-semibold", approve ? "text-live-300" : "text-amber-200")}>
        {busy ? <Spinner /> : <CheckMini />}
        {headline}
      </div>
      <dl className="grid gap-x-4 gap-y-1.5 border-t border-line-strong bg-wash px-4 py-3 text-xs @lg:grid-cols-2">
        <ReceiptRow label="Reason" value={receipt.reasonLabel} mono={receipt.reasonCode} />
        <ReceiptRow label="Primary trigger" value={receipt.trigger} />
        <ReceiptRow label="Decided" value={new Date(receipt.at).toLocaleString()} />
        {receipt.note && <ReceiptRow label="Note" value={receipt.note} />}
      </dl>
    </motion.div>
  );
}

function ReceiptRow({ label, value, mono }: { label: string; value: string; mono?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="text-slate-200">
        {value}
        {mono && <span className="ml-1.5 font-mono text-[11px] text-slate-500">{mono}</span>}
      </dd>
    </div>
  );
}

function PaneFrame({
  title,
  badge,
  right,
  children,
}: {
  title: string;
  badge?: React.ReactNode;
  right?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col overflow-hidden rounded-xl border border-line-strong bg-wash">
      <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-xs font-semibold text-slate-200">{title}</span>
          {badge}
        </div>
        {right}
      </div>
      {children}
    </div>
  );
}

function OriginalPane({ messageId, onOpened, tall }: { messageId?: string; onOpened: () => void; tall?: boolean }) {
  const [loaded, setLoaded] = useState(false);
  const url = messageId ? invoicePdfUrl(messageId) : null;
  return (
    <PaneFrame
      title="Original document"
      badge={
        url ? (
          <span className="shrink-0 rounded border border-brand-400/30 bg-brand-500/10 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-brand-300">
            Unmodified source
          </span>
        ) : (
          <span className="shrink-0 rounded border border-amber-400/40 bg-amber-500/10 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-amber-300">
            Unavailable
          </span>
        )
      }
      right={
        url && (
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={onOpened}
            className="flex shrink-0 items-center gap-1 text-[11px] text-slate-400 transition hover:text-slate-100"
          >
            Open <ExternalLinkIcon />
          </a>
        )
      }
    >
      {url ? (
        <div className={cn("relative bg-slate-200/90", tall ? "h-full min-h-[36rem]" : "h-[24rem]")}>
          {!loaded && (
            <div className="absolute inset-0 grid place-items-center text-xs text-slate-500">Loading the vendor’s PDF…</div>
          )}
          <iframe
            title="Original vendor invoice"
            src={`${url}#view=FitH`}
            onLoad={() => {
              setLoaded(true);
              onOpened();
            }}
            className="relative h-full w-full border-0"
          />
        </div>
      ) : (
        <div className="grid h-40 place-items-center px-4 text-center text-xs text-amber-200/90">
          The original attachment can’t be retrieved for this run. Check the source email before deciding — this is
          recorded in the audit trail.
        </div>
      )}
      {url && (
        <p className="border-t border-line px-3 py-1.5 text-[11px] text-slate-500">
          Fetched live from the vendor’s email — the exact file the agent read, not a summary.
        </p>
      )}
    </PaneFrame>
  );
}

const FIELD_ROWS: Array<[keyof InvoiceRecord, string]> = [
  ["vendor", "Vendor"],
  ["invoice_no", "Invoice no."],
  ["po_ref", "PO reference"],
  ["total", "Total"],
  ["currency", "Currency"],
];

function ExtractedPane({
  rec,
  highlight,
  primary,
}: {
  rec?: InvoiceRecord;
  highlight: Set<string>;
  primary?: RubricRule;
}) {
  const items = rec?.line_items ?? [];
  const total = num(rec?.total?.value);
  const lineSum = items.reduce((s, li) => s + (Number.isFinite(num(li.amount)) ? num(li.amount) : 0), 0);
  const canReconcile = items.length > 0 && Number.isFinite(total) && rec?.line_items_complete !== false;
  const reconciles = canReconcile && Math.abs(lineSum - total) <= Math.max(Math.abs(total) * 0.01, 0.01);

  return (
    <PaneFrame
      title="What the agent extracted"
      right={
        highlight.size > 0 ? (
          <span className="flex shrink-0 items-center gap-1 text-[11px] text-amber-300">
            <FlagIcon size={10} /> {primary?.headline ?? primary?.label ?? "trigger"}
          </span>
        ) : undefined
      }
    >
      <motion.div variants={staggerParent} initial="initial" animate="animate" className="divide-y divide-line">
        {FIELD_ROWS.map(([key, label]) => {
          const f = rec?.[key] as FieldValue | undefined;
          const value = fv(f);
          const hot = highlight.has(key);
          return (
            <motion.div
              key={key}
              variants={staggerItem}
              className={cn("flex items-center justify-between gap-3 px-3 py-2", hot && "bg-amber-400/[0.07] shadow-[inset_2px_0_0_0_rgba(251,191,36,0.8)]")}
            >
              <span className="flex items-center gap-1.5 text-xs text-slate-500">
                {hot && <span className="text-amber-300"><FlagIcon size={10} /></span>}
                {label}
              </span>
              <span className="flex min-w-0 items-center gap-2">
                {value ? (
                  <span className="truncate text-sm font-medium text-slate-100">{value}</span>
                ) : (
                  <span className="text-sm italic text-slate-500">Not found</span>
                )}
                <GroundingBadge value={value ? f?.source_grounding : "not_found"} />
              </span>
            </motion.div>
          );
        })}
      </motion.div>

      {items.length > 0 && (
        <div className="border-t border-line-strong">
          <div className="flex items-center justify-between px-3 pt-2 text-[11px] uppercase tracking-wider text-slate-500">
            <span>Line items ({items.length})</span>
            {rec?.line_items_complete === false && <span className="text-amber-300">incomplete</span>}
          </div>
          <div className="scroll-slim max-h-36 overflow-y-auto px-3 py-1.5">
            {items.map((li, i) => (
              <div key={i} className="flex items-center justify-between gap-3 py-0.5 text-xs text-slate-300">
                <span className="truncate">{li.name || "—"}</span>
                <span className="shrink-0 font-mono text-slate-400">{li.amount || "—"}</span>
              </div>
            ))}
          </div>
          {canReconcile && (
            <div
              className={cn(
                "flex items-center justify-between gap-2 border-t border-line px-3 py-2 text-[11px]",
                reconciles ? "text-slate-400" : "bg-amber-400/[0.07] text-amber-200",
              )}
            >
              <span>Σ line items {money(lineSum)}</span>
              <span className="flex items-center gap-1 font-semibold">
                {reconciles ? <CheckDot /> : <WarnDot />}
                {reconciles ? "= total" : `≠ total ${money(total)}`}
              </span>
            </div>
          )}
        </div>
      )}
    </PaneFrame>
  );
}

const GROUNDING_LABEL: Record<SourceGrounding, string> = {
  grounded: "Grounded",
  inferred: "Inferred",
  not_found: "Not found",
};

function GroundingBadge({ value }: { value?: SourceGrounding }) {
  const g = value ?? "not_found";
  const cls =
    g === "grounded"
      ? "text-live-400 bg-live-500/10 border-live-400/30"
      : g === "inferred"
        ? "text-amber-300 bg-amber-400/10 border-amber-400/30"
        : "text-rose-300 bg-rose-400/10 border-rose-400/30";
  return (
    <span
      title={
        g === "grounded"
          ? "Read directly off the document"
          : g === "inferred"
            ? "Interpreted from an ambiguous or degraded source — verify it"
            : "Not present in the document"
      }
      className={cn("shrink-0 rounded-full border px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide", cls)}
    >
      {GROUNDING_LABEL[g]}
    </span>
  );
}

function Rubric({ flagged, passed }: { flagged: RubricRule[]; passed: RubricRule[] }) {
  const [showPassed, setShowPassed] = useState(false);
  return (
    <div className="overflow-hidden rounded-xl border border-line-strong bg-wash">
      <div className="hidden grid-cols-[1.4rem_1.2fr_1.3fr_1.3fr_4.5rem] gap-3 border-b border-line px-3 py-1.5 text-[11px] uppercase tracking-wider text-slate-500 @xl:grid">
        <span />
        <span>Rule</span>
        <span>Policy criterion</span>
        <span>Observed on this invoice</span>
        <span className="text-right">Result</span>
      </div>
      {flagged.map((r) => (
        <RubricRow key={r.id} rule={r} />
      ))}
      {passed.length > 0 && (
        <>
          <button
            type="button"
            onClick={() => setShowPassed((v) => !v)}
            aria-expanded={showPassed}
            className="flex w-full items-center gap-2 border-t border-line px-3 py-2 text-left text-xs text-slate-400 transition hover:text-slate-200"
          >
            <Chevron open={showPassed} />
            <CheckDot />
            <span>
              <span className="font-semibold text-slate-300">{passed.length} checks passed</span>
            </span>
          </button>
          {showPassed && passed.map((r) => <RubricRow key={r.id} rule={r} />)}
        </>
      )}
    </div>
  );
}

function RubricRow({ rule }: { rule: RubricRule }) {
  const bad = rule.status === "flag";
  return (
    <div
      className={cn(
        "grid gap-x-3 gap-y-1 border-t border-line px-3 py-2.5 text-xs first:border-t-0 @xl:grid-cols-[1.4rem_1.2fr_1.3fr_1.3fr_4.5rem] @xl:items-start",
        bad && rule.rank === 1 && "bg-amber-400/[0.05]",
      )}
    >
      <span className="hidden @xl:block">
        {bad ? (
          <span
            className={cn(
              "grid h-4.5 w-4.5 place-items-center rounded text-[11px] font-bold",
              rule.rank === 1 ? "bg-amber-400/25 text-amber-200" : "bg-wash-strong text-slate-400",
            )}
          >
            {rule.rank}
          </span>
        ) : (
          <CheckDot />
        )}
      </span>
      <span className={cn("font-medium", bad ? "text-slate-100" : "text-slate-300")}>{rule.label}</span>
      <span className="text-slate-400">
        <span className="mr-1 text-[11px] uppercase tracking-wider text-slate-600 @xl:hidden">Criterion</span>
        {rule.criterion}
      </span>
      <span className={bad ? "text-slate-200" : "text-slate-400"}>
        <span className="mr-1 text-[11px] uppercase tracking-wider text-slate-600 @xl:hidden">Observed</span>
        {rule.observed}
      </span>
      <span
        className={cn(
          "text-[11px] font-semibold uppercase tracking-wide @xl:text-right",
          bad ? "text-amber-300" : "text-live-300",
        )}
      >
        {bad ? (rule.rank === 1 ? "Primary" : "Flagged") : "Pass"}
      </span>
    </div>
  );
}

function ModelReasoning({ text, model }: { text: string; model?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl border border-amber-400/15 bg-amber-500/[0.04]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3.5 py-2 text-left text-xs text-amber-300/90 transition hover:text-amber-200"
      >
        <Chevron open={open} />
        <span className="font-semibold">Extraction reasoning</span>
        <span className="text-slate-500">· the model’s own notes{model ? ` (${model})` : ""}</span>
      </button>
      {open && (
        <pre className="scroll-slim max-h-72 overflow-auto whitespace-pre-wrap break-words border-t border-amber-400/10 px-3.5 py-2.5 font-mono text-[11px] leading-relaxed text-slate-300/80">
          {text}
        </pre>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

function Spinner() {
  return (
    <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.3" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

function CheckMini({ small }: { small?: boolean }) {
  return (
    <svg className={small ? "h-2.5 w-2.5" : "h-4 w-4"} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 13l4 4L19 7" />
    </svg>
  );
}

function CheckDot() {
  return (
    <span className="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-live-500/15 text-live-300">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5">
        <path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

function WarnDot() {
  return (
    <span className="mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full bg-amber-400/15 text-amber-300">
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3">
        <path d="M12 8v5M12 17h.01" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

function FlagIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 21V4M5 4h11l-2 4 2 4H5" />
    </svg>
  );
}

function ExternalLinkIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M14 4h6v6M20 4 10 14M19 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      className={cn("shrink-0 transition-transform", open && "rotate-90")}
    >
      <path d="M9 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ExpandIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7" />
    </svg>
  );
}

function SparkIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
      <path d="M12 2l1.9 6.1L20 10l-6.1 1.9L12 18l-1.9-6.1L4 10l6.1-1.9L12 2zM19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z" />
    </svg>
  );
}
