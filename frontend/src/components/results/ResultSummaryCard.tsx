import { motion } from "framer-motion";
import { usePipelineStore } from "../../store/usePipelineStore";
import type { ArmorQuarantine } from "../../api/types";
import { useConfetti } from "../../hooks/useConfetti";
import { cn } from "../../lib/utils";
import { cardReveal } from "../../lib/motion";
import { FILTER_LABELS } from "../../lib/armor";
import { CountUp } from "../common/CountUp";
import { WhyLine } from "../common/WhyLine";

interface Outcome {
  title: string;
  accent: string;
  detail: string;
  doc?: string;
}

export function ResultSummaryCard() {
  const state = usePipelineStore((s) => s.state);
  const status = state.pipeline_status;
  useConfetti(status === "posted_erp");

  if (!status) return null;

  const rec = state.invoice_record ?? {};
  const hasInvoice = state.invoice_record != null;
  const inv = rec.invoice_no?.value ?? "N/A";
  const vendor = rec.vendor?.value ?? "N/A";
  const currency = rec.currency?.value ?? "";
  const amountNum = rec.total?.value != null ? Number(String(rec.total.value).replace(/[^0-9.-]+/g, "")) : NaN;
  const amount = rec.total?.value ? `${currency} ${rec.total.value}`.trim() : "N/A";

  const m = state.po_match;
  const spend = m
    ? `${m.route === "direct" ? "Direct" : "Indirect"}${m.category ? ` · ${m.category}` : ""}`
    : null;

  const outcome = outcomeFor(state);
  const celebratory = status === "posted_erp";

  return (
    <motion.div
      variants={cardReveal}
      initial="initial"
      animate="animate"
      exit="exit"
      id="result-card"
      // shrink-0 is required alongside overflow-hidden: in the conversation pane's
      // fixed-height flex column, flexbox otherwise crushes this card to ~2px
      // whenever the run's cards overflow the pane (see the same guard in A2uiHost).
      className="gradient-border glass-elevated shrink-0 scroll-mt-16 overflow-hidden rounded-2xl"
    >
      <div className={cn("flex items-center gap-3 px-5 py-4", outcome.accent)}>
        <motion.span
          initial={{ scale: 0, rotate: -30 }}
          animate={{ scale: 1, rotate: 0 }}
          transition={{ type: "spring", stiffness: 420, damping: 14, delay: 0.18 }}
          className="grid h-9 w-9 place-items-center rounded-full bg-white/20 ring-1 ring-white/30"
        >
          {celebratory || status === "discarded" ? <CheckIcon /> : <FlagIcon />}
        </motion.span>
        <div>
          <p className="text-sm font-semibold text-white">{outcome.title}</p>
          <p className="text-xs text-white/80">{outcome.detail}</p>
        </div>
      </div>

      <WhyLine className="rounded-none border-x-0 border-t-0 px-5" />

      {hasInvoice && (
        <div className="grid grid-cols-2 gap-px bg-wash sm:grid-cols-3">
          <Field label="Invoice" value={inv} />
          <Field label="Vendor" value={vendor} />
          <Field
            label="Amount"
            value={amount}
            display={
              Number.isNaN(amountNum) ? undefined : (
                <CountUp value={amountNum} decimals={2} group prefix={currency ? `${currency} ` : ""} />
              )
            }
          />
          {/* Spend and Document share the last row, so neither leaves an empty cell. */}
          {spend && <Field label="Spend" value={spend} full={!outcome.doc} />}
          {outcome.doc && <Field label="Document" value={outcome.doc} mono full={!spend} wide={!!spend} />}
        </div>
      )}

      {state.armor_quarantine && <SecurityScreening q={state.armor_quarantine} />}

      {state.validation_flags && state.validation_flags.length > 0 && (
        <div className="border-t border-line bg-ink-900 px-5 py-3">
          <p className="mb-1 text-[11px] uppercase tracking-wider text-slate-400">
            Flags that triggered review
          </p>
          <ul className="space-y-0.5">
            {state.validation_flags.map((f, i) => (
              <li key={i} className="text-xs text-slate-400">
                • {f}
              </li>
            ))}
          </ul>
        </div>
      )}
    </motion.div>
  );
}

/** The Model Armor details for a stopped invoice, in the card's own styling: which
 * filter fired, where it was caught, and what happens to the email. */
function SecurityScreening({ q }: { q: ArmorQuarantine }) {
  const threat = q.kind === "threat";
  const caught =
    q.stage === "extraction"
      ? "After extraction. The model had read the document, so its values can't be trusted."
      : "At intake, before any AI agent saw the email.";
  const filters = q.filters.map((f) => (FILTER_LABELS[f] ? `${FILTER_LABELS[f]} (${f})` : f)).join(", ");
  const next = threat
    ? "Not approved or posted. The email is labelled AP-Quarantined and left in the inbox for manual review. If it is a genuine invoice, ask the vendor to resend a clean copy."
    : "Nothing was approved or posted. The email is labelled AP-Error and is retried on the next intake catch-up.";
  return (
    <div className="border-t border-line">
      <p className="bg-ink-900 px-5 pt-3 text-[11px] uppercase tracking-wider text-slate-400">
        Security screening · Model Armor
      </p>
      <div className="mt-3 grid grid-cols-2 gap-px bg-wash sm:grid-cols-3">
        <Field label="Caught" value={caught} full />
        {filters && <Field label="Filter" value={filters} full />}
        {!threat && <Field label="Detail" value={q.reason} full />}
        <Field label="What happens next" value={next} full />
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  display,
  mono,
  full,
  wide,
}: {
  label: string;
  value: string;
  display?: React.ReactNode;
  mono?: boolean;
  full?: boolean;
  /** Two of the three columns. */
  wide?: boolean;
}) {
  return (
    <div className={cn("bg-ink-900 px-4 py-3", full && "col-span-2 sm:col-span-3", wide && "col-span-2")}>
      <p className="text-[11px] uppercase tracking-wider text-slate-500">{label}</p>
      <p className={cn("mt-0.5 text-sm text-slate-100", mono && "font-mono text-xs text-brand-300")}>
        {display ?? value}
      </p>
    </div>
  );
}

function outcomeFor(state: ReturnType<typeof usePipelineStore.getState>["state"]): Outcome {
  switch (state.pipeline_status) {
    case "posted_erp":
      return {
        title: "Posted to ERP",
        detail: `${state.po_match?.route === "indirect" ? "Indirect" : "Direct"}-spend invoice posted`,
        accent: "bg-gradient-to-r from-live-500/80 to-brand-500/60",
        doc: state.posting_result?.document_id,
      };
    case "escalated":
      return {
        title: "Escalated — duplicate detected",
        detail: state.escalation_result?.reason ?? "ServiceNow ticket raised",
        accent: "bg-gradient-to-r from-rose-500/80 to-amber-500/60",
        doc: state.escalation_result?.ticket_id,
      };
    case "discarded":
      return {
        title: "Screened out — not an invoice",
        detail:
          state.screening_assessment?.assessment_rationale ??
          "The screening agent determined this email is not a vendor invoice; no action taken.",
        accent: "bg-gradient-to-r from-slate-600/80 to-slate-500/50",
      };
    case "quarantined":
      return {
        title: "Quarantined — security screening",
        detail: "Model Armor flagged this invoice. It was not approved or posted.",
        accent: "bg-gradient-to-r from-rose-600/80 to-amber-500/60",
      };
    case "error":
      return {
        title: "Processing failed — nothing was posted",
        detail: `Stopped at ${state.pipeline_error_node ?? "an earlier step"}. The email is flagged AP-Error and will be retried.`,
        accent: "bg-gradient-to-r from-rose-600/80 to-amber-500/60",
      };
    case "rejected":
      return {
        title: "Invoice rejected — vendor notified",
        detail: state.notification_result?.summary ?? "Rejection email sent",
        accent: "bg-gradient-to-r from-rose-500/80 to-accent-500/60",
      };
    default:
      return {
        title: "Pipeline complete",
        detail: "Processing finished",
        accent: "bg-gradient-to-r from-slate-600/80 to-slate-500/50",
      };
  }
}

function CheckIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="3">
      <path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
function FlagIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
      <path d="M4 21V4h13l-2 4 2 4H4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
