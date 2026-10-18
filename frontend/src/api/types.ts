// ---------------------------------------------------------------------------
// ADK FastAPI wire types.
//
// IMPORTANT casing note (verified against google-adk 2.2.0):
//   * REQUEST bodies are snake_case (RunAgentRequest).
//   * STREAMED events are camelCase (Event.model_dump_json(by_alias=True)).
// ---------------------------------------------------------------------------

export interface AdkBlob {
  data: string; // base64
  mimeType: string;
}

export interface AdkFunctionCall {
  id?: string;
  name?: string;
  args?: Record<string, unknown>;
}

export interface AdkFunctionResponse {
  id?: string;
  name?: string;
  response?: Record<string, unknown>;
}

export interface AdkPart {
  text?: string;
  inlineData?: AdkBlob;
  functionCall?: AdkFunctionCall;
  functionResponse?: AdkFunctionResponse;
}

export interface AdkContent {
  role?: string;
  parts?: AdkPart[];
}

export interface AdkEventActions {
  stateDelta?: Record<string, unknown>;
  route?: string;
}

export interface AdkNodeInfo {
  path?: string; // e.g. "invoice_processing_workflow@1/fetch_invoice_email@1"
}

export interface AdkEvent {
  content?: AdkContent;
  author?: string;
  nodeInfo?: AdkNodeInfo;
  invocationId?: string;
  id?: string;
  timestamp?: number;
  longRunningToolIds?: string[];
  actions?: AdkEventActions;
  errorCode?: string;
  errorMessage?: string;
  error?: string; // mid-stream error frames: { "error": "..." }
}

export interface AdkSession {
  id: string;
  appName?: string;
  userId?: string;
}

// Request bodies (snake_case on the wire).
export interface RunAgentRequest {
  app_name: string;
  user_id: string;
  session_id: string;
  new_message: AdkContent;
  streaming?: boolean;
  invocation_id?: string;
}

// ---------------------------------------------------------------------------
// Domain types mirrored from app/schemas.py
// ---------------------------------------------------------------------------

export type SignalStrength = "strong" | "weak" | "absent";

export interface InvoiceScreeningAssessment {
  attachment_signal?: SignalStrength;
  body_signal?: SignalStrength;
  subject_signal?: SignalStrength;
  observed_signals?: string[];
  assessment_rationale?: string;
}

// How an extracted field value was obtained: read directly off the document,
// interpreted from an ambiguous/degraded source, or absent. Observable
// evidence, not a self-graded confidence score — routing is decided
// deterministically on the backend (app/nodes/validation.py).
export type SourceGrounding = "grounded" | "inferred" | "not_found";

export interface FieldValue {
  value?: string | null;
  source_grounding?: SourceGrounding;
}

/** One additional labeled data point a line item prints beyond name/amount
 * (e.g. circuit ID, site, billing period, rate) — label vocabulary is NOT
 * fixed, it varies per invoice (see app/schemas.py's LineItemAttribute). */
export interface LineItemAttribute {
  label?: string;
  value?: string;
}

export interface LineItem {
  name?: string;
  amount?: string;
  attributes?: LineItemAttribute[];
  source_grounding?: SourceGrounding;
}

export interface InvoiceRecord {
  vendor?: FieldValue;
  invoice_no?: FieldValue;
  po_ref?: FieldValue;
  line_items?: LineItem[];
  /** False when line-item extraction was truncated before every row could be
   * captured (a very large invoice exceeding the extraction call budget) —
   * line_items is then partial and the total can't be reconciled reliably. */
  line_items_complete?: boolean;
  total?: FieldValue;
  currency?: FieldValue;
}

/** Why Model Armor stopped an invoice — state["armor_quarantine"], set at intake
 * (fetch_invoice_email) or after extraction (validate_invoice). */
export interface ArmorQuarantine {
  reason: string;
  /** "threat": a filter matched (AP-Quarantined). "scan_error": no verdict (AP-Error, retried). */
  kind: "threat" | "scan_error";
  stage: "intake" | "extraction";
  filters: string[];
}

/** Why one filter fired, and where — app/model_armor.py _filter_details. Names, categories
 * and levels only: never the flagged text or the sensitive values. */
export interface ArmorFinding {
  filter: string;
  /** Which scanned item it was found in, e.g. "Email body" or "PDF attachment". */
  source?: string;
  /** Prompt injection / jailbreak: how sure Model Armor is. */
  confidence?: "low" | "medium" | "high";
  /** Malicious URL: the matched links (shown as plain text, never clickable). */
  uris?: string[];
  /** Sensitive data: info-type names (e.g. BANK_ACCOUNT_NUMBER), not values. */
  info_types?: { name: string; likelihood?: string | null }[];
  /** Harmful content categories, e.g. "hate_speech". */
  categories?: string[];
  /** Malware names reported by the attachment scan. */
  threats?: string[];
  /** Matched but exempt by policy (sensitive data on inbound content). */
  ignored?: boolean;
}

/** How one scanned input (email header, body, attachment) fared — app/model_armor.py
 * ArmorVerdict.inputs. */
export interface ArmorInput {
  label: string;
  kind: "text" | "attachment";
  /** clean / flagged: scanned · not_scanned: skipped (see note) · error: the scan failed. */
  status: "clean" | "flagged" | "not_scanned" | "error";
  /** Attachments: type, size and file name, e.g. "PDF · 142 KB · invoice.pdf". */
  detail?: string;
  /** Why it was not scanned, or the error. */
  note?: string;
  /** Every filter that matched this input (including ones exempt by policy). */
  matched: string[];
}

/** One Model Armor scan — app/model_armor.py ArmorVerdict.to_state(). */
export interface ArmorVerdict {
  scope: string;
  direction: "inbound" | "outbound";
  scanned: boolean;
  matched: boolean;
  /** Whether the verdict stopped the content (false in inspect-only mode). */
  blocked: boolean;
  filters: string[];
  ignored_filters: string[];
  /** Every filter that actually ran (matched or clean) — what the UI lists as checked. */
  checked?: string[];
  /** Why each matched filter fired and where it was found. */
  findings?: ArmorFinding[];
  /** One result per scanned input (header, body, attachment). */
  inputs?: ArmorInput[];
  /** Info-type names the template's de-identify step masked (never the values). */
  masked_info_types?: string[];
  error: string | null;
  notes: string[];
  latency_ms: number | null;
  enforce: boolean;
  template: string;
}

/** Policy values in force for a run — app/policy.py Policy.audit(), sourced from
 * Firestore (policies/ap_rules). Stamped at intake and re-stamped by validation. */
export interface PolicySnapshot {
  version?: string;
  hitl_threshold_usd?: number;
  reconcile_tolerance_pct?: number;
  required_fields?: string[];
  screening_min_strong_signals?: number;
  min_invoice_no_length?: number;
  unmatched_po_route?: string;
  po_vendor_min_similarity?: number;
  /** null = the invoice-vs-PO amount check is disabled. */
  po_amount_tolerance_pct?: number | null;
  blocked_po_statuses?: string[];
}

export interface POMatchResult {
  route?: string;
  po_number?: string | null;
  po_type?: string | null;
  matched?: boolean;
  vendor_name?: string | null;
  po_amount?: number | null;
  currency?: string | null;
  category?: string | null;
  cost_center?: string | null;
  gl_account?: string | null;
  requisitioner?: string | null;
  po_status?: string | null;
  target_system?: string | null;
  /** Firestore document the PO was read from (purchase_orders/<PO number>). */
  store_path?: string | null;
  /** Wall time of the PO lookup, in ms. */
  latency_ms?: number | null;
}

export interface PostingResult {
  system?: string;
  document_id?: string;
  status?: string;
  message?: string;
  posted_at?: string | null;
  reference?: string | null;
  /** Firestore document the posting was written to (posted_invoices/<doc id>). */
  store_path?: string | null;
  /** Wall time of the posting write, in ms. */
  latency_ms?: number | null;
}

export interface EscalationResult {
  ticket_id?: string;
  system?: string;
  status?: string;
  reason?: string;
  invoice_no?: string | null;
  priority?: string;
  assignment_group?: string;
  created_at?: string | null;
}

export interface NotificationResult {
  email_sent?: boolean;
  recipient?: string;
  subject?: string;
  summary?: string;
}

export interface AgentCostEntry {
  label: string;
  input_tokens: number;
  output_tokens: number;
  /** Billed at the same rate as output_tokens; reported separately by the
   * Gemini API (thoughts_token_count) since Gemini 3.x always thinks. */
  thinking_tokens?: number;
  costs: Record<string, number>;
  estimated?: boolean;
}

export interface CostSummary {
  agents: Record<string, AgentCostEntry>;
  totals: {
    input_tokens: number;
    output_tokens: number;
    thinking_tokens?: number;
    costs: Record<string, number>;
  };
  current_model: string;
}

/** Advisory approve/reject precedent for a vendor + flag kind (app/memory.py). */
export interface PrecedentSummary {
  vendor: string;
  flag_kinds: string[];
  total_count: number;
  approve_count: number;
  reject_count: number;
  recommendation?: "approve" | "reject" | null;
  approved_amounts: string[]; // e.g. ["USD 4750.00"]
  rejected_amounts: string[];
  /** Ready-to-render natural-language sentence — deterministic (Python
   * template, no LLM), the single source of truth every card renders verbatim. */
  narrative: string;
}

/** One policy rule as evaluated on this invoice (app/nodes/validation.py's
 * build_rubric): the facts observed, the criterion, and the outcome. */
export interface RubricRule {
  id: string;
  label: string;
  /** Failure-phrased title, e.g. "Over the auto-approval limit". */
  headline?: string;
  criterion: string;
  observed: string;
  status: "pass" | "flag";
  /** The flag text(s) this rule raised, when status is "flag". */
  finding?: string | null;
  /** 1-based severity rank among flagged rules (1 = primary trigger). */
  rank?: number | null;
}

/** Structured record of a human decision (app/nodes/approval.py's
 * _build_audit_record), written to state as `approval_audit`. */
export interface ApprovalAudit {
  decision: "approve" | "reject";
  reason_code: string;
  rationale_options?: string[];
  note: string;
  primary_trigger?: string | null;
  flagged_rules: string[];
  validation_flags: string[];
  override: boolean;
  attested_source_review: boolean;
  source_document_opened: boolean;
  reviewer?: string | null;
  decided_at: string;
}

/** Plain-language briefing for the approver (app/review_summary.py). */
export interface ReviewSummary {
  text: string;
  /** "llm" = written by the model; "fallback" = deterministic text. */
  source: "llm" | "fallback";
  model?: string | null;
  latency_ms?: number | null;
  /** Token usage of the call that wrote it (absent for the deterministic fallback). */
  input_tokens?: number;
  output_tokens?: number;
  thinking_tokens?: number;
  cache_read_tokens?: number;
}

/** The slice of ctx.state the UI mirrors from stateDelta. */
export interface PipelineState {
  screening_assessment?: InvoiceScreeningAssessment;
  /** Authoritative proceed/discard decision from the backend routing policy. */
  screening_route?: "invoice" | "not_invoice";
  invoice_record?: InvoiceRecord;
  /** route_extraction_strategy's deterministic size/complexity decision — how
   * the PDF is sent, and the model tier it recommends (only actually applied
   * when EXTRACTION_DYNAMIC_MODEL_TIERING is on server-side). */
  extraction_strategy?: "inline" | "files_api" | "multipass";
  extraction_recommended_model?: "lite" | "flash" | "pro";
  extraction_page_count?: number;
  extraction_byte_size?: number;
  extraction_has_native_text?: boolean;
  /** Gemini 3.x reasoning depth route_extraction_strategy applied for this tier
   * (always applied — unlike extraction_recommended_model, not gated by
   * EXTRACTION_DYNAMIC_MODEL_TIERING, since it's a depth knob on whichever
   * model is already running, not a model swap). */
  extraction_thinking_level?: "MINIMAL" | "LOW" | "MEDIUM" | "HIGH";
  /** Whether the recommended tier above was actually applied to extraction_agent
   * (EXTRACTION_DYNAMIC_MODEL_TIERING server-side) — false means it's advisory
   * only and extraction ran on its currently configured model. */
  extraction_dynamic_tiering_enabled?: boolean;
  validation_flags?: string[];
  validation_route?: string;
  /** Per-rule facts -> criterion -> result, plus the single ranked trigger. */
  validation_rubric?: RubricRule[];
  validation_primary_trigger?: string | null;
  review_summary?: ReviewSummary;
  /** LLM-written Agent Memory note (app/review_summary.py generate_memory_summary);
   * "fallback" means the precedent's own template sentence. */
  memory_summary?: ReviewSummary;
  approval_audit?: ApprovalAudit;
  po_match?: POMatchResult;
  posting_result?: PostingResult;
  /** Wall time of the ERP posting write (app/posting_store.py). */
  posting_latency_ms?: number;
  /** Wall time of the PO ledger lookup in spend classification (app/po_store.py). */
  classify_latency_ms?: number;
  policy_snapshot?: PolicySnapshot;
  escalation_result?: EscalationResult;
  notification_result?: NotificationResult;
  rejection_email_draft?: { subject?: string; body?: string };
  pipeline_status?: string;
  /** Model Armor verdicts (app/model_armor.py); present only when it is enabled. */
  armor_intake?: ArmorVerdict;
  armor_extraction?: ArmorVerdict;
  armor_rejection_email?: ArmorVerdict;
  /** PII masking of the email text the LLMs read (Model Armor de-identification): info-type names only, never values. */
  pii_masking?: {
    masked: boolean;
    info_types: string[];
    error: string | null;
    latency_ms: number;
  };
  /** Set when Model Armor stopped the invoice, at intake or after extraction. */
  armor_quarantine?: ArmorQuarantine;
  /** Set when a node failed and the run never reached an outcome (pipeline_status "error"). */
  pipeline_error?: string;
  pipeline_error_node?: string;
  invoice_sender_email?: string;
  /** AgentMail message id — lets the UI re-fetch the original source document
   * on demand (see api/client.ts's invoicePdfUrl) without the PDF bytes
   * ever riding the SSE/state wire themselves. */
  invoice_message_id?: string;
  decision_precedents?: PrecedentSummary;
  // Real model latency + name, emitted by ADK model callbacks in app/agent.py.
  screening_model?: string;
  screening_latency_ms?: number;
  screening_input_tokens?: number;
  screening_output_tokens?: number;
  /** Billed at the output-token rate but reported separately by the Gemini
   * API (thoughts_token_count) — Gemini 3.x models always think. */
  screening_thinking_tokens?: number;
  /** Tokens served from Gemini implicit context cache for this step (billed at 10% of standard input rate). */
  screening_cache_read_tokens?: number;
  extraction_model?: string;
  extraction_latency_ms?: number;
  extraction_input_tokens?: number;
  extraction_output_tokens?: number;
  extraction_thinking_tokens?: number;
  extraction_cache_read_tokens?: number;
  extraction_thought_text?: string;
  /** Real spend for the extraction step, blended across the main call's
   * (possibly tiered) model and the line-items fallback pass's model —
   * computed server-side where both models are known. Prefer this over
   * client-side computeCost() for the extraction step's displayed cost. */
  extraction_cost_usd?: number;
  draft_email_model?: string;
  draft_email_latency_ms?: number;
  draft_email_input_tokens?: number;
  draft_email_output_tokens?: number;
  draft_email_thinking_tokens?: number;
  draft_email_cache_read_tokens?: number;
  cost_summary?: CostSummary;
}

export interface InterruptInfo {
  interruptId: string;
  invocationId?: string;
  message?: string;
  kind: "approval" | "email_review" | "unknown";
}
