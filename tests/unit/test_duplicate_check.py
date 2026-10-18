"""Duplicate detection against the Firestore ``posted_invoices`` ledger (in-memory backend).

The ledger is what post_to_erp writes to, so an invoice the pipeline
posts is duplicate-protected the next time it arrives. No hard-coded registry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import config
from app.duplicate_check import (
    dedupe_keys,
    find_posted_by_invoice,
    find_posted_by_po,
    history_document,
    invoice_key,
)
from app.firestore_store import (
    ConfigStoreError,
    FailingBackend,
    InMemoryBackend,
    set_backend,
)
from app.nodes import _check_rules, post_to_erp, validate_invoice
from app.nodes.validation import check_duplicates
from app.schemas import InvoiceRecord

COLL = config.POSTED_INVOICES_COLLECTION


def _fv(value, grounding="grounded"):
    return {"value": value, "source_grounding": grounding}


def _record(*, vendor="Acme Industrial Inc", invoice_no="ACME-7001", po="PO-2026-1003", total="1500.00", currency="USD"):
    return {
        "vendor": _fv(vendor),
        "invoice_no": _fv(invoice_no),
        "po_ref": _fv(po),
        "total": _fv(total),
        "currency": _fv(currency),
        "line_items": [],
    }


def _entry(doc_id="SAP-TEST-0001", **overrides):
    entry = {
        "document_id": doc_id,
        "system": "SAP_CIMS",
        "vendor": "Acme Industrial Inc",
        "invoice_no": "ACME-7001",
        "po_ref": "PO-2026-1003",
        "total": 1500.0,
        "currency": "USD",
        "posted_at": "2026-06-01T00:00:00+00:00",
    }
    entry.update(overrides)
    return entry


def _posted(backend, doc_id="SAP-TEST-0001", **overrides):
    backend.set(COLL, doc_id, history_document(_entry(doc_id, **overrides)))


def _dup(rec: dict) -> str | None:
    return check_duplicates(InvoiceRecord(**rec))


# ---------------------------------------------------------------------------
# Keys and lookups
# ---------------------------------------------------------------------------


def test_invoice_key_ignores_case_spacing_and_punctuation():
    assert invoice_key("inv-2024-001") == invoice_key("INV 2024 001") == invoice_key("INV2024001")
    assert invoice_key(None) == invoice_key("  ") == ""


def test_dedupe_keys_normalize_both_identifiers():
    assert dedupe_keys("inv 1", "po 2026 1003") == {"invoice_key": "INV1", "po_key": "PO-2026-1003"}


def test_lookups_find_only_posted_documents(firestore_backend):
    _posted(firestore_backend, "SAP-A")
    firestore_backend.set(COLL, "SAP-B", {**history_document(_entry("SAP-B")), "status": "reversed"})
    assert [d.id for d in find_posted_by_invoice("acme 7001")] == ["SAP-A"]
    assert [d.id for d in find_posted_by_po("PO 2026 1003")] == ["SAP-A"]
    assert find_posted_by_invoice("") == [] and find_posted_by_po(None) == []


def test_history_document_matches_the_posting_shape():
    doc = history_document(_entry("X-1", invoice_no="I-1", po_ref="po 2026 2001", total=1234.5))
    assert doc["status"] == "posted" and doc["invoice"]["total_amount"] == 1234.5
    assert doc["invoice"]["total"] == "1,234.50"
    assert doc["dedupe"] == {"invoice_key": "I1", "po_key": "PO-2026-2001"}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_empty_ledger_means_no_duplicate(firestore_backend):
    assert _dup(_record()) is None
    assert _check_rules(InvoiceRecord(**_record()))[0] == "proceed"


def test_same_vendor_and_invoice_number_is_a_duplicate(firestore_backend):
    _posted(firestore_backend)
    flag = _dup(_record(invoice_no="acme 7001"))  # re-typed differently
    assert flag and flag.startswith("Duplicate invoice number detected") and "SAP-TEST-0001" in flag
    route, flags = _check_rules(InvoiceRecord(**_record()))
    assert route == "duplicate_po" and len(flags) == 1


def test_same_invoice_number_from_a_different_vendor_is_not_a_duplicate(firestore_backend):
    _posted(firestore_backend)
    assert _dup(_record(vendor="Totally Different Supplies Ltd", po="PO-2026-2002", total="10.00")) is None


def test_vendor_name_variants_still_match(firestore_backend):
    _posted(firestore_backend, vendor="Acme Industrial Inc")
    assert _dup(_record(vendor="ACME Industrial, Incorporated")) is not None


def test_missing_vendor_on_the_ledger_side_errs_towards_duplicate(firestore_backend):
    _posted(firestore_backend, vendor=None)
    assert _dup(_record()) is not None


def test_same_po_vendor_and_amount_under_a_new_invoice_number_is_a_duplicate(firestore_backend):
    _posted(firestore_backend)
    flag = _dup(_record(invoice_no="ACME-7002"))
    assert flag and flag.startswith("PO number already invoiced (duplicate)")


def test_partial_billing_against_the_same_po_is_not_a_duplicate(firestore_backend):
    _posted(firestore_backend)
    assert _dup(_record(invoice_no="ACME-7002", total="500.00")) is None


def test_same_po_and_amount_in_a_different_currency_is_not_a_po_duplicate(firestore_backend):
    _posted(firestore_backend)
    assert _dup(_record(invoice_no="ACME-7002", currency="EUR")) is None


def test_an_invoice_the_pipeline_just_posted_is_caught_next_time(firestore_backend):
    """The gap this replaces: the same invoice arriving twice used to be posted twice."""
    state = {
        "invoice_record": _record(),
        "po_match": {"po_number": "PO-2026-1003", "po_type": "direct", "route": "direct", "matched": True},
        "validation_flags": [],
        "policy_snapshot": {"version": "v"},
    }
    assert _check_rules(InvoiceRecord(**_record()))[0] == "proceed"  # first arrival
    assert post_to_erp._func(SimpleNamespace(state=state), None).actions.route == "done"

    ctx = SimpleNamespace(state={"invoice_record": _record(), "po_match": None}, resume_inputs={})
    second = validate_invoice._func(ctx, _record())  # second arrival
    assert second.actions.route == "duplicate_po"
    assert second.actions.state_delta["validation_flags"][0].startswith("Duplicate invoice number")


# ---------------------------------------------------------------------------
# Outage handling
# ---------------------------------------------------------------------------


def test_firestore_outage_errors_instead_of_letting_a_duplicate_through():
    set_backend(FailingBackend())
    with pytest.raises(ConfigStoreError):
        _dup(_record())
    ctx = SimpleNamespace(state={"invoice_record": _record(), "po_match": None}, resume_inputs={})
    event = validate_invoice._func(ctx, _record())
    assert event.actions.route == "error"
    assert event.actions.state_delta["pipeline_error_node"] == "validate_invoice"


# ---------------------------------------------------------------------------
# Backend query
# ---------------------------------------------------------------------------


def test_in_memory_query_equal_supports_dotted_paths_and_limit():
    backend = InMemoryBackend({"c": {f"d{i}": {"a": {"b": "x" if i < 3 else "y"}} for i in range(5)}})
    assert {d.id for d in backend.query_equal("c", "a.b", "x")} == {"d0", "d1", "d2"}
    assert len(backend.query_equal("c", "a.b", "x", limit=2)) == 2
    assert backend.query_equal("c", "a.b", "nope") == []
    assert backend.query_equal("missing", "a.b", "x") == []
