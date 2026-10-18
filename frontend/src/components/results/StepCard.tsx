import { motion } from "framer-motion";
import { usePipelineStore, type StepKind } from "../../store/usePipelineStore";
import { cardReveal } from "../../lib/motion";
import { StepHeader } from "../common/StepHeader";
import { STAGES, type StageId } from "../../store/stageMap";
import { TIER_MODEL } from "../../lib/cost";
import { formatLatency } from "../../lib/duration";
import { minStrongSignals } from "../../lib/policy";

/** "142 KB" / "3.4 MB" — no existing byte formatter in frontend/src/lib. */
function formatBytes(n: number): string {
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

const STRATEGY_LABEL: Record<string, string> = {
  inline: "Inline",
  files_api: "Files API (large file)",
  multipass: "Multipass (long document)",
};
const THINKING_LABEL: Record<string, string> = {
  MINIMAL: "minimal",
  LOW: "low",
  MEDIUM: "medium",
  HIGH: "high",
};

/** Plain-English rationale for the recommended tier, keyed off the same
 * signals decide_extraction_tier used server-side — a canned qualitative
 * sentence rather than re-deriving the numeric thresholds client-side, so it
 * can't silently drift out of sync with app/config.py. */
function tierRationale(
  strategy: string,
  tier: string | undefined,
  hasNativeText: boolean | undefined,
  pages: number | undefined,
  thinkingLevel: string | undefined,
): string {
  let base: string;
  if (strategy === "multipass") {
    base = `Long document (${pages ?? "many"} pages) — split into multiple passes, most capable tier.`;
  } else if (hasNativeText === false) {
    base = "Scanned or low-quality document — routed to the most capable tier for accuracy.";
  } else if (tier === "lite") {
    base = "Small, clear document — fastest, cheapest tier.";
  } else {
    base = "Standard invoice — default tier.";
  }
  const depth = thinkingLevel ? THINKING_LABEL[thinkingLevel] : undefined;
  return depth ? `${base} Reasoning depth: ${depth}.` : base;
}

interface StepCardContent {
  stageId: StageId;
  accent: string;
  icon: string;
  title: string;
  badge: string;
  rows: [string, string][];
  /** Wall time of the step's external call (the posting write), shown as a
   * latency pill beside the badge — same look as the extraction card's tag. */
  latencyMs?: number;
  /** Optional caveat banner — currently only the routing step's
   * recommendation-not-applied notice. */
  note?: string;
  /** Per-result one-line narrative; overrides the stage's static one when the
   * right wording depends on the outcome (Spend Classification). */
  narrative?: string;
}

/** Inline result card for deterministic nodes that don't emit a chat message. */
export function StepCard({ step }: { step: StepKind }) {
  const state = usePipelineStore((s) => s.state);
  const stepNumbers = usePipelineStore((s) => s.stepNumbers);

  const content = ((): StepCardContent | null => {
    if (step === "screening_decision") {
      const route = state.screening_route;
      if (!route) return null;
      const a = state.screening_assessment;
      const strengths = [a?.attachment_signal, a?.body_signal, a?.subject_signal];
      const strong = strengths.filter((s) => s === "strong").length;
      const weak = strengths.filter((s) => s === "weak").length;
      const proceed = route === "invoice";
      const minStrong = minStrongSignals(state.policy_snapshot);
      return {
        stageId: "screening_decision" as StageId,
        accent: proceed
          ? "text-live-400 border-live-400/30 bg-live-500/10"
          : "text-slate-400 border-line-bold bg-wash",
        icon: proceed ? "→" : "⨯",
        title: proceed ? "Invoice" : "Not an Invoice",
        badge: proceed ? "INVOICE" : "NOT AN INVOICE",
        rows: [
          ["Signal count", `${strong} strong · ${weak} weak`],
          ["Policy", proceed ? `≥${minStrong} strong signal${minStrong === 1 ? "" : "s"} → proceed` : "insufficient corroboration → discard"],
          ["Next step", proceed ? "Proceed to Invoice Extraction" : "No further processing"],
        ] as [string, string][],
      };
    }
    if (step === "routing") {
      const strategy = state.extraction_strategy;
      if (!strategy) return null;
      const tier = state.extraction_recommended_model;
      const pages = state.extraction_page_count;
      const size = state.extraction_byte_size;
      const hasNativeText = state.extraction_has_native_text;
      const thinkingLevel = state.extraction_thinking_level;
      const tieringEnabled = state.extraction_dynamic_tiering_enabled;

      const docBits = [
        size != null ? formatBytes(size) : null,
        pages != null ? `${pages} page${pages === 1 ? "" : "s"}` : null,
        hasNativeText != null ? (hasNativeText ? "digital text" : "scanned/image") : null,
      ].filter(Boolean);

      return {
        stageId: "routing" as StageId,
        accent: "text-brand-300 border-brand-400/30 bg-brand-500/10",
        icon: "⮑",
        title: "Extraction Strategy",
        badge: tier ? (TIER_MODEL[tier] ?? tier) : "—",
        rows: [
          ...(docBits.length ? [["Document", docBits.join(" · ")] as [string, string]] : []),
          ["Why", tierRationale(strategy, tier, hasNativeText, pages, thinkingLevel)],
          // The common "inline" case conveys nothing to a reviewer — only
          // surface transport when something out of the ordinary happened.
          ...(strategy !== "inline"
            ? [["Transport", STRATEGY_LABEL[strategy] ?? strategy] as [string, string]]
            : []),
        ] as [string, string][],
        note:
          tieringEnabled === false
            ? "Recommendation only — dynamic tiering is off, so extraction ran on its currently configured model."
            : undefined,
      };
    }
    if (step === "classify") {
      const m = state.po_match;
      if (!m) return null;
      const direct = m.route === "direct";
      const target = m.target_system ?? "ERP";
      const matched = !!m.matched;
      // This step only looks the PO up and picks the system the invoice WOULD post
      // to. Validation, approval and posting all come later, and the invoice can
      // still be rejected or quarantined, so nothing here claims it was routed.
      return {
        stageId: "classify" as StageId,
        narrative: matched
          ? `The purchase order was looked up in the ledger. If the invoice passes validation and any approval, it will post to ${target}.`
          : `No matching purchase order was found. If the invoice passes validation and any approval, it would default to ${target}.`,
        accent: !matched
          ? "text-amber-300 border-amber-400/30 bg-amber-500/10"
          : direct
            ? "text-brand-300 border-brand-400/30 bg-brand-500/10"
            : "text-accent-400 border-accent-400/30 bg-accent-500/10",
        icon: "⮑",
        title: "Spend Classification",
        badge: matched ? `${direct ? "DIRECT" : "INDIRECT"} · ${target} (PENDING)` : `NO PO MATCH · ${target} (DEFAULT)`,
        latencyMs: m.latency_ms ?? state.classify_latency_ms,
        rows: [
          ["PO number", m.po_number ?? "—"],
          ["PO match", matched ? "Matched in the PO ledger" : `No match (defaulted ${m.route ?? "direct"})`],
          ...(m.store_path ? [["Looked up in", `Firestore · ${m.store_path}`] as [string, string]] : []),
          ...(m.category ? [["Category", m.category] as [string, string]] : []),
          ...(m.cost_center ? [["Cost center", m.cost_center] as [string, string]] : []),
          ...(m.gl_account ? [["G/L account", m.gl_account] as [string, string]] : []),
          ...(m.requisitioner ? [["Requisitioner", m.requisitioner] as [string, string]] : []),
          ...(m.vendor_name ? [["PO vendor", m.vendor_name] as [string, string]] : []),
          [matched ? "Posts to (if approved)" : "Default target (if approved)", target],
        ] as [string, string][],
      };
    }
    if (step === "posting") {
      const p = state.posting_result;
      if (!p) return null;
      return {
        stageId: "post_erp" as StageId,
        accent: "text-live-400 border-live-400/30 bg-live-500/10",
        icon: "✓",
        title: "Posted to ERP",
        badge: p.status?.toUpperCase() ?? "POSTED",
        latencyMs: p.latency_ms ?? state.posting_latency_ms,
        rows: [
          ["Document", p.document_id ?? "—"],
          ["System", "ERP"],
          ...(p.reference ? [["Reference", p.reference] as [string, string]] : []),
          ...(p.store_path ? [["Stored in", `Firestore · ${p.store_path}`] as [string, string]] : []),
          ...(p.posted_at ? [["Posted at", p.posted_at] as [string, string]] : []),
        ] as [string, string][],
      };
    }
    if (step === "escalation") {
      const e = state.escalation_result;
      if (!e) return null;
      return {
        stageId: "escalate" as StageId,
        accent: "text-rose-300 border-rose-400/30 bg-rose-400/10",
        icon: "!",
        title: "Escalation Raised",
        badge: e.system?.toUpperCase() ?? "SERVICENOW",
        rows: [
          ["Ticket", e.ticket_id ?? "—"],
          ...(e.priority ? [["Priority", e.priority] as [string, string]] : []),
          ...(e.assignment_group ? [["Assigned to", e.assignment_group] as [string, string]] : []),
          ["Reason", e.reason ?? "—"],
        ] as [string, string][],
      };
    }
    return null;
  })();

  if (!content) return null;
  const stepNum = stepNumbers[content.stageId];
  const stageDef = STAGES[content.stageId];

  return (
    <motion.div
      variants={cardReveal}
      initial="initial"
      animate="animate"
      exit="exit"
      className="card rounded-2xl p-4"
    >
      <div className="mb-3 border-b border-line pb-3">
        <StepHeader step={stepNum} title={stageDef.label} agent={!!stageDef.agent} narrative={content.narrative ?? stageDef.narrative} />
      </div>
      <div className="mb-2 flex items-center gap-2">
        <span className={`grid h-6 w-6 place-items-center rounded-lg border text-xs ${content.accent}`}>
          {content.icon}
        </span>
        <span className="text-sm font-medium text-slate-200">{content.title}</span>
        <div className="ml-auto flex items-center gap-2">
          {content.latencyMs != null && (
            <span
              className="rounded-md border border-line bg-wash px-2 py-0.5 font-mono text-[12px] font-medium text-live-300"
              title="Time taken to write the posting record"
            >
              {formatLatency(content.latencyMs)}
            </span>
          )}
          <span className={`rounded-full border px-2 py-0.5 text-[11px] font-semibold tracking-wide ${content.accent}`}>
            {content.badge}
          </span>
        </div>
      </div>
      <div className="grid grid-cols-1 gap-1.5 pl-8 text-xs sm:grid-cols-2">
        {content.rows.map(([k, v]) => (
          <div
            key={k}
            className={`flex gap-2 rounded-md px-1.5 py-0.5 transition hover:bg-wash ${
              k === "Why" ? "sm:col-span-2" : ""
            }`}
          >
            <span className="shrink-0 text-slate-500">{k}:</span>
            <span className={k === "Why" ? "text-slate-300" : "font-mono text-slate-300"}>{v}</span>
          </div>
        ))}
      </div>
      {content.note && (
        <div className="ml-8 mt-2.5 rounded-md border border-amber-400/20 bg-amber-400/[0.06] px-2.5 py-1.5 text-[11px] text-amber-200/90">
          {content.note}
        </div>
      )}
    </motion.div>
  );
}
