"""Duplicate-invoice lookups against the Firestore ``posted_invoices`` collection.

``posted_invoices`` is the ledger the SAP / Coupa posting steps write to
(``app/posting_store.py``), so it is the system of record for "already booked".
Each posting document carries a ``dedupe`` map with normalized lookup keys, which
this module queries with a single-field equality filter (served by Firestore's
automatic index; no composite index to deploy):

    dedupe.invoice_key   the invoice number, upper-cased, punctuation/space removed
    dedupe.po_key        the canonical PO number (``app.config._normalize_po``)

Normalizing both sides means "inv-2024-001", "INV 2024 001" and "INV2024001" are
the same invoice, which exact string matching would miss.

This module only fetches candidate postings. Whether a candidate is a genuine
duplicate (vendor match, same amount) is decided in ``app/nodes/validation.py``,
next to the other AP rules.

A failed read raises ``ConfigStoreError``. It must NOT be treated as "no
duplicate": the caller's error route handles it, so a Firestore outage holds the
invoice instead of letting a possible duplicate through to posting.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import POSTED_INVOICES_COLLECTION, _normalize_po
from app.firestore_store import Doc, get_backend

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")

# A single vendor+invoice number or PO realistically has a handful of postings.
_MAX_CANDIDATES = 25


def invoice_key(invoice_no: str | None) -> str:
    """Normalized invoice number used for duplicate matching ("" when absent)."""
    return _NON_ALNUM.sub("", (invoice_no or "").upper())


def po_key(po_ref: str | None) -> str:
    """Canonical PO number used for duplicate matching ("" when absent)."""
    return _normalize_po(po_ref)


def dedupe_keys(invoice_no: str | None, po_ref: str | None) -> dict[str, str]:
    """The ``dedupe`` map stored on every posting document."""
    return {"invoice_key": invoice_key(invoice_no), "po_key": po_key(po_ref)}


def _posted_only(docs: list[Doc]) -> list[Doc]:
    return [d for d in docs if (d.data.get("status") or "posted") == "posted"]


def find_posted_by_invoice(invoice_no: str | None) -> list[Doc]:
    """Postings already booked under this invoice number (any vendor)."""
    key = invoice_key(invoice_no)
    if not key:
        return []
    return _posted_only(
        get_backend().query_equal(
            POSTED_INVOICES_COLLECTION, "dedupe.invoice_key", key, _MAX_CANDIDATES
        )
    )


def find_posted_by_po(po_ref: str | None) -> list[Doc]:
    """Postings already booked against this PO."""
    key = po_key(po_ref)
    if not key:
        return []
    return _posted_only(
        get_backend().query_equal(
            POSTED_INVOICES_COLLECTION, "dedupe.po_key", key, _MAX_CANDIDATES
        )
    )


def history_document(entry: dict[str, Any]) -> dict[str, Any]:
    """Full ``posted_invoices`` document for a ``MOCK_POSTED_INVOICES`` entry.

    Same shape as ``posting_store.build_posting_record`` so seeded history and
    pipeline postings are queried identically.
    """
    total = entry.get("total")
    return {
        "system": entry["system"],
        "document_id": entry["document_id"],
        "status": "posted",
        "posted_at": entry.get("posted_at"),
        "approval": "straight_through",
        "invoice": {
            "vendor": entry.get("vendor"),
            "invoice_no": entry.get("invoice_no"),
            "po_ref": entry.get("po_ref"),
            "total": f"{total:,.2f}" if total is not None else None,
            "total_amount": total,
            "currency": entry.get("currency"),
            "line_item_count": 0,
        },
        "po": {"po_number": po_key(entry.get("po_ref")) or None},
        "dedupe": dedupe_keys(entry.get("invoice_no"), entry.get("po_ref")),
        "validation_flags": [],
        "policy_version": None,
        "source": {"message_id": None, "sender": None},
        "seeded": True,
    }
