/** Builds and downloads the audit record for a run (JSON). Everything comes from
 * the state the UI already mirrors; nothing is re-fetched. */
import type { PipelineState } from "../api/types";
import type { StageStatus } from "../store/usePipelineStore";
import type { StageId } from "../store/stageMap";
import { deriveRunCost } from "./runCost";
import { decisionWhy } from "./why";

export const AUDIT_SCHEMA = "invoice-agent/run-export@1";

export interface AuditExport {
  schema: typeof AUDIT_SCHEMA;
  exportedAt: string;
  run: {
    status: string;
    startedAt: string | null;
    endedAt: string | null;
    elapsedMs: number | null;
    stages: Partial<Record<StageId, StageStatus>>;
  };
  invoice: PipelineState["invoice_record"] | null;
  decision: {
    why: string | null;
    pipelineStatus: string | null;
    validationRoute: string | null;
    flags: string[];
    rubric: PipelineState["validation_rubric"] | null;
    approval: PipelineState["approval_audit"] | null;
    poMatch: PipelineState["po_match"] | null;
    posting: PipelineState["posting_result"] | null;
    escalation: PipelineState["escalation_result"] | null;
    notification: PipelineState["notification_result"] | null;
  };
  cost: {
    totalUsd: number;
    inputTokens: number;
    outputTokens: number;
    steps: { step: string; model: string; inputTokens: number; outputTokens: number; costUsd: number }[];
  };
  /** The full pipeline state mirrored by the UI. */
  pipelineState: PipelineState;
}

const iso = (t: number | null) => (t == null ? null : new Date(t).toISOString());

export function buildAuditExport(input: {
  state: PipelineState;
  status: string;
  stageStatus: Partial<Record<StageId, StageStatus>>;
  runStartedAt: number | null;
  runEndedAt: number | null;
  now?: Date;
}): AuditExport {
  const { state, status, stageStatus, runStartedAt, runEndedAt } = input;
  const cost = deriveRunCost(state);
  return {
    schema: AUDIT_SCHEMA,
    exportedAt: (input.now ?? new Date()).toISOString(),
    run: {
      status,
      startedAt: iso(runStartedAt),
      endedAt: iso(runEndedAt),
      elapsedMs: runStartedAt != null && runEndedAt != null ? runEndedAt - runStartedAt : null,
      stages: stageStatus,
    },
    invoice: state.invoice_record ?? null,
    decision: {
      why: decisionWhy(state)?.text ?? null,
      pipelineStatus: state.pipeline_status ?? null,
      validationRoute: state.validation_route ?? null,
      flags: state.validation_flags ?? [],
      rubric: state.validation_rubric ?? null,
      approval: state.approval_audit ?? null,
      poMatch: state.po_match ?? null,
      posting: state.posting_result ?? null,
      escalation: state.escalation_result ?? null,
      notification: state.notification_result ?? null,
    },
    cost: {
      totalUsd: cost.totalCost,
      inputTokens: cost.inTokens,
      outputTokens: cost.outTokens,
      steps: cost.steps.map((s) => ({
        step: s.label,
        model: s.model,
        inputTokens: s.inTokens,
        outputTokens: s.outTokens,
        costUsd: s.cost,
      })),
    },
    pipelineState: state,
  };
}

/** "invoice-INV-1042-2026-10-15.json" — safe for any filesystem. */
export function exportFilename(prefix: string, state: PipelineState, ext = "json", now = new Date()): string {
  const no = state.invoice_record?.invoice_no?.value;
  const slug = (no ? String(no) : "run").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "run";
  return `${prefix}-${slug}-${now.toISOString().slice(0, 10)}.${ext}`;
}

export function downloadJson(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
