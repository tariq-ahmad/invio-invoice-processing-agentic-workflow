"""Rule-engine tests for the demo / test invoice scenarios.

These assert the *deterministic* behaviour behind each fixture in
``tests/fixtures/invoices/`` — the AP business rules (``_check_rules``), the
PO lookup / spend classification (``lookup_po`` + ``classify_spend``), and the
shape of the expanded mock master data in ``app/config.py``. They do NOT call
the LLM, so they're fast and stable; the live extraction outcome for each PDF
should land on the same route.

Keep this in sync with ``tests/fixtures/invoices/README.md``.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app import config
from app.config import (
    MOCK_PO_DATABASE,
    MOCK_POSTED_INVOICES,
    PO_AMOUNT_HITL_THRESHOLD_USD,
)
from app.nodes import (
    _check_rules,
    _format_validation_summary,
    classify_spend,
    prepare_rejection_brief,
)
from app.po_store import lookup_po
from app.schemas import InvoiceRecord

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PO_KEYS = {
    "type", "vendor", "amount", "currency", "category",
    "cost_center", "gl_account", "requisitioner", "status", "created",
}


def _fv(value, grounding="grounded"):
    return {"value": value, "source_grounding": grounding}


def _record(
    *, vendor="Vendor", invoice_no="INV-X", po="PO-X", total="100.00",
    currency="USD", grounding="grounded", line_items=None,
) -> dict:
    """A FieldValue-shaped InvoiceRecord dict mirroring an extracted invoice."""
    return {
        "vendor": _fv(vendor, grounding),
        "invoice_no": _fv(invoice_no, grounding),
        "po_ref": _fv(po, grounding) if po is not None else _fv(None, "not_found"),
        "total": _fv(total, grounding),
        "currency": _fv(currency, grounding),
        "line_items": line_items if line_items is not None else [],
    }


def _route(rec_dict: dict) -> tuple[str, list[str]]:
    return _check_rules(InvoiceRecord(**rec_dict))


# ---------------------------------------------------------------------------
# Per-scenario routing (mirrors the fixtures + README)
# ---------------------------------------------------------------------------


def test_s1_direct_straight_through():
    route, flags = _route(_record(invoice_no="INV-2026-0301", po="PO-2026-1003", total="3250.00"))
    assert route == "proceed"
    assert flags == []
    assert lookup_po("PO-2026-1003")["type"] == "direct"


def test_s2_indirect_straight_through():
    route, flags = _route(_record(invoice_no="INV-2026-0302", po="PO-2026-2002", total="920.00"))
    assert route == "proceed"
    assert flags == []
    assert lookup_po("PO-2026-2002")["type"] == "indirect"


def test_s3_high_value_needs_approval():
    route, flags = _route(_record(invoice_no="INV-2026-0303", po="PO-2026-1010", total="12500.00"))
    assert route == "human_review"
    assert any("auto-approval limit" in f.lower() for f in flags)
    assert lookup_po("PO-2026-1010")["type"] == "direct"  # approve -> SAP


def test_s4_duplicate_invoice_number_escalates():
    route, flags = _route(
        _record(vendor="Cascade Components Inc", invoice_no="INV-2024-001", po="PO-2026-1002", total="5400.00")
    )
    assert route == "duplicate_po"
    assert any("duplicate" in f.lower() for f in flags)


def test_s5_duplicate_po_escalates():
    route, flags = _route(
        _record(vendor="Globex Trading Co", invoice_no="INV-2026-0305", po="PO-2026-0098", total="6000.00")
    )
    assert route == "duplicate_po"
    assert any("duplicate" in f.lower() or "already" in f.lower() for f in flags)


def test_s6_missing_po_needs_review():
    route, flags = _route(_record(invoice_no="2026/887-A", po=None, total="2750.00"))
    assert route == "human_review"
    assert any("po_ref" in f.lower() or "required field" in f.lower() for f in flags)


def test_s7_ungrounded_field_needs_review():
    """A blurry/illegible scan surfaces as a required field the model could not
    ground in the document — routed to review, without any self-graded score."""
    route, flags = _route(
        _record(invoice_no="INV-2026-0307", po="PO-2026-2007", total="4750.00", grounding="inferred")
    )
    assert route == "human_review"
    assert any("not grounded" in f.lower() for f in flags)


def test_s8_zero_total_is_invalid():
    route, flags = _route(_record(invoice_no="INV-2026-0308", po="PO-2026-2011", total="0.00"))
    assert route == "human_review"
    assert any("invalid total" in f.lower() for f in flags)


def test_policy_check_marks_over_limit_as_flag():
    """Regression: a total over the auto-approval limit must render as Flag in the
    Policy Checks table, not a false Pass (the flag needle must match the wording)."""
    rec = _record(invoice_no="INV-2026-0309", po="PO-2026-2007", total="4750.00")
    route, flags = _route(rec)
    summary = _format_validation_summary(InvoiceRecord(**rec), route, flags)
    row = next(line for line in summary.splitlines() if "Within auto-approval limit" in line)
    assert row.rstrip().endswith("| Flag |")  # result in the right-hand column
    assert "Pass" not in row


def test_policy_check_flags_ungrounded_field_independently():
    """Regression: total-reconciliation and field-grounding failures must render
    as two independent Policy Check rows, not a single ambiguous row.

    Line items reconcile with the total; only po_ref is ungrounded — the
    reconciliation row must stay Pass while the grounding row flags."""
    rec = _record(
        invoice_no="INV-2026-0310", po="PO-2026-2007", total="2000.00",
        line_items=[{"name": "Service", "amount": "2000.00"}],
    )
    rec["po_ref"] = _fv("PO-2026-2007", "inferred")
    route, flags = _route(rec)
    summary = _format_validation_summary(InvoiceRecord(**rec), route, flags)

    reconcile_row = next(line for line in summary.splitlines() if "Line items reconcile to total" in line)
    grounded_row = next(line for line in summary.splitlines() if "grounded in the document" in line)
    assert reconcile_row.rstrip().endswith("| Pass |")
    assert grounded_row.rstrip().endswith("| Flag |")


def test_policy_check_flags_total_mismatch_independently():
    """Reverse case: line items don't reconcile with the total but every field is
    grounded — the grounding row must stay Pass while the reconciliation row flags."""
    rec = _record(
        invoice_no="INV-2026-0311", po="PO-2026-2007", total="2000.00",
        line_items=[{"name": "Service", "amount": "500.00"}],
    )
    route, flags = _route(rec)
    summary = _format_validation_summary(InvoiceRecord(**rec), route, flags)

    reconcile_row = next(line for line in summary.splitlines() if "Line items reconcile to total" in line)
    grounded_row = next(line for line in summary.splitlines() if "grounded in the document" in line)
    assert reconcile_row.rstrip().endswith("| Flag |")
    assert grounded_row.rstrip().endswith("| Pass |")


# ---------------------------------------------------------------------------
# classify_spend enriches the PO match (drives the StepCard / summary)
# ---------------------------------------------------------------------------


def test_classify_spend_direct_is_enriched():
    ctx = SimpleNamespace(state={"invoice_record": _record(po="PO-2026-1003")})
    ev = classify_spend._func(ctx, None)
    po = ev.actions.state_delta["po_match"]
    # classify_spend now runs before validation: it records the match and moves
    # on ("classified"); route_posting later routes to ERP for both spend types.
    assert ev.actions.route == "classified"
    assert po["route"] == "direct"
    assert po["matched"] is True
    assert po["target_system"] == "ERP"
    assert po["category"] == "Raw Materials"
    assert po["cost_center"] == "CC-PROD-1100"
    assert po["gl_account"] == "500100"


def test_classify_spend_unmatched_po_defaults_direct():
    ctx = SimpleNamespace(state={"invoice_record": _record(po="PO-9999-0000")})
    ev = classify_spend._func(ctx, None)
    po = ev.actions.state_delta["po_match"]
    assert ev.actions.route == "classified"
    assert po["route"] == "direct"
    assert po["matched"] is False
    assert po["target_system"] == "ERP"


# ---------------------------------------------------------------------------
# lookup_po normalization (makes the PO check tolerant of vendor formatting)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PO-2026-1003", "PO-2026-1003"),
        ("po-2026-1003", "PO-2026-1003"),
        ("  PO 2026 1003 ", "PO-2026-1003"),
        ("#PO-2026-2002", "PO-2026-2002"),
        ("PO_2026_2002", "PO-2026-2002"),
    ],
)
def test_lookup_po_normalizes(raw, expected):
    match = lookup_po(raw)
    assert match is not None
    assert match["po_number"] == expected


def test_lookup_po_unknown_returns_none():
    assert lookup_po("not-a-po") is None
    assert lookup_po("") is None
    assert lookup_po(None) is None


# ---------------------------------------------------------------------------
# Expanded mock master data (the "20 records in each" requirement)
# ---------------------------------------------------------------------------


def test_po_ledger_has_20_direct_and_20_indirect():
    direct = [v for v in MOCK_PO_DATABASE.values() if v["type"] == "direct"]
    indirect = [v for v in MOCK_PO_DATABASE.values() if v["type"] == "indirect"]
    assert len(direct) == 20
    assert len(indirect) == 20
    assert len(MOCK_PO_DATABASE) == 40


def test_posted_history_seeds_both_duplicate_scenarios():
    """Fixtures 04 and 05 only escalate because this history exists in Firestore."""
    by_invoice = {e["invoice_no"]: e for e in MOCK_POSTED_INVOICES}
    assert by_invoice["INV-2024-001"]["vendor"] == "Cascade Components Inc"  # fixture 04
    assert any(  # fixture 05: same vendor, PO and amount, different invoice number
        e["po_ref"] == "PO-2026-0098" and e["vendor"] == "Globex Trading Co" and e["total"] == 6_000.0
        for e in MOCK_POSTED_INVOICES
    )
    ids = [e["document_id"] for e in MOCK_POSTED_INVOICES]
    assert len(ids) == len(set(ids))


def test_po_records_are_well_formed():
    for po_no, rec in MOCK_PO_DATABASE.items():
        assert po_no.startswith("PO-"), po_no
        assert _PO_KEYS.issubset(rec.keys()), f"{po_no} missing {_PO_KEYS - set(rec)}"
        assert rec["type"] in ("direct", "indirect")
        assert isinstance(rec["amount"], float)


def test_both_routes_have_straight_through_examples():
    """At least one direct and one indirect PO sit below the approval limit so
    straight-through posting can be demoed on both SAP and Coupa paths."""
    under = lambda t: [  # noqa: E731
        v for v in MOCK_PO_DATABASE.values()
        if v["type"] == t and v["amount"] <= PO_AMOUNT_HITL_THRESHOLD_USD
    ]
    assert under("direct"), "no direct PO under the auto-approval limit"
    assert under("indirect"), "no indirect PO under the auto-approval limit"


def test_fixture_pdfs_exist():
    base = os.path.join(os.path.dirname(config.__file__), "..", "tests", "fixtures", "invoices")
    expected = [
        "01_direct_straight_through.pdf",
        "02_indirect_straight_through.pdf",
        "03_high_value_approval.pdf",
        "04_duplicate_invoice_number.pdf",
        "05_duplicate_po.pdf",
        "06_malformed_po_missing_fields.pdf",
        "07_blurry_scan.pdf",
        "07_blurry_scan.png",
        "08_business_rule_violation_zero_total.pdf",
    ]
    for name in expected:
        assert os.path.isfile(os.path.join(base, name)), f"missing fixture: {name}"


# ---------------------------------------------------------------------------
# Missing invoice_no edge cases
# ---------------------------------------------------------------------------


def _ctx_with_record(record: dict, flags: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state={"invoice_record": record, "validation_flags": flags or []},
        resume_inputs={},
    )


def test_missing_invoice_no_flags_required_field():
    """Validation must raise a flag when invoice_no is absent from the PDF."""
    rec = _record(invoice_no=None, po="PO-2026-1003", total="3250.00")
    rec["invoice_no"] = _fv(None, "not_found")
    route, flags = _route(rec)
    assert route == "human_review"
    assert any("invoice_no" in f.lower() or "required field" in f.lower() for f in flags)


def test_missing_invoice_no_brief_falls_back_to_po_ref():
    """prepare_rejection_brief uses the PO number when invoice_no is absent."""
    record = {
        "vendor": _fv("Helix Cloud Services"),
        "invoice_no": _fv(None, "not_found"),
        "po_ref": _fv("PO-2026-2007"),
        "total": _fv("4750.00"),
        "currency": _fv("USD"),
        "line_items": [],
    }
    prompt = prepare_rejection_brief._func(_ctx_with_record(record), None)
    assert "PO-2026-2007" in prompt
    assert "PO Reference" in prompt
    # Invoice number UNKNOWN must not leak into the prompt as a reference
    assert "Invoice no." not in prompt


def test_missing_invoice_no_and_po_ref_falls_back_to_amount():
    """When both identifiers are absent the prompt falls back to the amount."""
    record = {
        "vendor": _fv("Unknown Vendor"),
        "invoice_no": _fv(None, "not_found"),
        "po_ref": _fv(None, "not_found"),
        "total": _fv("1209.20"),
        "currency": _fv("INR"),
        "line_items": [],
    }
    prompt = prepare_rejection_brief._func(_ctx_with_record(record), None)
    assert "1209.20" in prompt
    assert "INR" in prompt
    # No "UNKNOWN" token should appear — the fallback message explains the gap
    assert "UNKNOWN" not in prompt


def test_both_identifiers_present_includes_both_in_brief():
    """prepare_rejection_brief includes invoice number AND PO ref when both are available."""
    record = {
        "vendor": _fv("Helix Cloud Services"),
        "invoice_no": _fv("INV-2026-0042"),
        "po_ref": _fv("PO-2026-2007"),
        "total": _fv("4750.00"),
        "currency": _fv("USD"),
        "line_items": [],
    }
    prompt = prepare_rejection_brief._func(_ctx_with_record(record), None)
    assert "INV-2026-0042" in prompt
    assert "PO-2026-2007" in prompt
