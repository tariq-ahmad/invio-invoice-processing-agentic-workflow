/**
 * Maps ADK node/agent authors (event.author) to the visual pipeline stages
 * shown in the timeline. The canonical ordered set is rendered as a stepper;
 * branch stages are revealed based on the route the agent actually takes.
 */

export type StageId =
  | "intake"
  | "armor_intake"
  | "screening"
  | "screening_decision"
  | "routing"
  | "extraction"
  | "validation"
  | "memory"
  | "approval"
  | "classify"
  | "post_erp"
  | "escalate"
  | "draft_email"
  | "review_email"
  | "notify"
  | "complete";

export interface StageDef {
  id: StageId;
  label: string;
  hint: string;
  /** One human-tone sentence shown under the step number/badge — the single
   * source of truth for step copy (see the tone guideline in the step-header
   * unification plan: third person, one sentence, no internal jargon). */
  narrative: string;
  accent: "brand" | "amber" | "emerald" | "rose" | "violet" | "sky";
  branch?: boolean; // shown only when the path reaches it
  agent?: boolean; // backed by an LLM agent (vs a deterministic function node)
  armor?: boolean; // a Model Armor security scan (only present when Model Armor is enabled)
  model?: string; // model name shown in the UI for agent-backed stages
  /** State keys carrying the stage's latency: the model callbacks in app/agent.py
   * for agent stages, or the posting write (app/posting_store.py) for ERP posting. */
  latencyKey?:
    | "screening_latency_ms"
    | "extraction_latency_ms"
    | "draft_email_latency_ms"
    | "classify_latency_ms"
    | "posting_latency_ms";
  modelKey?: "screening_model" | "extraction_model" | "draft_email_model";
}

export const STAGES: Record<StageId, StageDef> = {
  intake: { id: "intake", label: "Intake", hint: "Fetch latest email from AgentMail", narrative: "An invoice email arrived in the accounts payable inbox and is being processed.", accent: "sky" },
  armor_intake: { id: "armor_intake", label: "Security Scan", hint: "Model Armor checks the email + attachment", narrative: "Model Armor scanned the email and its attachment for prompt injection, malicious links and malware before any AI agent could read them.", accent: "sky", armor: true },
  screening: { id: "screening", label: "Screening", hint: "Check email for invoice signals", narrative: "The email and its attachment were reviewed for invoice signals.", accent: "brand", agent: true, model: "gemini-3.5-flash-lite", latencyKey: "screening_latency_ms", modelKey: "screening_model" },
  screening_decision: { id: "screening_decision", label: "Screening Decision", hint: "Determine invoice vs. not an invoice", narrative: "A deterministic policy weighed the screening evidence to determine whether the email is an invoice.", accent: "brand" },
  routing: { id: "routing", label: "Extraction Routing", hint: "Choose PDF transport + model tier", narrative: "A deterministic policy read the document's size and complexity to choose how to send the PDF and which Gemini tier should read it.", accent: "brand" },
  extraction: { id: "extraction", label: "Extraction", hint: "Read the PDF with Gemini", narrative: "The invoice PDF was read and structured into field-level data.", accent: "brand", agent: true, model: "gemini-3.8-flash", latencyKey: "extraction_latency_ms", modelKey: "extraction_model" },
  validation: { id: "validation", label: "Validation", hint: "Apply AP business rules", narrative: "The invoice was checked against the accounts payable policy book.", accent: "brand" },
  memory: { id: "memory", label: "Agent Memory", hint: "Recall prior AP decisions for this vendor", narrative: "Prior approve/reject decisions for this vendor and flag were recalled.", accent: "violet", branch: true },
  approval: { id: "approval", label: "Human Approval", hint: "Awaiting your decision", narrative: "This invoice requires your review before it can be posted.", accent: "amber", branch: true },
  classify: { id: "classify", label: "Spend Classification", hint: "Match PO · direct vs indirect", narrative: "The purchase order was looked up and the target system chosen, pending validation and approval.", accent: "brand", latencyKey: "classify_latency_ms" },
  post_erp: { id: "post_erp", label: "Post Invoice to ERP", hint: "ERP posting (direct & indirect spend)", narrative: "The invoice was posted to the ERP system.", accent: "sky", branch: true, latencyKey: "posting_latency_ms" },
  escalate: { id: "escalate", label: "Escalation", hint: "Raise ServiceNow ticket", narrative: "A ticket was raised for the accounts payable team to investigate.", accent: "rose", branch: true },
  draft_email: { id: "draft_email", label: "Draft Rejection", hint: "Compose vendor email", narrative: "A rejection email was drafted for the vendor.", accent: "brand", branch: true, agent: true, model: "gemini-3.5-flash-lite", latencyKey: "draft_email_latency_ms", modelKey: "draft_email_model" },
  review_email: { id: "review_email", label: "Review Email", hint: "Awaiting your review", narrative: "The draft rejection email is awaiting your review.", accent: "amber", branch: true },
  notify: { id: "notify", label: "Notify Vendor", hint: "Send rejection email", narrative: "The rejection email was sent to the vendor.", accent: "rose", branch: true },
  complete: { id: "complete", label: "Complete", hint: "Pipeline finished", narrative: "Processing is complete.", accent: "emerald" },
};

/** The Model Armor scan step: one scan, right after intake and before any AI reads the email. */
export type ArmorStageId = "armor_intake";

export const ARMOR_STAGES: ArmorStageId[] = ["armor_intake"];

export function isArmorStage(id: StageId | null | undefined): id is ArmorStageId {
  return id != null && STAGES[id].armor === true;
}

/** Node author → stage id. Authors not listed don't advance the stepper. */
const AUTHOR_TO_STAGE: Record<string, StageId> = {
  fetch_invoice_email: "intake",
  check_inbox: "intake",
  inbox_empty: "intake",
  quarantine_invoice: "complete",
  screening_agent: "screening",
  screen_email_signal: "screening_decision",
  discard_non_invoice: "complete",
  route_extraction_strategy: "routing",
  extraction_agent: "extraction",
  emit_extraction_card: "extraction",
  validate_invoice: "validation",
  recall_precedents: "memory",
  human_approval_gate: "approval",
  classify_spend: "classify",
  post_to_erp: "post_erp",
  raise_duplicate_alert: "escalate",
  prepare_rejection_brief: "draft_email",
  draft_rejection_email: "draft_email",
  review_email_draft: "review_email",
  send_rejection_email: "notify",
  pipeline_complete: "complete",
};

export function stageForAuthor(author?: string): StageId | null {
  if (!author) return null;
  return AUTHOR_TO_STAGE[author] ?? null;
}
