"""PO master-data lookup backed by the Firestore ``purchase_orders`` collection.

Replaces the in-code ``MOCK_PO_DATABASE`` dict at runtime. One document per PO;
the document id is the canonical PO number (e.g. ``PO-2026-1003``) and the
fields mirror what an AP clerk sees on a PO header:

    type            "direct" -> SAP CIMS | "indirect" -> Coupa AP
    vendor, amount, currency, category, cost_center, gl_account,
    requisitioner, status, created

``lookup_po`` keeps the old contract — an enriched record dict, or ``None`` when
the PO is genuinely not on file — and adds one new outcome: a ``ConfigStoreError``
when Firestore cannot be read. That MUST NOT be treated as "unmatched": an
unmatched PO routes to a default spend system, so swallowing an outage would
silently misroute an indirect PO to SAP. The caller's error route handles it.
"""

from __future__ import annotations

import logging

from app.config import PURCHASE_ORDERS_COLLECTION, _normalize_po
from app.firestore_store import ConfigStoreError, get_backend

logger = logging.getLogger(__name__)

VALID_PO_TYPES = ("direct", "indirect")


def lookup_po(po_ref: str | None) -> dict | None:
    """Look up a PO by (possibly messy) reference, tolerant of formatting.

    Returns the PO record with the normalized key added under ``po_number``, or
    ``None`` when no such PO exists. Raises ``ConfigStoreError`` when Firestore
    cannot be read or the stored document is malformed.
    """
    if not po_ref or not po_ref.strip():
        return None
    key = _normalize_po(po_ref)
    # Canonical key first; the raw reference second (an exact-id hit on a PO that
    # was stored un-normalized).
    candidates = [key] if key else []
    raw = po_ref.strip()
    if raw not in candidates:
        candidates.append(raw)

    backend = get_backend()
    for doc_id in candidates:
        doc = backend.get(PURCHASE_ORDERS_COLLECTION, doc_id)
        if doc is None:
            continue
        record = dict(doc.data)
        po_type = record.get("type")
        if po_type not in VALID_PO_TYPES:
            raise ConfigStoreError(
                f"{PURCHASE_ORDERS_COLLECTION}/{doc_id} has invalid type {po_type!r}; "
                f"expected one of {list(VALID_PO_TYPES)}"
            )
        return {**record, "po_number": key or doc_id}
    return None
