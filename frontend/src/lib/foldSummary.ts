/** One-line summary shown when a finished step is folded: what it concluded,
 * how long it took, and which model (if any) did the work. */
import type { PipelineState } from "../api/types";
import { STAGES, type StageId } from "../store/stageMap";
import { armorRailSummary, armorVerdict } from "./armor";
import { humanizeCode } from "./why";

export interface FoldSummary {
  text: string;
  ms?: number;
  model?: string;
}

const TIER: Record<string, string> = { lite: "Lite", flash: "Flash", pro: "Pro" };
const val = (f?: { value?: string | null }) => (f?.value && String(f.value).trim() ? String(f.value).trim() : "");

export function foldSummary(stage: StageId, state: PipelineState): FoldSummary {
  const def = STAGES[stage];
  const ms = def.latencyKey ? (state[def.latencyKey] as number | undefined) : undefined;
  const model = def.agent ? ((def.modelKey ? state[def.modelKey] : undefined) ?? def.model) : undefined;
  const base = { ms, model };

  const rec = state.invoice_record;
  const flags = state.validation_flags?.length ?? 0;

  let text: string;
  switch (stage) {
    case "armor_intake":
      text = armorRailSummary(armorVerdict("armor_intake", state)) || "Scanned";
      break;
    case "screening":
      text = state.screening_route
        ? state.screening_route === "invoice"
          ? "Invoice signals found"
          : "No invoice signals"
        : def.hint;
      break;
    case "screening_decision":
      text = state.screening_route === "not_invoice" ? "Not an invoice — discarded" : "Invoice — proceed";
      break;
    case "routing": {
      const tier = state.extraction_recommended_model;
      text = tier ? `${TIER[tier] ?? tier} tier · ${state.extraction_strategy ?? "inline"}` : def.hint;
      break;
    }
    case "extraction": {
      const parts = [val(rec?.vendor), val(rec?.invoice_no)].filter(Boolean);
      const total = val(rec?.total);
      if (total) parts.push(`${val(rec?.currency)} ${total}`.trim());
      text = parts.join(" · ") || "Invoice data extracted";
      break;
    }
    case "classify": {
      const m = state.po_match;
      text = m
        ? m.matched
          ? `PO ${m.po_number ?? "matched"} → ${m.target_system ?? "ERP"}`
          : "No PO match"
        : def.hint;
      break;
    }
    case "validation":
      text = flags > 0 ? `${flags} finding${flags > 1 ? "s" : ""} → review` : "All checks passed";
      break;
    case "memory":
      text = "Prior decisions recalled";
      break;
    case "approval":
      text = state.approval_audit
        ? `${state.approval_audit.decision === "approve" ? "Approved" : "Rejected"} · ${humanizeCode(state.approval_audit.reason_code)}`
        : "Decision recorded";
      break;
    case "post_erp":
      text = state.posting_result?.document_id ? `Document ${state.posting_result.document_id}` : "Posted";
      break;
    case "escalate":
      text = state.escalation_result?.ticket_id ? `Ticket ${state.escalation_result.ticket_id}` : "Ticket raised";
      break;
    case "draft_email":
      text = "Rejection email drafted";
      break;
    case "review_email":
      text = "Email reviewed";
      break;
    case "notify":
      text = "Vendor notified";
      break;
    default:
      text = def.hint;
  }
  return { text, ...base };
}
