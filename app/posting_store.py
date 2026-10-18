"""Persists posted invoices to the Firestore ``posted_invoices`` collection.

This is what the "Post Invoice to ERP" step actually does: the ERP system is not
connected, so each posting is recorded as one document here instead (document id
= the posting's document number). A real integration would replace
``record_posting`` with the ERP API call and keep the same contract: return only
after the system has durably accepted the invoice, raise if it has not.

The write raises ``ConfigStoreError`` on any failure, so a node can never report
"posted" for an invoice that was not stored. Each document also carries a ``dedupe`` map
of normalized lookup keys: ``validate_invoice`` queries this collection
(``app/duplicate_check.py``) to catch an invoice that was already posted.
Documents hold the invoice header and PO details, not the line items (a very large invoice could exceed
Firestore's 1 MiB document limit) and never the PDF.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import POSTED_INVOICES_COLLECTION
from app.duplicate_check import dedupe_keys
from app.firestore_store import get_backend
from app.nodes._helpers import _safe_float

logger = logging.getLogger(__name__)


def _field(record: dict, key: str) -> str | None:
    return (record.get(key) or {}).get("value") or None


def build_posting_record(
    state: Any, *, system: str, document_id: str, posted_at: str
) -> dict[str, Any]:
    """The Firestore document for one posting, built from the pipeline state."""
    record = state.get("invoice_record") or {}
    po = state.get("po_match") or {}
    total = _field(record, "total")
    # The human gate only runs when validation routed to human_review; an
    # invoice that got here via "proceed" was straight-through.
    approval = (
        "human_approved" if state.get("approval_decision") else "straight_through"
    )
    return {
        "system": system,
        "document_id": document_id,
        "status": "posted",
        "posted_at": posted_at,
        "approval": approval,
        "invoice": {
            "vendor": _field(record, "vendor"),
            "invoice_no": _field(record, "invoice_no"),
            "po_ref": _field(record, "po_ref"),
            "total": total,
            "total_amount": _safe_float(total),
            "currency": _field(record, "currency"),
            "line_item_count": len(record.get("line_items") or []),
        },
        "po": {
            "po_number": po.get("po_number"),
            "po_type": po.get("po_type") or po.get("route"),
            "matched": po.get("matched"),
            "vendor_name": po.get("vendor_name"),
            "po_amount": po.get("po_amount"),
            "category": po.get("category"),
            "cost_center": po.get("cost_center"),
            "gl_account": po.get("gl_account"),
            "requisitioner": po.get("requisitioner"),
        },
        "dedupe": dedupe_keys(_field(record, "invoice_no"), _field(record, "po_ref")),
        "validation_flags": list(state.get("validation_flags") or []),
        "policy_version": (state.get("policy_snapshot") or {}).get("version"),
        "source": {
            "message_id": state.get("invoice_message_id"),
            "sender": state.get("invoice_sender_email"),
        },
    }


def record_posting(
    state: Any, *, system: str, document_id: str, posted_at: str
) -> tuple[str, int]:
    """Write the posting to Firestore. Returns ``(path, latency_ms)``.

    ``latency_ms`` is the wall time of the write — the posting step's equivalent
    of a model call's latency in the UI. Raises ``ConfigStoreError`` if the
    document could not be stored.
    """
    data = build_posting_record(
        state, system=system, document_id=document_id, posted_at=posted_at
    )
    t0 = time.perf_counter()
    get_backend().create(POSTED_INVOICES_COLLECTION, document_id, data)
    latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
    path = f"{POSTED_INVOICES_COLLECTION}/{document_id}"
    logger.info("record_posting: stored %s in %d ms", path, latency_ms)
    return path, latency_ms
