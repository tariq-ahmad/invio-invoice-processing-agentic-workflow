/** Maps the conversation feed's items to pipeline stages, so the graph and the
 * feed can navigate to each other and finished steps can fold. */
import type { ConversationItem, StepKind } from "../store/usePipelineStore";
import type { PipelineState } from "../api/types";
import type { StageId } from "../store/stageMap";

export function stageForSurface(surfaceId: string): StageId {
  if (surfaceId.startsWith("invoice_screening")) return "screening";
  if (surfaceId.startsWith("agent_memory")) return "memory";
  if (surfaceId.startsWith("approval_gate")) return "approval";
  if (surfaceId.startsWith("email_review")) return "review_email";
  return "extraction";
}

export function stageForStep(step: StepKind, _state: PipelineState): StageId {
  switch (step) {
    case "posting":
      return "post_erp";
    case "escalation":
      return "escalate";
    default:
      return step; // classify | screening_decision | routing | armor_intake share their stage id
  }
}

export function stageOfItem(item: ConversationItem, state: PipelineState): StageId | null {
  switch (item.kind) {
    case "text":
      return item.stageId ?? null;
    case "surface":
      return stageForSurface(item.surfaceId);
    case "step":
      return stageForStep(item.step, state);
  }
}

/** The (first) feed item that shows a stage's output, if any. */
export function findItemForStage(
  items: ConversationItem[],
  stage: StageId,
  state: PipelineState,
): ConversationItem | undefined {
  return items.find((it) => stageOfItem(it, state) === stage);
}

export function stagesWithCards(items: ConversationItem[], state: PipelineState): Set<StageId> {
  const out = new Set<StageId>();
  for (const it of items) {
    const st = stageOfItem(it, state);
    if (st) out.add(st);
  }
  return out;
}
