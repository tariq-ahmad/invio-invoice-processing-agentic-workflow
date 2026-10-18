import type { InterruptInfo, PipelineState } from "../api/types";
import type { RunStatus } from "../store/usePipelineStore";
import { STAGES, type StageId } from "../store/stageMap";

export type ChipTone = "neutral" | "brand" | "amber" | "green" | "rose";
export interface StatusChip {
  label: string;
  tone: ChipTone;
}

const FINAL: Record<string, StatusChip> = {
  posted_erp: { label: "Posted to ERP", tone: "green" },
  escalated: { label: "Escalated", tone: "amber" },
  rejected: { label: "Rejected", tone: "rose" },
  discarded: { label: "Not an invoice", tone: "neutral" },
  quarantined: { label: "Quarantined", tone: "rose" },
  error: { label: "Failed", tone: "rose" },
};

/** The invoice's headline status for the sticky header (null = nothing to show). */
export function runStatusChip(args: {
  status: RunStatus;
  state: PipelineState;
  currentStage: StageId | null;
  interrupt: InterruptInfo | null;
}): StatusChip | null {
  const { status, state, currentStage, interrupt } = args;
  if (status === "awaiting_input") {
    return {
      label: interrupt?.kind === "email_review" ? "Review the draft email" : "Needs your approval",
      tone: "amber",
    };
  }
  if (status === "error") return { label: "Failed", tone: "rose" };
  if (state.pipeline_status && FINAL[state.pipeline_status]) return FINAL[state.pipeline_status];
  if (status === "streaming") {
    return { label: currentStage ? `Processing · ${STAGES[currentStage].label}` : "Processing", tone: "brand" };
  }
  return null;
}
