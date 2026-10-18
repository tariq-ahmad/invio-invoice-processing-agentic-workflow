import { useEffect, useState } from "react";
import { usePipelineStore, WORKING_LABEL } from "../store/usePipelineStore";
import type { StageId } from "../store/stageMap";

interface SubMessage {
  atMs: number;
  text: string;
}

// Intake has two distinct behaviors depending on how the run was triggered:
// a real AgentMail event always carries the exact message ID (a precise fetch —
// see app/nodes/intake.py's forced_msg_id path), while a generic "search
// latest invoice" only runs as a rare fallback with no target message. The
// copy must say which one is actually happening, or it misrepresents an
// event-driven, precisely-targeted fetch as a vague inbox search.
const INTAKE_EXACT: SubMessage[] = [
  { atMs: 0, text: "Fetching the triggered invoice email from AgentMail…" },
  { atMs: 3_000, text: "Downloading the attachment…" },
  { atMs: 8_000, text: "Still working — AgentMail is taking a little longer than usual to respond…" },
];
const INTAKE_SEARCH: SubMessage[] = [
  { atMs: 0, text: "Connecting to AgentMail…" },
  { atMs: 3_000, text: "Searching the inbox for the latest invoice…" },
  { atMs: 8_000, text: "Still working — the inbox search is taking a little longer than usual…" },
];

// Only intake and extraction have real, user-perceptible latency with zero
// backend sub-progress signal (each is a single blocking call). Every other
// stage is fast or already artificially paced (STAGE_DELAY) and keeps its
// existing static WORKING_LABEL entry via the fallback below.
const WORKING_SUBMESSAGES: Partial<Record<StageId, SubMessage[]>> = {
  extraction: [
    { atMs: 0, text: "Reading the invoice PDF with Gemini…" },
    { atMs: 8_000, text: "Parsing header fields — vendor, invoice number, PO reference…" },
    { atMs: 20_000, text: "Extracting line items from the invoice…" },
    { atMs: 40_000, text: "Cross-checking totals and tax against the line items…" },
    { atMs: 60_000, text: "Finalizing the structured extraction…" },
  ],
};

/** Live rotating label for a stage, ticking locally (same pattern as
 * ElapsedTime — no global store re-render churn). Returns WORKING_LABEL's
 * static single-message fallback whenever `stage` isn't the live/current
 * stage, or isn't in WORKING_SUBMESSAGES. Omit `stage` to always track
 * whichever stage is currently active. */
export function useWorkingLabel(stage?: StageId): string {
  const currentStage = usePipelineStore((s) => s.currentStage);
  const status = usePipelineStore((s) => s.status);
  const stageActivatedAt = usePipelineStore((s) => s.stageActivatedAt);
  const intakeHasExactMessage = usePipelineStore((s) => s.intakeHasExactMessage);

  const targetStage = stage ?? currentStage;
  const isLive = status === "streaming" && targetStage != null && targetStage === currentStage;
  const table =
    targetStage === "intake"
      ? intakeHasExactMessage
        ? INTAKE_EXACT
        : INTAKE_SEARCH
      : targetStage
        ? WORKING_SUBMESSAGES[targetStage]
        : undefined;

  const [, forceTick] = useState(0);
  useEffect(() => {
    if (!isLive || !table) return;
    const id = setInterval(() => forceTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [isLive, table]);

  if (!targetStage) return "Agent is working…";
  if (!isLive || !table) return WORKING_LABEL[targetStage];

  const startedAt = stageActivatedAt[targetStage] ?? Date.now();
  const elapsed = Date.now() - startedAt;
  let text = table[0].text;
  for (const step of table) {
    if (elapsed >= step.atMs) text = step.text;
    else break;
  }
  return text;
}
