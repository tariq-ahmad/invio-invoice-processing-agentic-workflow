"""Pydantic schemas for the invoice-processing-agent pipeline."""

from typing import Literal

from pydantic import BaseModel, Field

# Evidence strength the screening agent assigns to each surface signal.
SignalStrength = Literal["strong", "weak", "absent"]

# How an extracted field value was obtained: read directly off the document,
# interpreted/reconstructed from an ambiguous or degraded source, or absent.
# Observable evidence, not a self-graded probability — a deterministic policy
# (app/nodes/validation.py) decides HITL routing from this, not the model.
SourceGrounding = Literal["grounded", "inferred", "not_found"]


class FieldValue(BaseModel):
    """A single extracted field with its source-grounding evidence."""

    value: str | None = Field(
        default=None,
        description="The extracted value, cleaned and trimmed. Null if genuinely "
        "absent from the document — never invented.",
    )
    source_grounding: SourceGrounding = Field(
        default="not_found",
        description="How this value was obtained: 'grounded' = read directly off "
        "the document, printed clearly and unambiguous. 'inferred' = the document "
        "shows the field but it had to be interpreted or reconstructed (partially "
        "obscured, ambiguous formatting, a blurry/low-quality scan). 'not_found' = "
        "genuinely absent from the document. Judge in isolation from every other "
        "field: one field's grounding must never raise or lower another's.",
    )


class LineItemAttribute(BaseModel):
    """One additional labeled data point a specific line item prints, beyond
    its name and amount. The label vocabulary is NOT fixed — invoices vary in
    what they attach to a line (circuit ID, site, billing period, unit rate,
    serial number, etc.). The model determines what's actually printed per
    line, per invoice; nothing here is a hardcoded enum.
    """

    label: str = Field(
        default="",
        description="This attribute's label, VERBATIM as printed on the "
        "document (e.g. if the invoice prints 'Circuit ID:', use exactly "
        "'Circuit ID') — never a paraphrase. Anchoring to the printed text, "
        "not the model's own wording, is what keeps the same invoice "
        "producing the same label across repeated extraction runs.",
    )
    value: str = Field(default="", description="That attribute's value for this line, exactly as printed.")


class LineItem(BaseModel):
    """One line item on the invoice.

    ``name``, ``amount``, and ``source_grounding`` are the model-chosen display
    values (shown on the card and fed into deterministic total reconciliation).
    ``attributes`` is an open-ended list of additional per-line data points —
    what it contains varies by invoice; see its own description.
    """

    name: str = Field(
        default="",
        description="Short human-readable name for this line — the product/"
        "service name, or its circuit/item identifier.",
    )
    amount: str = Field(default="", description="This line's cost/total, exactly as printed.")
    attributes: list[LineItemAttribute] = Field(
        default_factory=list,
        description="Additional distinct labeled data points THIS line prints "
        "beyond name/amount (e.g. circuit ID, site, billing period, rate, "
        "serial number) — only when the document actually shows them "
        "per-line. Decide per line what's really there; do not force every "
        "line to carry the same attributes, and never invent or copy an "
        "invoice-level value onto a line as if it were line-specific. Leave "
        "empty when the line has nothing beyond name/amount.",
    )
    source_grounding: SourceGrounding = Field(
        default="not_found",
        description="How this line item was obtained — same semantics as a "
        "header field's source_grounding. Judge each line item in isolation: "
        "one line item's grounding must never raise or lower another's, or "
        "any header field's.",
    )


class InvoiceRecord(BaseModel):
    """Structured invoice extracted by extraction_agent.

    Each top-level field carries a ``source_grounding`` evidence rating (grounded
    / inferred / not_found). There is no self-graded overall score — the
    deterministic validation policy derives the accept/HITL decision from
    per-field grounding plus arithmetic reconciliation of ``line_items`` against
    ``total``. ``line_items`` is declared LAST so that, if the model ever
    truncates its JSON output, every header field is already emitted before line
    items begin.
    """

    vendor: FieldValue = Field(
        default_factory=FieldValue,
        description="The supplier/biller name (who is owed money) — not the buyer.",
    )
    invoice_no: FieldValue = Field(
        default_factory=FieldValue,
        description="Invoice number / ID exactly as printed, e.g. 'INV-2026-0042'.",
    )
    po_ref: FieldValue = Field(
        default_factory=FieldValue,
        description="Purchase order reference, e.g. 'PO-2026-1003'. Null if the "
        "invoice shows no PO or only an illegible/placeholder value.",
    )
    total: FieldValue = Field(
        default_factory=FieldValue,
        description="The FINAL amount due for THIS invoice: the single largest "
        "aggregate the customer must actually pay — never a subtotal, per-section "
        "charge, or any individual line's amount, even if it appears in a larger "
        "font or earlier on the page. Plain numeric string, no currency symbol or "
        "thousands separators, e.g. '4750.00'.",
    )
    currency: FieldValue = Field(
        default_factory=FieldValue,
        description="ISO 4217 currency code, e.g. USD, EUR, GBP.",
    )
    line_items: list[LineItem] = Field(
        default_factory=list,
        description="Billable product/service lines from the SAME selected "
        "invoice, covering every page. Exclude summary/non-line rows — payment or "
        "adjustment entries, charge-summary rollups, period subtotals, taxes, "
        "past-due/balance-forward, and grand totals are summaries, not line items. "
        "Never collapse distinct products into one row. Empty list when the "
        "invoice has no billable line items.",
    )
    line_items_complete: bool = Field(
        default=True,
        description="False when line-item extraction was truncated before every "
        "row could be captured (a very large invoice exceeding the extraction "
        "call budget). Set by the pipeline, not the model — declared here so it "
        "survives InvoiceRecord(**raw) reconstruction. When False, line_items is "
        "a partial list and any total reconciliation is unreliable.",
    )


class POMatchResult(BaseModel):
    """Result of the S/4HANA PO lookup / spend classification."""

    route: str  # "direct" | "indirect"
    po_number: str | None = None
    po_type: str | None = None
    matched: bool = False
    vendor_name: str | None = None
    po_amount: float | None = None
    currency: str | None = None
    category: str | None = None
    cost_center: str | None = None
    gl_account: str | None = None
    requisitioner: str | None = None
    po_status: str | None = None
    target_system: str | None = None  # "SAP" | "Coupa AP"
    store_path: str | None = None  # Firestore document the PO was read from
    latency_ms: int | None = None  # wall time of the PO lookup


class PostingResult(BaseModel):
    """Confirmed posting in SAP CIMS or Coupa AP."""

    system: str  # e.g. "ERP"
    document_id: str
    status: str = "posted"
    message: str = ""
    posted_at: str | None = None  # ISO-8601 timestamp of the posting
    reference: str | None = None  # vendor / invoice reference on the document
    store_path: str | None = None  # Firestore document the posting was written to
    latency_ms: int | None = None  # wall time of the posting write


class EscalationResult(BaseModel):
    """ServiceNow ticket raised for a confirmed duplicate invoice."""

    ticket_id: str
    system: str = "ServiceNow"
    status: str = "created"
    reason: str = ""
    invoice_no: str | None = None
    priority: str = "3 - Moderate"
    assignment_group: str = "Accounts Payable Operations"
    created_at: str | None = None


class NotificationResult(BaseModel):
    """Outcome of the vendor rejection notification."""

    email_sent: bool
    recipient: str = ""
    subject: str = ""
    summary: str = ""


class InvoiceScreeningAssessment(BaseModel):
    """Structured evidence assessment from the intake screening agent.

    The agent reports observed invoice-evidence strength per surface source. The
    routing policy — not the model — decides whether to proceed to extraction.
    """

    # Required (no default) so controlled generation forces the model to commit a
    # value per source — an empty/degenerate response must not silently read as
    # all-"absent", which the fail-open routing policy would treat as discard.
    attachment_signal: SignalStrength = Field(
        ...,
        description="Evidence from the attachment: a PDF is present AND its "
        "filename reads like a vendor invoice (names a vendor, 'invoice', a "
        "document number). Presence and filename only — never PDF contents.",
    )
    body_signal: SignalStrength = Field(
        ...,
        description="Evidence in the email body of a genuine invoice/PO "
        "transmittal: amount due, remittance/payment terms, or a reference to an "
        "attached invoice or PO.",
    )
    subject_signal: SignalStrength = Field(
        ...,
        description="Strength of invoice/PO intent or identifiers in the subject line.",
    )
    observed_signals: list[str] = Field(
        default_factory=list,
        description="Concrete cues actually observed, for audit. No PDF contents, "
        "no fabricated cues.",
    )
    assessment_rationale: str = Field(
        default="",
        description="One-sentence justification for the assessment.",
    )


class RejectionEmail(BaseModel):
    """LLM-drafted vendor rejection email."""

    subject: str
    body: str
