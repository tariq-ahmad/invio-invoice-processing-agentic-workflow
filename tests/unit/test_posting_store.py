"""ERP posting step persists the invoice to Firestore and reports latency."""

from __future__ import annotations

from types import SimpleNamespace

from app import config
from app.firestore_store import FailingBackend, set_backend
from app.nodes import post_to_erp

COLL = config.POSTED_INVOICES_COLLECTION


def _fv(value, grounding="grounded"):
    return {"value": value, "source_grounding": grounding}


def _state(**extra):
    state = {
        "invoice_record": {
            "vendor": _fv("BluePeak Plastics"),
            "invoice_no": _fv("INV-2026-0301"),
            "po_ref": _fv("PO-2026-1003"),
            "total": _fv("3,250.00"),
            "currency": _fv("USD"),
            "line_items": [{"name": "Resin", "amount": "3250.00"}],
        },
        "po_match": {
            "po_number": "PO-2026-1003",
            "po_type": "direct",
            "route": "direct",
            "matched": True,
            "vendor_name": "BluePeak Plastics",
            "po_amount": 3250.0,
            "category": "Raw Materials",
            "cost_center": "CC-PROD-1100",
            "gl_account": "500100",
            "requisitioner": "A. Whitfield",
        },
        "validation_flags": [],
        "policy_snapshot": {"version": "ap_rules@x;fx_rates@y"},
        "invoice_message_id": "<msg-1@example>",
        "invoice_sender_email": "ap@bluepeak.example",
    }
    state.update(extra)
    return state


def _stored(backend, doc_id):
    doc = backend.get(COLL, doc_id)
    assert doc is not None, f"{COLL}/{doc_id} was not written"
    return doc.data


def test_post_to_erp_writes_a_firestore_document(firestore_backend):
    ev = post_to_erp._func(SimpleNamespace(state=_state()), None)
    result = ev.actions.state_delta["posting_result"]
    assert ev.actions.route == "done"
    assert ev.actions.state_delta["pipeline_status"] == "posted_erp"

    doc = _stored(firestore_backend, result["document_id"])
    assert result["document_id"].startswith("ERP-")
    assert result["store_path"] == f"{COLL}/{result['document_id']}"
    assert doc["system"] == "ERP"
    assert doc["status"] == "posted"
    assert doc["posted_at"] == result["posted_at"]
    assert doc["invoice"] == {
        "vendor": "BluePeak Plastics",
        "invoice_no": "INV-2026-0301",
        "po_ref": "PO-2026-1003",
        "total": "3,250.00",
        "total_amount": 3250.0,
        "currency": "USD",
        "line_item_count": 1,
    }
    assert doc["dedupe"] == {"invoice_key": "INV20260301", "po_key": "PO-2026-1003"}
    assert doc["po"]["po_number"] == "PO-2026-1003"
    assert doc["po"]["po_type"] == "direct"
    assert doc["po"]["cost_center"] == "CC-PROD-1100"
    assert doc["policy_version"] == "ap_rules@x;fx_rates@y"
    assert doc["source"]["message_id"] == "<msg-1@example>"


def test_post_to_erp_indirect_writes_a_firestore_document(firestore_backend):
    state = _state()
    state["po_match"].update(po_number="PO-2026-2002", po_type="indirect", route="indirect")
    ev = post_to_erp._func(SimpleNamespace(state=state), None)
    result = ev.actions.state_delta["posting_result"]
    assert ev.actions.state_delta["pipeline_status"] == "posted_erp"

    doc = _stored(firestore_backend, result["document_id"])
    assert result["document_id"].startswith("ERP-")
    assert doc["system"] == "ERP"
    assert doc["po"]["po_type"] == "indirect"


def test_posting_reports_latency_in_state_and_result():
    ev = post_to_erp._func(SimpleNamespace(state=_state()), None)
    latency = ev.actions.state_delta["posting_latency_ms"]
    assert isinstance(latency, int) and latency >= 1
    assert ev.actions.state_delta["posting_result"]["latency_ms"] == latency


def test_approval_path_is_recorded(firestore_backend):
    auto = post_to_erp._func(SimpleNamespace(state=_state()), None)
    approved = post_to_erp._func(SimpleNamespace(state=_state(approval_decision="approve")), None)
    auto_id = auto.actions.state_delta["posting_result"]["document_id"]
    approved_id = approved.actions.state_delta["posting_result"]["document_id"]
    assert _stored(firestore_backend, auto_id)["approval"] == "straight_through"
    assert _stored(firestore_backend, approved_id)["approval"] == "human_approved"


def test_each_posting_gets_its_own_document(firestore_backend):
    ids = {
        post_to_erp._func(SimpleNamespace(state=_state()), None)
        .actions.state_delta["posting_result"]["document_id"]
        for _ in range(3)
    }
    assert len(ids) == 3
    for doc_id in ids:
        _stored(firestore_backend, doc_id)


def test_line_items_are_counted_not_stored(firestore_backend):
    state = _state()
    state["invoice_record"]["line_items"] = [{"name": f"item {i}", "amount": "1.00"} for i in range(500)]
    ev = post_to_erp._func(SimpleNamespace(state=state), None)
    doc = _stored(firestore_backend, ev.actions.state_delta["posting_result"]["document_id"])
    assert doc["invoice"]["line_item_count"] == 500
    assert "line_items" not in doc["invoice"]


def test_failed_write_is_an_error_not_a_posting():
    set_backend(FailingBackend())
    ev = post_to_erp._func(SimpleNamespace(state=_state()), None)
    assert ev.actions.route == "error"
    assert "posting_result" not in ev.actions.state_delta
    assert "pipeline_status" not in ev.actions.state_delta
    assert ev.actions.state_delta["pipeline_error_node"] == "post_to_erp"
