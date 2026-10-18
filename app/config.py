"""Pipeline configuration and mock master data.

Demo scenario: the buying enterprise (``BUYER_ORG``) runs its Accounts Payable
team on **AP Copilot**. Invoices arrive by email, are read by Gemini, validated
against the AP rule book, matched to a purchase order, and posted to the ERP
(both direct and indirect spend) — pausing for human approval when a rule is
tripped. The master data below (the PO ledger and the posted-invoice history)
is mocked seed data so the demo is fully self-contained and repeatable; each
block notes the real system it stands in for.
"""

import os

# ---------------------------------------------------------------------------
# Brand / tenant identity (neutral fictional enterprise — used in UI + emails)
# ---------------------------------------------------------------------------
PRODUCT_NAME = "Invoice Agentic Workflow"
BUYER_ORG = "Agent Corporation"
AP_TEAM_SIGNATURE = (
    f"Regards,\n\n"
    f"Accounts Payable Team\n"
    f"{BUYER_ORG}"
)

# ---------------------------------------------------------------------------
# AgentMail intake (see app/agentmail_tools.py, app/agentmail_listener.py)
# ---------------------------------------------------------------------------
# The AgentMail inbox this deployment monitors for invoices — its email address,
# e.g. "invoices@agentmail.to". Used for the WebSocket subscription, every REST
# call, and the UI/messages that reference the inbox.
AGENTMAIL_INBOX_ID: str = os.getenv("AGENTMAIL_INBOX_ID", "").strip()

# API key. Locally set AGENTMAIL_API_KEY directly. On Cloud Run set
# AGENTMAIL_API_KEY_SECRET to a Secret Manager resource name
# (projects/<project>/secrets/<name>/versions/latest) so the key never appears in
# the service's env vars.
AGENTMAIL_API_KEY: str = os.getenv("AGENTMAIL_API_KEY", "").strip()
AGENTMAIL_API_KEY_SECRET: str = os.getenv("AGENTMAIL_API_KEY_SECRET", "").strip()

# Kill switch for the WebSocket listener (pause/resume intake without a redeploy
# of code; see deployment/toggle-agentmail-intake.sh). The rest of the service
# keeps running either way.
AGENTMAIL_INTAKE_ENABLED: bool = os.getenv("AGENTMAIL_INTAKE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)

# After each (re)connect, pick up unhandled mail from this many minutes back.
# AgentMail does not replay events missed while disconnected, so this bounds the
# gap a restart/redeploy can leave. 0 disables catch-up, which also means nothing
# older than the connection is ever processed (the old "baseline" guardrail).
AGENTMAIL_CATCHUP_MINUTES: int = int(os.getenv("AGENTMAIL_CATCHUP_MINUTES", "30"))

# Test-only: inbox used to send fixture invoices to AGENTMAIL_INBOX_ID
# (tests/fixtures/seed_inbox.py). Empty -> self-send from the AP inbox.
AGENTMAIL_SEED_INBOX_ID: str = os.getenv("AGENTMAIL_SEED_INBOX_ID", "").strip()

# Send vendor rejection emails through AgentMail's hosted MCP server instead of
# the SDK. If the MCP call fails the node falls back to the SDK send.
AGENTMAIL_SEND_VIA_MCP: bool = os.getenv("AGENTMAIL_SEND_VIA_MCP", "true").lower() in (
    "1", "true", "yes", "on",
)
AGENTMAIL_MCP_URL: str = os.getenv("AGENTMAIL_MCP_URL", "https://mcp.agentmail.to/mcp").strip()

# Recipient for vendor rejection drafts when the vendor's own address is unknown.
VENDOR_FALLBACK_EMAIL = os.getenv("VENDOR_FALLBACK_EMAIL", "ap-vendors@northwind.example")

# ---------------------------------------------------------------------------
# Firestore-backed configuration (see app/firestore_store.py, app/policy.py,
# app/po_store.py)
# ---------------------------------------------------------------------------
# The AP policies and the PO master data are read from Firestore at runtime so
# anyone with GCP access can change them without a code change or redeploy. The
# values defined in this file (PO_AMOUNT_HITL_THRESHOLD_USD, FX_TO_USD,
# MOCK_PO_DATABASE, ...) are the DEFAULTS: they seed Firestore
# (deployment/seed_firestore.py) and are the fallback when a policy document is
# missing, invalid or Firestore is unreachable.
#
# "firestore" (default) reads the live database; "local" serves the in-code
# defaults instead, for offline development without GCP credentials.
CONFIG_SOURCE: str = os.getenv("CONFIG_SOURCE", "firestore").strip().lower()
FIRESTORE_DATABASE: str = os.getenv("FIRESTORE_DATABASE", "(default)").strip() or "(default)"
# Collection holding the policy documents ("ap_rules", "fx_rates").
POLICIES_COLLECTION: str = os.getenv("FIRESTORE_POLICIES_COLLECTION", "policies").strip()
# Collection holding one document per PO (document id = canonical PO number).
PURCHASE_ORDERS_COLLECTION: str = os.getenv(
    "FIRESTORE_PURCHASE_ORDERS_COLLECTION", "purchase_orders"
).strip()
# Collection the ERP posting step writes each posted invoice to
# (document id = the posting's document number, e.g. ERP-AB12CD34EF).
POSTED_INVOICES_COLLECTION: str = os.getenv(
    "FIRESTORE_POSTED_INVOICES_COLLECTION", "posted_invoices"
).strip()
# Collection holding one small outcome document per completed run (document id =
# the run's invocation id). Written by pipeline_complete, aggregated by GET /stats
# for the UI's stats drawer. See app/run_stats.py.
RUN_OUTCOMES_COLLECTION: str = os.getenv(
    "FIRESTORE_RUN_OUTCOMES_COLLECTION", "run_outcomes"
).strip()
# How long a fetched policy is reused before Firestore is read again — the
# worst-case delay before an edit made in the console takes effect.
POLICY_CACHE_TTL_SECONDS: float = float(os.getenv("POLICY_CACHE_TTL_SECONDS", "60"))

# ---------------------------------------------------------------------------
# AP policy defaults (live values come from Firestore: policies/ap_rules)
# ---------------------------------------------------------------------------
# POs / invoice totals above this threshold always go to HITL approval.
PO_AMOUNT_HITL_THRESHOLD_USD = 4_000.0
# Line items must sum to the invoice total within this fraction (or $0.01,
# whichever is larger) or the invoice is flagged for human review. Deterministic
# reconciliation replaces the old self-graded confidence score as the primary
# extraction-quality signal.
RECONCILE_TOLERANCE_PCT = 0.01
# At least this many of the three screening signals (attachment, body, subject)
# must be independently "strong" for an email to proceed to extraction.
SCREENING_MIN_STRONG_SIGNALS = 2
# An extracted invoice number shorter than this is flagged as not a valid id.
MIN_INVOICE_NO_LENGTH = 2
# Spend route for an invoice whose PO is not found in the PO ledger.
UNMATCHED_PO_ROUTE = "direct"
# PO checks (run in validation once the PO has been looked up and matched):
# the invoice vendor must be at least this similar (0-1, after dropping legal
# suffixes like Inc/LLC) to the vendor on the PO; 0 disables the check.
PO_VENDOR_MIN_SIMILARITY = 0.85
# An invoice total may exceed the PO amount (compared in USD) by at most this
# fraction before it is flagged; an invoice BELOW the PO amount is a normal
# partial billing and never flagged. None disables the check.
PO_AMOUNT_TOLERANCE_PCT: float | None = 0.05
# A PO in one of these statuses is not open for invoicing; the invoice is flagged.
BLOCKED_PO_STATUSES: tuple[str, ...] = ("closed", "cancelled", "fully_invoiced")

# ---------------------------------------------------------------------------
# Extraction size-tiering — route_extraction_strategy (app/nodes/extraction.py)
# ---------------------------------------------------------------------------
# Gemini's inline-request payload cap; above this the PDF must go through the
# Files API instead of being embedded as base64 bytes.
FILES_API_SIZE_THRESHOLD_BYTES = 20 * 1024 * 1024
# Page count above which extraction always runs a dedicated line-item pass
# (rather than relying on the truncation-triggered fallback) so very long
# invoices don't lose line items to the structured-output token ceiling.
MULTIPASS_PAGE_THRESHOLD = 25
# Small, native-text, single-page-ish invoices this size or under are eligible
# for the cheapest model tier.
SMALL_INVOICE_PAGE_THRESHOLD = 3
SMALL_INVOICE_SIZE_THRESHOLD_BYTES = 2 * 1024 * 1024
# A phone-scanning app (Adobe Scan, Google Drive scan, iOS Notes, CamScanner,
# etc.) embeds a real, searchable OCR text layer UNDER a full-page photograph
# of the document — extract_text() finding that layer must not be mistaken for
# "clean born-digital document." A page whose dominant embedded image is at
# least this many pixels on its shorter side, AND whose aspect ratio is within
# DOMINANT_IMAGE_ASPECT_TOLERANCE of the page's own aspect ratio, is treated as
# a photographed/scanned page regardless of any text layer found on it.
DOMINANT_IMAGE_MIN_PX = 400
DOMINANT_IMAGE_ASPECT_TOLERANCE = 0.35

# Max number of free-form line-item calls _extract_line_items_direct will make
# for one invoice. Each Gemini call is bounded by the model's 65,536 output-token
# ceiling (~450 richly-attributed items/call), so very large invoices need
# several sequential calls, each resuming where the last truncated. 8 calls ≈ up
# to ~3,600 items; a typical 500-item invoice needs 1-2. Raising this trades
# worst-case latency (each call adds ~20-40s, all inside the synchronous
# extraction node) for completeness on pathologically large invoices; if the cap
# is hit while still truncated, the invoice is flagged line_items_incomplete and
# forced to human review rather than silently under-reporting.
MAX_LINE_ITEM_CALLS: int = int(os.getenv("MAX_LINE_ITEM_CALLS", "8"))
# How many already-captured line items to quote verbatim in a continuation prompt
# as the "resume after these" anchor. 3 gives the model enough context to find
# the boundary without repeating, while keeping the prompt small.
LINE_ITEM_CONTINUATION_ANCHOR_K: int = int(os.getenv("LINE_ITEM_CONTINUATION_ANCHOR_K", "3"))

# Whether route_extraction_strategy actually re-points the extraction agent to
# a cheaper/pricier tier. Validated on the "lite" tier (small/clean/native-text
# invoices): gemini-3.1-flash-lite reproduced identical grounded field values
# to gemini-3.5-flash on tests/fixtures/invoices/01_direct_straight_through.pdf
# (see artifacts/probe_baseline.json / probe_candidate2.json). The "pro" tier
# (scanned/complex docs) and multipass path are NOT yet separately validated —
# monitor those via production observability (timeline / cost card) rather
# than assuming parity.
EXTRACTION_DYNAMIC_MODEL_TIERING: bool = os.getenv(
    "EXTRACTION_DYNAMIC_MODEL_TIERING", "true"
).strip().lower() in ("1", "true", "yes", "on")

# GCS bucket (name only, no "gs://") for staging invoice PDFs that exceed
# Gemini's 20 MB inline-request limit — Vertex AI requires a Cloud Storage URI
# for files that size, unlike the Gemini Developer API's ephemeral Files API.
# Empty -> not yet provisioned; the files_api/multipass strategies fall back
# to inline transport with a warning (same empty-string-fallback idiom as
# MEMORY_AGENT_ENGINE_ID above). Provision via the deploy-infra skill, then set
# this and the service account needs roles/storage.objectUser on the bucket.
EXTRACTION_GCS_BUCKET: str = os.getenv("EXTRACTION_GCS_BUCKET", "").strip()

# Approximate, STATIC demo FX rates → USD (not live FX). Used to compare invoice
# amounts in any currency against the USD thresholds above. Unknown currencies
# fall back to FX_DEFAULT_RATE (1.0, i.e. treated as USD). These are the
# defaults; live values come from Firestore (policies/fx_rates).
FX_DEFAULT_RATE = 1.0
FX_TO_USD: dict[str, float] = {
    "USD": 1.0,
    "INR": 0.012,
    "EUR": 1.08,
    "GBP": 1.27,
    "JPY": 0.0067,
    "AUD": 0.66,
    "CAD": 0.73,
    "SGD": 0.74,
    "AED": 0.27,
}


def to_usd(amount: float, currency: str | None) -> float:
    """Convert an amount in `currency` to USD using the DEFAULT static FX table.

    The pipeline itself converts through ``app.policy.Policy.to_usd`` (the
    Firestore-backed table); this is the in-code default.
    """
    return amount * FX_TO_USD.get((currency or "USD").strip().upper(), FX_DEFAULT_RATE)

# ---------------------------------------------------------------------------
# Required AP fields (must be present and high-confidence for straight-through)
# ---------------------------------------------------------------------------
REQUIRED_FIELDS: list[str] = ["vendor", "invoice_no", "po_ref", "total"]

# ---------------------------------------------------------------------------
# Posted-invoice history — SEED DATA for the Firestore `posted_invoices`
# collection (deployment/seed_firestore.py) and the `CONFIG_SOURCE=local`
# fallback. Duplicate detection does NOT read this list: at runtime
# validate_invoice queries Firestore `posted_invoices` (app/duplicate_check.py),
# the same collection post_to_erp writes to, so every invoice the
# pipeline posts becomes duplicate-protected automatically. These entries only
# stand in for invoices booked before go-live so the duplicate scenarios in
# tests/fixtures/invoices (04, 05) have history to collide with. Each entry is
# turned into a full document by app.duplicate_check.history_document().
# ---------------------------------------------------------------------------
MOCK_POSTED_INVOICES: list[dict] = [
    # 04_duplicate_invoice_number.pdf re-submits this invoice number.
    {"document_id": "SAP-PRD-100-1000-HIST0001", "system": "SAP_CIMS", "vendor": "Cascade Components Inc", "invoice_no": "INV-2024-001", "po_ref": "PO-2026-1002", "total": 5_400.0, "currency": "USD", "posted_at": "2026-01-27T10:15:00+00:00"},
    # 05_duplicate_po.pdf bills the same vendor, PO and amount again under a new invoice number.
    {"document_id": "SAP-PRD-100-1000-HIST0002", "system": "SAP_CIMS", "vendor": "Globex Trading Co", "invoice_no": "INV-2026-0211", "po_ref": "PO-2026-0098", "total": 6_000.0, "currency": "USD", "posted_at": "2026-05-04T14:02:00+00:00"},
    {"document_id": "SAP-PRD-100-1000-HIST0003", "system": "SAP_CIMS", "vendor": "Meridian Steel & Alloys", "invoice_no": "INV-2025-0455", "po_ref": "PO-2026-1001", "total": 8_750.0, "currency": "USD", "posted_at": "2026-02-10T09:30:00+00:00"},
    {"document_id": "COUPA-HIST000004", "system": "Coupa_AP", "vendor": "Helix Cloud Services", "invoice_no": "INV-2026-0012", "po_ref": "PO-2026-2001", "total": 6_200.0, "currency": "USD", "posted_at": "2026-02-18T16:45:00+00:00"},
    {"document_id": "COUPA-HIST000005", "system": "Coupa_AP", "vendor": "BrightOffice Supplies Co", "invoice_no": "INV-2026-0027", "po_ref": "PO-2026-2002", "total": 920.0, "currency": "USD", "posted_at": "2026-03-02T11:20:00+00:00"},
]

# ---------------------------------------------------------------------------
# Mock S/4HANA PO ledger — SEED DATA for the Firestore `purchase_orders`
# collection (deployment/seed_firestore.py) and the `CONFIG_SOURCE=local`
# fallback. At runtime the PO lookup reads Firestore (app/po_store.py); edit the
# documents there rather than this dict. Replace with a real RFC/OData call to
# S/4HANA when wiring up the live system.
#
# Each record mirrors the fields an AP clerk would see on a PO header:
#   type          - "direct" (cost of goods, account-assignment cat. P) or
#                   "indirect" (overhead, cat. K) — both route to the ERP
#   vendor        - supplier on the PO (used to corroborate the invoice vendor)
#   amount        - PO net value (the committed spend)
#   currency      - ISO 4217
#   category      - spend category / commodity
#   cost_center   - controlling cost center the spend is booked to
#   gl_account    - G/L account for the posting
#   requisitioner - buyer / requester who raised the PO
#   status        - "open" | "partially_invoiced" | "approved"
#   created       - PO creation date (ISO)
#
# 20 direct (PO-2026-10xx) + 20 indirect (PO-2026-20xx) = 40 records. Several of
# each route sit below PO_AMOUNT_HITL_THRESHOLD_USD so straight-through posting
# can be demoed on both direct and indirect ERP paths.
# ---------------------------------------------------------------------------
MOCK_PO_DATABASE: dict[str, dict] = {
    # ---- Direct spend (cost of goods / production) -> SAP CIMS ----
    "PO-2026-1001": {"type": "direct", "vendor": "Meridian Steel & Alloys", "amount": 8_750.0, "currency": "USD", "category": "Raw Materials", "cost_center": "CC-PROD-1100", "gl_account": "500100", "requisitioner": "A. Whitfield", "status": "open", "created": "2026-01-12"},
    "PO-2026-1002": {"type": "direct", "vendor": "Cascade Components Inc", "amount": 5_400.0, "currency": "USD", "category": "Components", "cost_center": "CC-PROD-1120", "gl_account": "500120", "requisitioner": "R. Delgado", "status": "open", "created": "2026-01-19"},
    "PO-2026-1003": {"type": "direct", "vendor": "BluePeak Plastics", "amount": 3_250.0, "currency": "USD", "category": "Raw Materials", "cost_center": "CC-PROD-1100", "gl_account": "500100", "requisitioner": "A. Whitfield", "status": "open", "created": "2026-02-03"},
    "PO-2026-1004": {"type": "direct", "vendor": "Ironclad Fasteners LLC", "amount": 2_150.0, "currency": "USD", "category": "Components", "cost_center": "CC-PROD-1120", "gl_account": "500120", "requisitioner": "R. Delgado", "status": "open", "created": "2026-02-08"},
    "PO-2026-1005": {"type": "direct", "vendor": "Summit Circuit Boards", "amount": 18_250.0, "currency": "USD", "category": "Electronic Components", "cost_center": "CC-PROD-1150", "gl_account": "500150", "requisitioner": "K. Nakamura", "status": "open", "created": "2026-02-15"},
    "PO-2026-1006": {"type": "direct", "vendor": "Apex Industrial Bearings", "amount": 6_900.0, "currency": "USD", "category": "Components", "cost_center": "CC-PROD-1120", "gl_account": "500120", "requisitioner": "R. Delgado", "status": "partially_invoiced", "created": "2026-02-20"},
    "PO-2026-1007": {"type": "direct", "vendor": "Continental Wire & Cable", "amount": 4_480.0, "currency": "USD", "category": "Raw Materials", "cost_center": "CC-PROD-1100", "gl_account": "500100", "requisitioner": "A. Whitfield", "status": "open", "created": "2026-03-01"},
    "PO-2026-1008": {"type": "direct", "vendor": "Granite Metal Works", "amount": 9_300.0, "currency": "USD", "category": "Raw Materials", "cost_center": "CC-PROD-1100", "gl_account": "500100", "requisitioner": "A. Whitfield", "status": "open", "created": "2026-03-04"},
    "PO-2026-1009": {"type": "direct", "vendor": "Pacific Polymers Corp", "amount": 7_120.0, "currency": "USD", "category": "Raw Materials", "cost_center": "CC-PROD-1100", "gl_account": "500100", "requisitioner": "A. Whitfield", "status": "open", "created": "2026-03-09"},
    "PO-2026-1010": {"type": "direct", "vendor": "Meridian Steel & Alloys", "amount": 12_500.0, "currency": "USD", "category": "Raw Materials", "cost_center": "CC-PROD-1100", "gl_account": "500100", "requisitioner": "A. Whitfield", "status": "open", "created": "2026-03-12"},
    "PO-2026-1011": {"type": "direct", "vendor": "Vanguard Machining Co", "amount": 5_650.0, "currency": "USD", "category": "Manufacturing Supplies", "cost_center": "CC-PROD-1130", "gl_account": "500130", "requisitioner": "L. Ferraro", "status": "open", "created": "2026-03-18"},
    "PO-2026-1012": {"type": "direct", "vendor": "Titan Tooling Group", "amount": 3_990.0, "currency": "USD", "category": "Manufacturing Supplies", "cost_center": "CC-PROD-1130", "gl_account": "500130", "requisitioner": "L. Ferraro", "status": "open", "created": "2026-03-22"},
    "PO-2026-1013": {"type": "direct", "vendor": "Northgate Castings", "amount": 8_100.0, "currency": "USD", "category": "Components", "cost_center": "CC-PROD-1120", "gl_account": "500120", "requisitioner": "R. Delgado", "status": "open", "created": "2026-03-27"},
    "PO-2026-1014": {"type": "direct", "vendor": "Stellar Semiconductors", "amount": 22_400.0, "currency": "USD", "category": "Electronic Components", "cost_center": "CC-PROD-1150", "gl_account": "500150", "requisitioner": "K. Nakamura", "status": "open", "created": "2026-04-02"},
    "PO-2026-1015": {"type": "direct", "vendor": "Redwood Lumber Supply", "amount": 2_750.0, "currency": "USD", "category": "Raw Materials", "cost_center": "CC-PROD-1100", "gl_account": "500100", "requisitioner": "A. Whitfield", "status": "open", "created": "2026-04-06"},
    "PO-2026-1016": {"type": "direct", "vendor": "Atlas Hydraulics", "amount": 6_320.0, "currency": "USD", "category": "Components", "cost_center": "CC-PROD-1120", "gl_account": "500120", "requisitioner": "R. Delgado", "status": "open", "created": "2026-04-11"},
    "PO-2026-1017": {"type": "direct", "vendor": "Crestline Adhesives", "amount": 1_880.0, "currency": "USD", "category": "Manufacturing Supplies", "cost_center": "CC-PROD-1130", "gl_account": "500130", "requisitioner": "L. Ferraro", "status": "open", "created": "2026-04-15"},
    "PO-2026-1018": {"type": "direct", "vendor": "Sterling Sheet Metal", "amount": 10_250.0, "currency": "USD", "category": "Raw Materials", "cost_center": "CC-PROD-1100", "gl_account": "500100", "requisitioner": "A. Whitfield", "status": "open", "created": "2026-04-20"},
    "PO-2026-1019": {"type": "direct", "vendor": "Quantum Electronics Mfg", "amount": 15_600.0, "currency": "USD", "category": "Electronic Components", "cost_center": "CC-PROD-1150", "gl_account": "500150", "requisitioner": "K. Nakamura", "status": "open", "created": "2026-04-24"},
    "PO-2026-1020": {"type": "direct", "vendor": "Evergreen Packaging Materials", "amount": 4_730.0, "currency": "USD", "category": "Packaging Materials", "cost_center": "CC-PROD-1140", "gl_account": "500140", "requisitioner": "S. Bauer", "status": "open", "created": "2026-04-29"},
    # ---- Indirect spend (overhead / operations) -> Coupa AP ----
    "PO-2026-2001": {"type": "indirect", "vendor": "Helix Cloud Services", "amount": 6_200.0, "currency": "USD", "category": "IT & Cloud Services", "cost_center": "CC-IT-5300", "gl_account": "640300", "requisitioner": "P. Nguyen", "status": "open", "created": "2026-01-15"},
    "PO-2026-2002": {"type": "indirect", "vendor": "BrightOffice Supplies Co", "amount": 920.0, "currency": "USD", "category": "Office Supplies", "cost_center": "CC-OPS-4200", "gl_account": "620100", "requisitioner": "T. Alvarez", "status": "open", "created": "2026-01-22"},
    "PO-2026-2003": {"type": "indirect", "vendor": "Pinnacle Facilities Management", "amount": 3_400.0, "currency": "USD", "category": "Facilities Management", "cost_center": "CC-FAC-4400", "gl_account": "630200", "requisitioner": "D. O'Connor", "status": "open", "created": "2026-01-28"},
    "PO-2026-2004": {"type": "indirect", "vendor": "Lumen Marketing Group", "amount": 14_800.0, "currency": "USD", "category": "Marketing", "cost_center": "CC-MKT-7100", "gl_account": "660100", "requisitioner": "E. Rossi", "status": "open", "created": "2026-02-05"},
    "PO-2026-2005": {"type": "indirect", "vendor": "Vertex Consulting Partners", "amount": 26_500.0, "currency": "USD", "category": "Professional Services", "cost_center": "CC-FIN-3100", "gl_account": "650400", "requisitioner": "H. Park", "status": "open", "created": "2026-02-11"},
    "PO-2026-2006": {"type": "indirect", "vendor": "SwiftTravel Corporate", "amount": 5_120.0, "currency": "USD", "category": "Travel & Expense", "cost_center": "CC-OPS-4200", "gl_account": "670300", "requisitioner": "T. Alvarez", "status": "open", "created": "2026-02-17"},
    "PO-2026-2007": {"type": "indirect", "vendor": "Helix Cloud Services", "amount": 4_750.0, "currency": "USD", "category": "IT & Cloud Services", "cost_center": "CC-IT-5300", "gl_account": "640300", "requisitioner": "P. Nguyen", "status": "open", "created": "2026-02-24"},
    "PO-2026-2008": {"type": "indirect", "vendor": "DataGuard IT Security", "amount": 9_800.0, "currency": "USD", "category": "IT & Cloud Services", "cost_center": "CC-IT-5300", "gl_account": "640300", "requisitioner": "P. Nguyen", "status": "open", "created": "2026-03-02"},
    "PO-2026-2009": {"type": "indirect", "vendor": "Clearview Janitorial Services", "amount": 2_100.0, "currency": "USD", "category": "Facilities Management", "cost_center": "CC-FAC-4400", "gl_account": "630200", "requisitioner": "D. O'Connor", "status": "open", "created": "2026-03-06"},
    "PO-2026-2010": {"type": "indirect", "vendor": "Orbit Software Licensing", "amount": 11_250.0, "currency": "USD", "category": "Software Licenses", "cost_center": "CC-IT-5300", "gl_account": "640500", "requisitioner": "P. Nguyen", "status": "open", "created": "2026-03-10"},
    "PO-2026-2011": {"type": "indirect", "vendor": "Greenfield Catering", "amount": 1_450.0, "currency": "USD", "category": "Office Services", "cost_center": "CC-OPS-4200", "gl_account": "620300", "requisitioner": "T. Alvarez", "status": "open", "created": "2026-03-14"},
    "PO-2026-2012": {"type": "indirect", "vendor": "Metro Print & Signage", "amount": 2_380.0, "currency": "USD", "category": "Marketing", "cost_center": "CC-MKT-7100", "gl_account": "660100", "requisitioner": "E. Rossi", "status": "open", "created": "2026-03-19"},
    "PO-2026-2013": {"type": "indirect", "vendor": "Sentinel Security Systems", "amount": 7_600.0, "currency": "USD", "category": "Facilities Management", "cost_center": "CC-FAC-4400", "gl_account": "630200", "requisitioner": "D. O'Connor", "status": "open", "created": "2026-03-23"},
    "PO-2026-2014": {"type": "indirect", "vendor": "Aspen HR Advisory", "amount": 8_900.0, "currency": "USD", "category": "Professional Services", "cost_center": "CC-HR-3300", "gl_account": "650200", "requisitioner": "G. Mbeki", "status": "open", "created": "2026-03-28"},
    "PO-2026-2015": {"type": "indirect", "vendor": "Nimbus Telecom", "amount": 3_150.0, "currency": "USD", "category": "Telecom", "cost_center": "CC-IT-5300", "gl_account": "640400", "requisitioner": "P. Nguyen", "status": "open", "created": "2026-04-03"},
    "PO-2026-2016": {"type": "indirect", "vendor": "Beacon Legal Services", "amount": 19_500.0, "currency": "USD", "category": "Legal", "cost_center": "CC-LEG-3200", "gl_account": "650100", "requisitioner": "C. Whitman", "status": "open", "created": "2026-04-08"},
    "PO-2026-2017": {"type": "indirect", "vendor": "Cobalt Staffing Solutions", "amount": 13_400.0, "currency": "USD", "category": "Contingent Staffing", "cost_center": "CC-HR-3300", "gl_account": "650300", "requisitioner": "G. Mbeki", "status": "open", "created": "2026-04-13"},
    "PO-2026-2018": {"type": "indirect", "vendor": "Horizon Logistics & Courier", "amount": 2_680.0, "currency": "USD", "category": "Logistics", "cost_center": "CC-OPS-4200", "gl_account": "670100", "requisitioner": "T. Alvarez", "status": "open", "created": "2026-04-18"},
    "PO-2026-2019": {"type": "indirect", "vendor": "Lattice Analytics SaaS", "amount": 7_200.0, "currency": "USD", "category": "Software Licenses", "cost_center": "CC-IT-5300", "gl_account": "640500", "requisitioner": "P. Nguyen", "status": "open", "created": "2026-04-22"},
    "PO-2026-2020": {"type": "indirect", "vendor": "Fountain Water & Coffee Services", "amount": 640.0, "currency": "USD", "category": "Office Services", "cost_center": "CC-OPS-4200", "gl_account": "620300", "requisitioner": "T. Alvarez", "status": "open", "created": "2026-04-27"},
}


def _normalize_po(po_ref: str | None) -> str:
    """Normalize a PO reference for lookup.

    Vendors print PO numbers inconsistently ("po 2026 1003", "PO#2026-1003").
    We upper-case, trim, drop a leading ``#``, and collapse internal spaces to
    dashes so a human-legible reference still resolves to the canonical key.
    """
    if not po_ref:
        return ""
    cleaned = po_ref.strip().upper().lstrip("#").strip()
    # Collapse runs of spaces / underscores to single dashes.
    for sep in ("_", " "):
        cleaned = cleaned.replace(sep, "-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned


# PO lookup lives in app/po_store.py (``lookup_po``): it reads the Firestore
# `purchase_orders` collection and uses _normalize_po above for the document id.

# ---------------------------------------------------------------------------
# Agent Memory Bank — advisory approve/reject precedent per vendor + flag kind
# ---------------------------------------------------------------------------
# Off by default: the recall_precedents node still runs (it's a graph node, not
# optional wiring) but is a no-op when this is false — the fastest rollback
# lever is `ENABLE_MEMORY=false`, no redeploy of a previous revision needed.
# See docs/spec-agent-memory-demo.md for the full rollback strategy.
ENABLE_MEMORY: bool = os.getenv("ENABLE_MEMORY", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Minimum consistent prior decisions (all-approve or all-reject) before the
# Agent Memory card shows an advisory recommendation.
MEMORY_PRECEDENT_MIN: int = int(os.getenv("MEMORY_PRECEDENT_MIN", "2"))
# Vertex AI Agent Engine id used PURELY as the Memory Bank backend (no agent
# code deployed there) — e.g. the "ledger-invoice-memory-demo" engine. Empty ->
# local InMemoryMemoryService fallback, no provisioning needed for local/demo.
MEMORY_AGENT_ENGINE_ID: str = os.getenv("MEMORY_AGENT_ENGINE_ID", "").strip()
# Memory Bank — including its embedding / similarity-search model — is confirmed
# working on the us-west1 regional endpoint, so the memory Agent Engine is
# co-located with Cloud Run (us-west1) to drop the cross-region hop on every recall.
MEMORY_LOCATION: str = os.getenv("MEMORY_LOCATION", "us-west1")

# ---------------------------------------------------------------------------
# Model Armor — screens untrusted invoice content (see app/model_armor.py)
# ---------------------------------------------------------------------------
# Off by default: with the flag false every scan is a no-op, so it is also the
# fastest rollback lever (no code redeploy needed). The template lives in a
# REGION (not `global`), and Model Armor is always called on its own regional
# endpoint — independent of the Gemini `global` endpoint in app/agent.py.
MODEL_ARMOR_ENABLED: bool = os.getenv("MODEL_ARMOR_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Which scan points are active (comma-separated): "intake" (the email + attachment, right
# after fetch), "extraction" (text the model read out of the PDF) and "outbound" (the
# rejection email draft and its final pre-send check). Default: intake only, so the one
# security scan runs before any AI reads the email. Add the others to re-enable them,
# e.g. MODEL_ARMOR_SCOPES=intake,extraction,outbound. Only applies when MODEL_ARMOR_ENABLED.
MODEL_ARMOR_SCOPES: frozenset[str] = frozenset(
    s.strip().lower() for s in os.getenv("MODEL_ARMOR_SCOPES", "intake").split(",") if s.strip()
)
MODEL_ARMOR_TEMPLATE: str = os.getenv("MODEL_ARMOR_TEMPLATE", "invoice_processing_model_armor").strip()
MODEL_ARMOR_LOCATION: str = os.getenv("MODEL_ARMOR_LOCATION", "us-west1").strip()
# Inspect-only rollout: false records every verdict (state, span, logs) but never
# blocks anything. Flip to true once the verdicts look right on real invoices.
MODEL_ARMOR_ENFORCE: bool = os.getenv("MODEL_ARMOR_ENFORCE", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# When Model Armor can't give a verdict (API error, file too large, ...): true
# stops the invoice (a retryable AP-Error at intake and after extraction, no send
# for outbound email); false lets it through with a warning.
MODEL_ARMOR_FAIL_CLOSED: bool = os.getenv("MODEL_ARMOR_FAIL_CLOSED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Filters whose matches are recorded but NOT acted on for INBOUND content.
# Invoices legitimately carry bank details, tax IDs and addresses, so Sensitive
# Data Protection ("sdp") would otherwise reject ordinary invoices. Outbound
# content (the rejection email) is never exempted. Comma-separated.
MODEL_ARMOR_INBOUND_IGNORE_FILTERS: frozenset[str] = frozenset(
    f.strip().lower()
    for f in os.getenv("MODEL_ARMOR_INBOUND_IGNORE_FILTERS", "sdp").split(",")
    if f.strip()
)
# Image attachments (JPEG/PNG). Off by default: Model Armor's image scanning is only
# offered in some locations, and on the us-west1 template an image scan returns
# invocation FAILURE (verified live), which under MODEL_ARMOR_FAIL_CLOSED would put
# every image invoice into an AP-Error retry loop. Image attachments are then
# noted on the verdict and only the email text is scanned. Enable if your template
# location supports image scanning.
MODEL_ARMOR_SCAN_IMAGES: bool = os.getenv("MODEL_ARMOR_SCAN_IMAGES", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
MODEL_ARMOR_TIMEOUT_S: float = float(os.getenv("MODEL_ARMOR_TIMEOUT_S", "15"))
# Model Armor's file-scan cap is 4 MB; larger attachments can't be scanned whole.
MODEL_ARMOR_MAX_FILE_BYTES: int = int(os.getenv("MODEL_ARMOR_MAX_FILE_BYTES", str(4 * 1024 * 1024)))
# Longest text span sent in one call (subject + body, extracted fields, drafts).
MODEL_ARMOR_MAX_TEXT_CHARS: int = int(os.getenv("MODEL_ARMOR_MAX_TEXT_CHARS", "20000"))

# ---------------------------------------------------------------------------
# PII masking — what the screening/extraction LLMs read of the email
# ---------------------------------------------------------------------------
# Uses the DE-IDENTIFIED text Model Armor returns when its template has the
# advanced Sensitive Data Protection setting (a DLP inspect template + a DLP
# de-identify template). WHAT is masked, and how, is therefore configured in those
# DLP templates, not here. The masked text replaces the email text in the LLM
# request only; the UI, session state and reply address stay unmasked. Model Armor
# cannot de-identify files, so the attachment itself is NOT masked (ENV_SETUP.md).
# Needs MODEL_ARMOR_ENABLED. Off by default. If the template returns no
# de-identified text the scan is an error and MODEL_ARMOR_FAIL_CLOSED decides
# whether the email is held (AP-Error) or sent unmasked with a warning.
PII_MASKING_ENABLED: bool = os.getenv("PII_MASKING_ENABLED", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
