"""Deterministic FunctionNodes for the invoice-processing pipeline.

Every node in this package is pure Python (no LLM calls, except the large-invoice
line-item fallback in ``extraction``). They implement:
  - check_inbox           - gate: invoice email present?
  - inbox_empty           - terminal: no email in monitored window
  - quarantine_invoice    - terminal: Model Armor flagged the email at intake
  - screen_email_signal   - router: proceed vs discard from evidence policy (after screening_agent)
  - discard_non_invoice   - terminal: email is not an invoice
  - fetch_invoice_email   - AgentMail intake; attaches PDF for multimodal extraction
  - route_extraction_strategy - deterministic size/complexity router: PDF transport + model tier
  - emit_extraction_card  - renders extracted invoice as A2UI surface
  - classify_spend        - PO lookup (Firestore purchase_orders), before validation
  - validate_invoice      - AP rules + PO checks + routing (clean / review / duplicate)
  - human_approval_gate   - HITL pause; approve -> route_posting, reject -> vendor
  - route_posting         - both direct and indirect route to ERP (from the PO match)
  - post_to_erp           - stubbed ERP document creation (handles direct and indirect spend)
  - raise_duplicate_alert - stubbed ServiceNow ticket creation
  - pipeline_complete     - terminal: emit final chat summary

Formerly a single ``app/nodes.py`` module; split into concern modules for
readability. This ``__init__`` re-exports every node callable and the handful of
helpers/state that ``app/agent.py`` and the tests reference by name, so
``from app.nodes import <name>`` and ``app.nodes.<attr>`` keep working unchanged.
"""

# Re-exported so ``app.nodes.memory`` / ``app.nodes.agentmail_tools`` resolve to the
# real modules — the tests monkeypatch attributes on them (e.g.
# ``nodes.memory.ENABLE_MEMORY``, ``nodes.agentmail_tools.mark_processed``).
from app import agentmail_tools, memory
from app.nodes._state import _INVOICE_PDF, _MASKED_TEXT
from app.nodes.approval import human_approval_gate
from app.nodes.classification import classify_spend, route_posting
from app.nodes.extraction import emit_extraction_card, route_extraction_strategy
from app.nodes.intake import (
    check_inbox,
    fetch_invoice_email,
    inbox_empty,
    quarantine_invoice,
)
from app.nodes.memory_recall import recall_precedents
from app.nodes.posting import post_to_erp, raise_duplicate_alert
from app.nodes.rejection_email import (
    prepare_rejection_brief,
    review_email_draft,
    send_rejection_email,
)
from app.nodes.screening import discard_non_invoice, screen_email_signal
from app.nodes.summary import pipeline_complete
from app.nodes.validation import (
    _check_rules,
    _format_validation_summary,
    validate_invoice,
)

__all__ = [  # noqa: RUF022  (grouped by concern for readability, not sorted)
    # Modules re-exported for test monkeypatching
    "agentmail_tools",
    "memory",
    # Shared cross-node state
    "_INVOICE_PDF",
    "_MASKED_TEXT",
    # Node callables
    "check_inbox",
    "inbox_empty",
    "quarantine_invoice",
    "fetch_invoice_email",
    "screen_email_signal",
    "discard_non_invoice",
    "route_extraction_strategy",
    "emit_extraction_card",
    "validate_invoice",
    "recall_precedents",
    "human_approval_gate",
    "classify_spend",
    "route_posting",
    "post_to_erp",
    "raise_duplicate_alert",
    "prepare_rejection_brief",
    "review_email_draft",
    "send_rejection_email",
    "pipeline_complete",
    # Helpers referenced by tests
    "_check_rules",
    "_format_validation_summary",
]
