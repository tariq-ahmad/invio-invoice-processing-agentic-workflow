"""Firestore-backed AP policies and PO lookup (in-memory backend, no GCP).

Covers: defaults when documents are absent, live values flowing into the rule
engine, validation / last-known-good fallback, the found / not-found / outage
distinction for PO lookup, and classify_spend's behaviour on each.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import config
from app.firestore_store import (
    ConfigStoreError,
    FailingBackend,
    set_backend,
    valid_doc_id,
)
from app.nodes import _check_rules, _format_validation_summary, classify_spend
from app.nodes.screening import route_screening_assessment
from app.po_store import lookup_po
from app.policy import (
    AP_RULES_DOC,
    FX_RATES_DOC,
    ApRules,
    default_policy,
    get_policy,
    reset_policy_cache,
)
from app.schemas import InvoiceRecord


def _fv(value, grounding="grounded"):
    return {"value": value, "source_grounding": grounding}


def _record(*, invoice_no="INV-X", po="PO-2026-1003", total="100.00", currency="USD", **extra):
    rec = {
        "vendor": _fv("Vendor"),
        "invoice_no": _fv(invoice_no),
        "po_ref": _fv(po),
        "total": _fv(total),
        "currency": _fv(currency),
        "line_items": [],
    }
    rec.update(extra)
    return rec


def _set_rules(backend, **fields):
    backend.set(config.POLICIES_COLLECTION, AP_RULES_DOC, fields)
    reset_policy_cache()


# ---------------------------------------------------------------------------
# Policy defaults and live values
# ---------------------------------------------------------------------------


def test_policy_defaults_match_in_code_constants():
    p = get_policy()
    assert p.hitl_threshold_usd == config.PO_AMOUNT_HITL_THRESHOLD_USD == 4_000.0
    assert p.reconcile_tolerance_pct == config.RECONCILE_TOLERANCE_PCT
    assert p.required_fields == tuple(config.REQUIRED_FIELDS)
    assert p.screening_min_strong_signals == 2
    assert p.unmatched_po_route == "direct"
    assert p.fx_rates == config.FX_TO_USD
    assert p.version == "ap_rules@default;fx_rates@default"


def test_policy_reads_firestore_document(firestore_backend):
    _set_rules(firestore_backend, hitl_threshold_usd=10_000, updated_by="someone")
    p = get_policy()
    assert p.hitl_threshold_usd == 10_000
    assert p.reconcile_tolerance_pct == config.RECONCILE_TOLERANCE_PCT  # untouched -> default
    assert p.version.startswith("ap_rules@in-memory")


def test_policy_is_cached_until_reset(firestore_backend):
    assert get_policy().hitl_threshold_usd == 4_000.0
    firestore_backend.set(config.POLICIES_COLLECTION, AP_RULES_DOC, {"hitl_threshold_usd": 9_999})
    assert get_policy().hitl_threshold_usd == 4_000.0  # still inside the TTL
    reset_policy_cache()
    assert get_policy().hitl_threshold_usd == 9_999


def test_policy_refreshes_after_ttl(firestore_backend, monkeypatch):
    monkeypatch.setattr(config, "POLICY_CACHE_TTL_SECONDS", 0.0)
    reset_policy_cache()
    assert get_policy().hitl_threshold_usd == 4_000.0
    firestore_backend.set(config.POLICIES_COLLECTION, AP_RULES_DOC, {"hitl_threshold_usd": 7_500})
    assert get_policy().hitl_threshold_usd == 7_500


@pytest.mark.parametrize(
    "bad",
    [
        {"hitl_threshold_usd": 0},
        {"hitl_threshold_usd": -5},
        {"reconcile_tolerance_pct": 2},
        {"required_fields": []},
        {"required_fields": ["vendor", "not_a_field"]},
        {"screening_min_strong_signals": 0},
        {"unmatched_po_route": "somewhere"},
    ],
)
def test_invalid_document_is_rejected(bad):
    with pytest.raises(ValueError):
        ApRules(**bad)


def test_invalid_document_falls_back_to_defaults(firestore_backend):
    _set_rules(firestore_backend, hitl_threshold_usd=-1)
    assert get_policy().hitl_threshold_usd == 4_000.0


def test_invalid_edit_keeps_last_known_good(firestore_backend, monkeypatch):
    monkeypatch.setattr(config, "POLICY_CACHE_TTL_SECONDS", 0.0)
    _set_rules(firestore_backend, hitl_threshold_usd=8_000)
    assert get_policy().hitl_threshold_usd == 8_000
    firestore_backend.set(config.POLICIES_COLLECTION, AP_RULES_DOC, {"hitl_threshold_usd": "oops"})
    assert get_policy().hitl_threshold_usd == 8_000  # bad edit ignored


def test_firestore_outage_keeps_last_known_good(firestore_backend, monkeypatch):
    monkeypatch.setattr(config, "POLICY_CACHE_TTL_SECONDS", 0.0)
    _set_rules(firestore_backend, hitl_threshold_usd=8_000)
    assert get_policy().hitl_threshold_usd == 8_000
    set_backend(FailingBackend())
    assert get_policy().hitl_threshold_usd == 8_000


def test_firestore_outage_on_first_load_uses_defaults():
    set_backend(FailingBackend())
    reset_policy_cache()
    p = get_policy()  # must not raise
    assert p.hitl_threshold_usd == 4_000.0


def test_fx_table_from_firestore(firestore_backend):
    firestore_backend.set(
        config.POLICIES_COLLECTION, FX_RATES_DOC, {"rates": {"eur": 2.0}, "default_rate": 0.5}
    )
    reset_policy_cache()
    p = get_policy()
    assert p.to_usd(100, "EUR") == 200.0
    assert p.to_usd(100, "XXX") == 50.0  # unknown currency -> default_rate


def test_default_policy_helper_matches_cold_start():
    assert default_policy().hitl_threshold_usd == 4_000.0


# ---------------------------------------------------------------------------
# Policies drive the rule engine
# ---------------------------------------------------------------------------


def test_threshold_change_flips_the_route(firestore_backend):
    rec = InvoiceRecord(**_record(total="4750.00"))
    assert _check_rules(rec)[0] == "human_review"  # default limit $4,000
    _set_rules(firestore_backend, hitl_threshold_usd=10_000)
    assert _check_rules(rec) == ("proceed", [])


def test_fx_rates_apply_to_the_threshold(firestore_backend):
    rec = InvoiceRecord(**_record(total="3500.00", currency="EUR"))  # 3500 * 1.08 = 3780 USD
    assert _check_rules(rec)[0] == "proceed"
    firestore_backend.set(config.POLICIES_COLLECTION, FX_RATES_DOC, {"rates": {"EUR": 2.0}})
    reset_policy_cache()
    assert _check_rules(rec)[0] == "human_review"  # 7000 USD


def test_required_fields_policy(firestore_backend):
    rec = InvoiceRecord(**_record(po=None))
    rec.po_ref.value = None
    assert any("po_ref" in f for f in _check_rules(rec)[1])
    _set_rules(firestore_backend, required_fields=["vendor", "invoice_no", "total"])
    assert not any("po_ref" in f for f in _check_rules(rec)[1])


def test_reconcile_tolerance_policy(firestore_backend):
    rec = InvoiceRecord(
        **_record(total="1000.00", line_items=[{"name": "x", "amount": "980.00"}])
    )
    assert any("do not reconcile" in f for f in _check_rules(rec)[1])  # 2% off, default 1%
    _set_rules(firestore_backend, reconcile_tolerance_pct=0.05)
    assert not any("do not reconcile" in f for f in _check_rules(rec)[1])


def test_policy_check_label_shows_live_threshold(firestore_backend):
    _set_rules(firestore_backend, hitl_threshold_usd=12_500)
    rec = _record(total="100.00")
    route, flags = _check_rules(InvoiceRecord(**rec))
    summary = _format_validation_summary(InvoiceRecord(**rec), route, flags)
    assert "Within auto-approval limit (≤ $12,500)" in summary


def test_screening_min_strong_signals_policy(firestore_backend):
    one_strong = {"attachment_signal": "strong", "body_signal": "weak", "subject_signal": "absent"}
    two_strong = {"attachment_signal": "strong", "body_signal": "strong", "subject_signal": "absent"}
    assert route_screening_assessment(one_strong) == "not_invoice"
    assert route_screening_assessment(two_strong) == "invoice"
    _set_rules(firestore_backend, screening_min_strong_signals=1)
    assert route_screening_assessment(one_strong) == "invoice"
    _set_rules(firestore_backend, screening_min_strong_signals=3)
    assert route_screening_assessment(two_strong) == "not_invoice"


# ---------------------------------------------------------------------------
# PO lookup: found / not found / outage
# ---------------------------------------------------------------------------


def test_lookup_reads_from_the_store_not_the_mock_dict(firestore_backend):
    firestore_backend.set(
        config.PURCHASE_ORDERS_COLLECTION,
        "PO-2026-7777",
        {"type": "indirect", "vendor": "New Vendor", "amount": 10.0},
    )
    match = lookup_po("po 2026 7777")
    assert match["po_number"] == "PO-2026-7777"
    assert match["type"] == "indirect"
    assert "PO-2026-7777" not in config.MOCK_PO_DATABASE


def test_lookup_edit_changes_spend_type(firestore_backend):
    assert lookup_po("PO-2026-1003")["type"] == "direct"
    firestore_backend.set(
        config.PURCHASE_ORDERS_COLLECTION, "PO-2026-1003", {"type": "indirect"}
    )
    assert lookup_po("PO-2026-1003")["type"] == "indirect"


def test_lookup_not_found_returns_none():
    assert lookup_po("PO-0000-0000") is None


@pytest.mark.parametrize("ref", ["PO/2026/1003", "..", "__x__", "   ", None, ""])
def test_lookup_unusable_reference_is_not_found_not_an_error(ref):
    assert lookup_po(ref) is None


def test_lookup_malformed_document_raises(firestore_backend):
    firestore_backend.set(
        config.PURCHASE_ORDERS_COLLECTION, "PO-2026-1003", {"type": "bogus"}
    )
    with pytest.raises(ConfigStoreError):
        lookup_po("PO-2026-1003")


def test_lookup_outage_raises_instead_of_reporting_unmatched():
    set_backend(FailingBackend())
    with pytest.raises(ConfigStoreError):
        lookup_po("PO-2026-1003")


def test_valid_doc_id():
    assert valid_doc_id("PO-2026-1003")
    assert not valid_doc_id("a/b")
    assert not valid_doc_id("")
    assert not valid_doc_id("__reserved__")


# ---------------------------------------------------------------------------
# classify_spend on each lookup outcome
# ---------------------------------------------------------------------------


def _ctx(po):
    return SimpleNamespace(state={"invoice_record": _record(po=po)})


def test_classify_spend_matched_uses_firestore_type(firestore_backend):
    firestore_backend.set(
        config.PURCHASE_ORDERS_COLLECTION, "PO-2026-1003", {"type": "indirect", "amount": 1.0}
    )
    ev = classify_spend._func(_ctx("PO-2026-1003"), None)
    assert ev.actions.route == "classified"
    assert ev.actions.state_delta["po_match"]["route"] == "indirect"
    assert ev.actions.state_delta["po_match"]["target_system"] == "ERP"


def test_classify_spend_unmatched_follows_policy_route(firestore_backend):
    ev = classify_spend._func(_ctx("PO-9999-0000"), None)
    assert ev.actions.state_delta["po_match"]["route"] == "direct"
    _set_rules(firestore_backend, unmatched_po_route="indirect")
    ev = classify_spend._func(_ctx("PO-9999-0000"), None)
    assert ev.actions.route == "classified"
    assert ev.actions.state_delta["po_match"]["route"] == "indirect"
    assert ev.actions.state_delta["po_match"]["matched"] is False
    assert ev.actions.state_delta["po_match"]["target_system"] == "ERP"


def test_classify_spend_invoice_without_po_is_unmatched_not_error(firestore_backend):
    # Extraction stores a missing PO as {"value": None, "source_grounding": "not_found"}.
    ctx = SimpleNamespace(
        state={"invoice_record": {"po_ref": {"value": None, "source_grounding": "not_found"}}}
    )
    ev = classify_spend._func(ctx, None)
    assert ev.actions.route == "classified"
    assert ev.actions.state_delta["po_match"]["matched"] is False
    assert ev.actions.state_delta["po_match"]["po_number"] is None


def test_classify_spend_outage_routes_to_error_not_unmatched():
    set_backend(FailingBackend())
    ev = classify_spend._func(_ctx("PO-2026-1003"), None)
    assert ev.actions.route == "error"
    assert "po_match" not in ev.actions.state_delta
    assert ev.actions.state_delta["pipeline_error_node"] == "classify_spend"


# ---------------------------------------------------------------------------
# Policy-driven labels: approval message and the snapshot the UI reads
# ---------------------------------------------------------------------------


def _approval_record(**grounding):
    keys = ("vendor", "invoice_no", "po_ref", "total", "currency")
    return {k: _fv("x", grounding.get(k, "grounded")) for k in keys}


def test_approval_message_counts_required_fields_not_a_fixed_four():
    from app.nodes.approval import _format_approval_message

    rec = _approval_record(po_ref="inferred")
    # default policy: 4 required fields, po_ref not grounded -> 3/4
    assert "| 3/4 |" in _format_approval_message(rec, ["flag"])
    # an explicit (decision-time) required list wins over the live policy
    assert "| 2/3 |" in _format_approval_message(rec, ["flag"], None, ["vendor", "po_ref", "total"])


def test_approval_message_follows_firestore_required_fields(firestore_backend):
    from app.nodes.approval import _format_approval_message

    _set_rules(firestore_backend, required_fields=["vendor", "invoice_no", "total"])
    msg = _format_approval_message(_approval_record(po_ref="not_found"), ["flag"])
    assert "Required fields grounded in the document | 3/3 |" in msg


def test_policy_audit_carries_everything_the_ui_labels(firestore_backend):
    _set_rules(firestore_backend, screening_min_strong_signals=3, required_fields=["vendor", "total"])
    audit = get_policy().audit()
    assert audit["screening_min_strong_signals"] == 3
    assert audit["required_fields"] == ["vendor", "total"]
    assert {"hitl_threshold_usd", "min_invoice_no_length", "unmatched_po_route"} <= set(audit)


def test_intake_stamps_the_policy_snapshot(firestore_backend, monkeypatch):
    from app import agentmail_tools
    from app.nodes import fetch_invoice_email

    invoice = {
        "subject": "Invoice", "sender": "v@example.com", "date": "", "body_text": "see attached",
        "attachment_bytes": None, "attachment_filename": "", "attachment_mime": "", "pdf_text": "",
    }
    monkeypatch.setattr(agentmail_tools, "search_latest_invoice", lambda: "msg-1")
    monkeypatch.setattr(agentmail_tools, "fetch_invoice", lambda _id: invoice)
    monkeypatch.setattr(agentmail_tools, "label_in_progress", lambda _id: True)

    _set_rules(firestore_backend, screening_min_strong_signals=3)
    ctx = SimpleNamespace(invocation_id="inv-1", state={})
    events = list(fetch_invoice_email._func(ctx, None))
    snapshot = events[-1].actions.state_delta["policy_snapshot"]
    assert snapshot["screening_min_strong_signals"] == 3
    assert snapshot["required_fields"] == list(config.REQUIRED_FIELDS)


def test_classify_spend_reports_lookup_latency_and_source():
    ev = classify_spend._func(_ctx("PO-2026-1003"), None)
    delta = ev.actions.state_delta
    latency = delta["classify_latency_ms"]
    assert isinstance(latency, int) and latency >= 1
    assert delta["po_match"]["latency_ms"] == latency
    assert delta["po_match"]["store_path"] == f"{config.PURCHASE_ORDERS_COLLECTION}/PO-2026-1003"


def test_classify_spend_unmatched_also_reports_latency():
    ev = classify_spend._func(_ctx("PO-9999-0000"), None)
    delta = ev.actions.state_delta
    assert delta["classify_latency_ms"] >= 1
    assert delta["po_match"]["matched"] is False
    assert delta["po_match"]["store_path"] is None
