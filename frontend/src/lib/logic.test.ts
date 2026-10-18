import { describe, expect, it } from "vitest";
import type { PipelineState } from "../api/types";
import { decisionWhy, humanizeCode } from "./why";
import { findItemForStage, stageForStep, stageForSurface, stageOfItem, stagesWithCards } from "./stageCards";
import { foldSummary } from "./foldSummary";
import { runStatusChip } from "./runStatus";
import { outcomeBreakdown, summarizeStats } from "./statsSummary";
import { buildAuditExport, exportFilename } from "./auditExport";
import { SPEEDS, speedLabel, speedToScale } from "./pacing";
import type { ConversationItem } from "../store/usePipelineStore";

const S = (s: Partial<PipelineState>) => s as PipelineState;

describe("decisionWhy", () => {
  it("returns null before any decision exists", () => {
    expect(decisionWhy(S({}))).toBeNull();
  });

  it("explains a review route with the primary finding and a count of the rest", () => {
    const why = decisionWhy(
      S({
        validation_route: "human_review",
        validation_rubric: [
          { id: "within_limit", label: "Limit", headline: "Over the auto-approval limit", criterion: "", observed: "USD 12,400", status: "flag", rank: 1 },
          { id: "po_status", label: "PO status", headline: "PO is closed", criterion: "", observed: "", status: "flag", rank: 2 },
          { id: "valid_total", label: "Total", criterion: "", observed: "", status: "pass" },
        ],
      }),
    );
    expect(why?.tone).toBe("review");
    expect(why?.text).toBe("Routed to review: Over the auto-approval limit (USD 12,400) · +1 more finding");
  });

  it("falls back to the policy threshold for the routine limit rule", () => {
    const why = decisionWhy(
      S({
        validation_route: "human_review",
        policy_snapshot: { hitl_threshold_usd: 4000 },
        validation_rubric: [{ id: "within_limit", label: "Limit", criterion: "", observed: "", status: "flag", rank: 1 }],
      }),
    );
    expect(why?.text).toBe("Routed to review: total exceeds the $4,000 auto-approval limit");
  });

  it("degrades to raw flag strings when there is no rubric", () => {
    const why = decisionWhy(S({ validation_route: "human_review", validation_flags: ["Missing PO reference"] }));
    expect(why?.text).toBe("Routed to review: Missing PO reference");
  });

  it("reports an automatic post and counts the checks", () => {
    const why = decisionWhy(
      S({
        pipeline_status: "posted_erp",
        validation_rubric: [
          { id: "a", label: "A", criterion: "", observed: "", status: "pass" },
          { id: "b", label: "B", criterion: "", observed: "", status: "pass" },
        ],
      }),
    );
    expect(why).toEqual({ tone: "ok", text: "Posted automatically to ERP: passed all 2 checks" });
  });

  it("attributes an approved post to the reviewer and flags overrides", () => {
    const why = decisionWhy(
      S({
        pipeline_status: "posted_erp",
        approval_audit: {
          decision: "approve",
          reason_code: "VERIFIED_AGAINST_SOURCE",
          note: "",
          flagged_rules: [],
          validation_flags: [],
          override: true,
          attested_source_review: true,
          source_document_opened: true,
          decided_at: "2026-10-15T00:00:00Z",
        },
      }),
    );
    expect(why?.text).toBe("Approved by a reviewer (verified against source) and posted to ERP — logged as an override");
  });

  it("covers the terminal outcomes", () => {
    expect(decisionWhy(S({ pipeline_status: "rejected" }))?.tone).toBe("stop");
    expect(decisionWhy(S({ pipeline_status: "error", pipeline_error_node: "extraction_agent" }))?.text).toBe(
      "Failed at extraction_agent; nothing was posted",
    );
    expect(
      decisionWhy(S({ pipeline_status: "quarantined", armor_quarantine: { reason: "prompt injection", kind: "threat", stage: "intake", filters: [] } }))
        ?.text,
    ).toBe("Stopped by Model Armor at intake: prompt injection");
    expect(decisionWhy(S({ pipeline_status: "discarded" }))?.text).toBe("Screened out: not enough invoice signals");
  });

  it("humanizes reason codes", () => {
    expect(humanizeCode("WITHIN_DELEGATED_AUTHORITY")).toBe("within delegated authority");
  });
});

describe("stage ↔ card mapping", () => {
  const items: ConversationItem[] = [
    { kind: "step", id: "1", step: "armor_intake" },
    { kind: "surface", id: "2", surfaceId: "invoice_extraction" },
    { kind: "text", id: "3", text: "ok", stageId: "validation" },
    { kind: "step", id: "4", step: "posting" },
  ];

  it("maps surfaces by id prefix", () => {
    expect(stageForSurface("invoice_screening")).toBe("screening");
    expect(stageForSurface("approval_gate")).toBe("approval");
    expect(stageForSurface("email_review-123")).toBe("review_email");
    expect(stageForSurface("agent_memory")).toBe("memory");
    expect(stageForSurface("invoice_extraction")).toBe("extraction");
  });

  it("resolves the posting step to the ERP stage", () => {
    expect(stageForStep("posting", S({ posting_result: { system: "ERP" } }))).toBe("post_erp");
    expect(stageForStep("escalation", S({}))).toBe("escalate");
  });

  it("finds the card for a stage and lists the stages that have one", () => {
    const state = S({ posting_result: { system: "ERP" } });
    expect(findItemForStage(items, "extraction", state)?.id).toBe("2");
    expect(findItemForStage(items, "intake", state)).toBeUndefined();
    expect(stageOfItem(items[2], state)).toBe("validation");
    expect([...stagesWithCards(items, state)].sort()).toEqual(["armor_intake", "extraction", "post_erp", "validation"]);
  });
});

describe("foldSummary", () => {
  it("summarises extraction from the record and carries latency + model", () => {
    const s = foldSummary(
      "extraction",
      S({
        invoice_record: { vendor: { value: "Acme" }, invoice_no: { value: "INV-9" }, total: { value: "120.00" }, currency: { value: "USD" } },
        extraction_latency_ms: 1800,
        extraction_model: "gemini-3.8-flash",
      }),
    );
    expect(s).toEqual({ text: "Acme · INV-9 · USD 120.00", ms: 1800, model: "gemini-3.8-flash" });
  });

  it("describes validation and approval outcomes", () => {
    expect(foldSummary("validation", S({ validation_flags: ["a", "b"] })).text).toBe("2 findings → review");
    expect(foldSummary("validation", S({})).text).toBe("All checks passed");
    expect(
      foldSummary(
        "approval",
        S({ approval_audit: { decision: "reject", reason_code: "AMOUNT_INCORRECT", note: "", flagged_rules: [], validation_flags: [], override: false, attested_source_review: false, source_document_opened: false, decided_at: "" } }),
      ).text,
    ).toBe("Rejected · amount incorrect");
  });
});

describe("runStatusChip", () => {
  const base = { state: S({}), currentStage: null, interrupt: null };
  it("prioritises waiting-on-you, then final outcome, then progress", () => {
    expect(runStatusChip({ ...base, status: "awaiting_input" })?.tone).toBe("amber");
    expect(runStatusChip({ ...base, status: "complete", state: S({ pipeline_status: "posted_erp" }) })?.label).toBe("Posted to ERP");
    expect(runStatusChip({ ...base, status: "streaming", currentStage: "extraction" })?.label).toBe("Processing · Extraction");
    expect(runStatusChip({ ...base, status: "idle" })).toBeNull();
  });
});

describe("summarizeStats", () => {
  it("averages only over runs that carried the data", () => {
    const s = summarizeStats({
      invoices: 4,
      amount_total_usd: 1000,
      touchless_count: 3,
      exception_count: 1,
      decision_ms_total: 6000,
      decision_ms_count: 2,
      cost_total_usd: 0.3,
      cost_count: 2, // two runs had no cost recorded
    });
    expect(s.autoRate).toBe(0.75);
    expect(s.avgDecisionMs).toBe(3000);
    expect(s.avgCostUsd).toBeCloseTo(0.15);
  });

  it("reports null (not NaN) when nothing has been processed", () => {
    const s = summarizeStats({ invoices: 0, amount_total_usd: 0, touchless_count: 0, exception_count: 0, decision_ms_total: 0, decision_ms_count: 0, cost_total_usd: 0, cost_count: 0 });
    expect(s.autoRate).toBeNull();
    expect(s.avgDecisionMs).toBeNull();
    expect(s.avgCostUsd).toBeNull();
  });
});

describe("outcomeBreakdown", () => {
  it("counts duplicates (escalated) and security quarantines, and rolls both into rejected", () => {
    const o = outcomeBreakdown({ posted_erp: 9, posted_sap: 1, rejected: 2, escalated: 3, quarantined: 4 }, 6);
    expect(o.postedAfterReview).toBe(4); // 10 posted - 6 auto
    expect(o.vendorDeclined).toBe(2);
    expect(o.duplicates).toBe(3);
    expect(o.rejectedTotal).toBe(9); // 2 declined + 3 duplicate + 4 security
    expect(o.securityFlagged).toBe(4);
  });

  it("is all zeros for an empty status map", () => {
    expect(outcomeBreakdown({}, 0)).toEqual({
      autoPosted: 0, postedAfterReview: 0, vendorDeclined: 0, duplicates: 0, securityFlagged: 0, rejectedTotal: 0,
    });
  });
});

describe("audit export", () => {
  it("builds a self-describing record", () => {
    const state = S({ pipeline_status: "posted_erp", invoice_record: { invoice_no: { value: "INV 42/A" } } });
    const out = buildAuditExport({
      state,
      status: "complete",
      stageStatus: { intake: "done" },
      runStartedAt: 1_000,
      runEndedAt: 4_000,
      now: new Date("2026-10-15T10:00:00Z"),
    });
    expect(out.schema).toBe("invoice-agent/run-export@1");
    expect(out.run.elapsedMs).toBe(3000);
    expect(out.decision.pipelineStatus).toBe("posted_erp");
    expect(out.pipelineState).toBe(state);
  });

  it("makes filesystem-safe filenames", () => {
    const state = S({ invoice_record: { invoice_no: { value: "INV 42/A" } } });
    expect(exportFilename("invoice", state, "json", new Date("2026-10-15T10:00:00Z"))).toBe("invoice-INV-42-A-2026-10-15.json");
    expect(exportFilename("invoice", S({}), "json", new Date("2026-10-15T10:00:00Z"))).toBe("invoice-run-2026-10-15.json");
  });
});

describe("pacing", () => {
  it("maps playback speeds to sleep scales (0 = max = no waiting)", () => {
    expect(SPEEDS.map(speedToScale)).toEqual([2, 1, 0.5, 0.25, 0]);
    expect(SPEEDS.map(speedLabel)).toEqual(["0.5×", "1×", "2×", "4×", "Max"]);
  });
});
