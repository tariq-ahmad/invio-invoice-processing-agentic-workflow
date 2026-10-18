"""Unit tests for the deterministic extraction-verification policy.

Mirrors tests/unit/test_screening.py: the model reports only observable
evidence (a value plus how it was obtained — source_grounding); a pure Python
policy (reconcile_totals / check_field_formats / assess_grounding /
_check_rules) owns the accept/HITL decision. No LLM is called — records are
injected directly as dicts.
"""

from __future__ import annotations

import pytest

from app.nodes._helpers import _safe_float
from app.nodes.validation import (
    _check_rules,
    assess_grounding,
    check_field_formats,
    reconcile_totals,
)
from app.schemas import FieldValue, InvoiceRecord, LineItem, LineItemAttribute

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fv(value, grounding="grounded"):
    return FieldValue(value=value, source_grounding=grounding)


def _record(
    *,
    vendor="Vendor",
    invoice_no="INV-X",
    po_ref="PO-X",
    total="100.00",
    currency="USD",
    grounding="grounded",
    line_items=None,
) -> InvoiceRecord:
    return InvoiceRecord(
        vendor=_fv(vendor, grounding),
        invoice_no=_fv(invoice_no, grounding),
        po_ref=_fv(po_ref, grounding),
        total=_fv(total, grounding),
        currency=_fv(currency, grounding),
        line_items=[LineItem(**li) for li in (line_items or [])],
    )


# ---------------------------------------------------------------------------
# FieldValue / InvoiceRecord schema
# ---------------------------------------------------------------------------


def test_field_value_defaults_to_not_found():
    """Default grounding is the safe side — an unpopulated field routes to
    review rather than silently passing as though it were confirmed."""
    assert FieldValue().source_grounding == "not_found"


def test_invoice_record_constructs_with_no_args():
    """The empty-PDF fallback (validate_invoice) constructs InvoiceRecord()
    with no args; every field must default to a safe not_found grounding."""
    rec = InvoiceRecord()
    assert rec.vendor.source_grounding == "not_found"
    assert rec.total.source_grounding == "not_found"
    assert rec.line_items == []


def test_line_item_defaults_to_not_found_grounding():
    """A line item built without an explicit grounding (e.g. the legacy
    free-form fallback path, or a test fixture) must default to the safe
    not_found side, same as a header FieldValue."""
    li = LineItem(name="A", amount="10.00")
    assert li.source_grounding == "not_found"


def test_line_item_attributes_round_trip():
    """attributes is an open-ended label/value list — not a fixed schema —
    so any invoice-specific set of attributes must construct and remain
    accessible verbatim."""
    li = LineItem(
        name="Fiber Leased Line",
        amount="298.14",
        attributes=[
            LineItemAttribute(label="Circuit ID", value="IS-10001"),
            LineItemAttribute(label="Site", value="Denver"),
            LineItemAttribute(label="Billing Period", value="06/01/2026 - 06/30/2026"),
        ],
        source_grounding="grounded",
    )
    assert [a.label for a in li.attributes] == ["Circuit ID", "Site", "Billing Period"]
    assert li.attributes[0].value == "IS-10001"


def test_line_item_attributes_defaults_empty_for_minimal_dict():
    """A LineItem built from a minimal {name, amount} dict (old cached data,
    or a fallback response with nothing beyond the essentials) must still
    construct cleanly, with attributes defaulting to an empty list."""
    li = LineItem(**{"name": "X", "amount": "1.00"})
    assert li.attributes == []


def test_line_items_complete_defaults_true_and_survives_parse():
    """The completeness flag defaults True and, when set False by the pipeline,
    survives InvoiceRecord(**raw) reconstruction (must be a declared field, since
    extra keys are ignored)."""
    assert InvoiceRecord().line_items_complete is True
    rec = InvoiceRecord(**{"line_items_complete": False})
    assert rec.line_items_complete is False


def test_incomplete_line_items_routes_review_with_distinct_flag():
    """A truncated (incomplete) line-item list must route to human review with a
    flag distinct from a genuine reconcile mismatch — and the reconcile check is
    skipped (its result would be a misleading 'do not reconcile' on partial data)."""
    rec = _record(total="100.00", line_items=[{"name": "A", "amount": "60.00"}])
    rec.line_items_complete = False
    route, flags = _check_rules(rec)
    assert route == "human_review"
    joined = " ".join(flags).lower()
    assert "line items incomplete" in joined
    assert "do not reconcile" not in joined


# ---------------------------------------------------------------------------
# reconcile_totals — deterministic arithmetic verification
# ---------------------------------------------------------------------------


def test_reconcile_totals_matching_sum_passes():
    rec = _record(total="100.00", line_items=[{"name": "A", "amount": "60.00"}, {"name": "B", "amount": "40.00"}])
    assert reconcile_totals(rec) is None


def test_reconcile_totals_mismatched_sum_flags():
    rec = _record(total="100.00", line_items=[{"name": "A", "amount": "60.00"}, {"name": "B", "amount": "10.00"}])
    flag = reconcile_totals(rec)
    assert flag is not None
    assert "do not reconcile" in flag.lower()


def test_reconcile_totals_no_line_items_is_not_flagged():
    """A lump-sum invoice with no line items has nothing to reconcile against."""
    rec = _record(total="100.00", line_items=[])
    assert reconcile_totals(rec) is None


def test_reconcile_totals_within_tolerance_passes():
    """Small rounding differences (within RECONCILE_TOLERANCE_PCT) don't flag."""
    rec = _record(total="100.00", line_items=[{"name": "A", "amount": "99.995"}])
    assert reconcile_totals(rec) is None


def test_safe_float_strips_currency_code_prefix():
    """Regression guard: this repo's own fixture generator prints line-item
    amounts as 'USD 2,250.00' (tests/fixtures/generate_invoices.py), which
    used to parse to None and silently drop from the sum."""
    assert _safe_float("USD 2,250.00") == 2250.00
    assert _safe_float("EUR 100.00") == 100.00
    assert _safe_float("$1,234.56") == 1234.56
    assert _safe_float("1234.56") == 1234.56


def test_reconcile_totals_currency_prefixed_amounts_reconcile():
    """A real bug: line items printed with a currency-code prefix used to sum
    to 0.00 (silently dropped by a broken parse) and falsely flag every such
    invoice for human review even though it reconciles exactly."""
    rec = _record(
        total="3250.00",
        line_items=[
            {"name": "A", "amount": "USD 2,250.00"},
            {"name": "B", "amount": "USD 1,000.00"},
        ],
    )
    assert reconcile_totals(rec) is None


# ---------------------------------------------------------------------------
# check_field_formats — deterministic format/plausibility checks
# ---------------------------------------------------------------------------


def test_check_field_formats_valid_currency_passes():
    rec = _record(currency="USD")
    assert check_field_formats(rec) == []


def test_check_field_formats_invalid_currency_flags():
    rec = _record(currency="US$")
    flags = check_field_formats(rec)
    assert any("not a valid iso 4217" in f.lower() for f in flags)


def test_check_field_formats_short_invoice_no_flags():
    rec = _record(invoice_no="X")
    flags = check_field_formats(rec)
    assert any("not a valid identifier" in f.lower() for f in flags)


# ---------------------------------------------------------------------------
# assess_grounding — deterministic policy over the model's evidence
# ---------------------------------------------------------------------------


def test_assess_grounding_all_grounded_passes():
    rec = _record(grounding="grounded")
    assert assess_grounding(rec) == []


def test_assess_grounding_inferred_field_flags():
    rec = _record(vendor="Vendor", grounding="grounded")
    rec.total = _fv("100.00", "inferred")
    flags = assess_grounding(rec)
    assert any("total" in f.lower() and "not grounded" in f.lower() for f in flags)


def test_assess_grounding_not_found_field_flags():
    rec = _record(grounding="grounded")
    rec.po_ref = _fv(None, "not_found")
    flags = assess_grounding(rec)
    assert any("po_ref" in f.lower() and "not grounded" in f.lower() for f in flags)


def test_assess_grounding_only_flags_required_fields():
    """currency is not in REQUIRED_FIELDS — an inferred currency must not flag."""
    rec = _record(grounding="grounded")
    rec.currency = _fv("USD", "inferred")
    assert assess_grounding(rec) == []


# ---------------------------------------------------------------------------
# _check_rules — end-to-end route (mirrors route_screening_assessment tests)
# ---------------------------------------------------------------------------


def test_route_all_grounded_and_reconciling_proceeds():
    rec = _record(
        total="100.00",
        line_items=[{"name": "Service", "amount": "100.00"}],
        grounding="grounded",
    )
    route, flags = _check_rules(rec)
    assert route == "proceed"
    assert flags == []


def test_route_ungrounded_required_field_routes_review():
    rec = _record(grounding="grounded")
    rec.total = _fv("100.00", "inferred")
    route, flags = _check_rules(rec)
    assert route == "human_review"
    assert any("not grounded" in f.lower() for f in flags)


def test_route_mismatched_line_items_routes_review():
    rec = _record(total="100.00", line_items=[{"name": "A", "amount": "1.00"}], grounding="grounded")
    route, flags = _check_rules(rec)
    assert route == "human_review"
    assert any("do not reconcile" in f.lower() for f in flags)


def test_route_malformed_currency_routes_review():
    rec = _record(currency="US$", grounding="grounded")
    route, flags = _check_rules(rec)
    assert route == "human_review"
    assert any("not a valid" in f.lower() for f in flags)


@pytest.mark.parametrize("field_name", ["vendor", "invoice_no", "po_ref", "total"])
def test_route_not_found_required_field_routes_review(field_name):
    rec = _record(grounding="grounded")
    setattr(rec, field_name, _fv(None, "not_found"))
    route, flags = _check_rules(rec)
    assert route == "human_review"
    assert any(field_name in f and "not grounded" in f.lower() for f in flags)


# ---------------------------------------------------------------------------
# Schema descriptions — field semantics must live in the schema (surfaced to
# Gemini's controlled generation via the JSON schema), not only in prompt
# prose. Regression guard so this can't silently go undocumented again.
# ---------------------------------------------------------------------------


def test_invoice_record_fields_carry_descriptions():
    schema = InvoiceRecord.model_json_schema()
    props = schema["properties"]
    for field_name in ("vendor", "invoice_no", "po_ref", "total", "currency", "line_items"):
        assert props[field_name].get("description"), f"{field_name} has no schema description"


def test_field_value_carries_grounding_description():
    schema = FieldValue.model_json_schema()
    props = schema["properties"]
    assert props["value"].get("description")
    grounding_desc = props["source_grounding"].get("description", "")
    assert grounding_desc
    # The judge-in-isolation rule is the load-bearing instruction that used to
    # live only in prompt prose — assert it's actually present in the schema.
    assert "isolation" in grounding_desc.lower()


def test_line_item_fields_carry_descriptions():
    schema = LineItem.model_json_schema()
    props = schema["properties"]
    assert props["name"].get("description")
    assert props["amount"].get("description")
    grounding_desc = props["source_grounding"].get("description", "")
    assert grounding_desc
    assert "isolation" in grounding_desc.lower()
