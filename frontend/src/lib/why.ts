/**
 * One plain sentence naming the deciding factor of a run, derived only from what
 * the backend already put in state (rubric, flags, policy snapshot, audit) — so it
 * can never disagree with the rule that actually ran.
 */
import type { PipelineState, RubricRule } from "../api/types";

export type WhyTone = "ok" | "review" | "stop" | "info";
export interface Why {
  tone: WhyTone;
  text: string;
}

const MAX_OBSERVED = 110;

const clip = (s: string, n = MAX_OBSERVED) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s);

/** "VERIFIED_AGAINST_SOURCE" -> "verified against source". */
export const humanizeCode = (code: string) => code.toLowerCase().replace(/_/g, " ");

function flaggedRules(state: PipelineState): RubricRule[] {
  const rubric = state.validation_rubric;
  if (rubric && rubric.length > 0) {
    return rubric.filter((r) => r.status === "flag").sort((a, b) => (a.rank ?? 99) - (b.rank ?? 99));
  }
  // Pre-rubric sessions only carry flag strings.
  return (state.validation_flags ?? []).map((f, i) => ({
    id: `flag_${i}`,
    label: "Policy finding",
    headline: f,
    criterion: "",
    observed: "",
    status: "flag" as const,
    rank: i + 1,
  }));
}

function primaryReason(state: PipelineState, primary: RubricRule): string {
  const threshold = state.policy_snapshot?.hitl_threshold_usd;
  if (primary.id === "within_limit" && !primary.headline && threshold != null) {
    return `total exceeds the $${threshold.toLocaleString()} auto-approval limit`;
  }
  const head = primary.headline ?? primary.label;
  const observed = primary.observed?.trim();
  return observed ? `${head} (${clip(observed)})` : head;
}

function moreSuffix(n: number): string {
  return n > 0 ? ` · +${n} more finding${n > 1 ? "s" : ""}` : "";
}

export function decisionWhy(state: PipelineState): Why | null {
  const audit = state.approval_audit;
  const flagged = flaggedRules(state);
  const primary = flagged[0];
  switch (state.pipeline_status) {
    case "posted_erp": {
      if (audit?.decision === "approve") {
        return {
          tone: "ok",
          text: `Approved by a reviewer (${humanizeCode(audit.reason_code)}) and posted to ERP${audit.override ? " — logged as an override" : ""}`,
        };
      }
      const checks = state.validation_rubric?.length;
      return {
        tone: "ok",
        text: `Posted automatically to ERP: ${checks ? `passed all ${checks} checks` : "passed every policy check"}`,
      };
    }
    case "rejected":
      return {
        tone: "stop",
        text: audit
          ? `Rejected by a reviewer (${humanizeCode(audit.reason_code)}); the vendor was notified`
          : "Rejected by a reviewer; the vendor was notified",
      };
    case "escalated":
      return {
        tone: "review",
        text: `Escalated: ${primary ? primaryReason(state, primary) : "possible duplicate purchase order"}`,
      };
    case "discarded":
      return {
        tone: "info",
        text: `Screened out: ${clip(state.screening_assessment?.assessment_rationale?.trim() || "not enough invoice signals")}`,
      };
    case "quarantined": {
      const q = state.armor_quarantine;
      return {
        tone: "stop",
        text: q
          ? `Stopped by Model Armor ${q.stage === "extraction" ? "after extraction" : "at intake"}: ${clip(q.reason)}`
          : "Stopped by Model Armor security screening",
      };
    }
    case "error":
      return {
        tone: "stop",
        text: `Failed at ${state.pipeline_error_node ?? "an earlier step"}; nothing was posted`,
      };
  }

  // Still running (or waiting on the human): read the validation route.
  switch (state.validation_route) {
    case "human_review":
      return {
        tone: "review",
        text: primary
          ? `Routed to review: ${primaryReason(state, primary)}${moreSuffix(flagged.length - 1)}`
          : "Routed to review by the AP policy",
      };
    case "duplicate_po":
      return {
        tone: "review",
        text: `Possible duplicate: ${primary ? primaryReason(state, primary) : "this PO was already invoiced"}`,
      };
    case "proceed":
      return { tone: "ok", text: "Passed every policy check — posting automatically" };
  }
  return null;
}
