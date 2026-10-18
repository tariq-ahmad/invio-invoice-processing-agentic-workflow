"""HITL review: the structured rubric / primary trigger and the decision audit record.

Covers the two backend halves of the reviewer workbench (frontend/src/a2ui/ApprovalReview.tsx):
  * ``build_rubric`` — facts -> criterion -> result per rule, with ONE ranked
    primary trigger so the reviewer isn't handed an undifferentiated flag list.
  * ``human_approval_gate`` — captures approvals / rejections / overrides as a
    structured ``approval_audit`` record (reason code + rationale), not a bare word.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app import nodes as n
from app.nodes import _check_rules
from app.nodes.validation import build_rubric
from app.schemas import InvoiceRecord


def _fv(value, grounding="grounded"):
    return {"value": value, "source_grounding": grounding}


def _record(*, total="12500.00", po="PO-2026-1010", grounding="grounded", line_items=None) -> dict:
    return {
        "vendor": _fv("Meridian Steel & Alloys", grounding),
        "invoice_no": _fv("INV-2026-0303", grounding),
        "po_ref": _fv(po, grounding) if po is not None else _fv(None, "not_found"),
        "total": _fv(total, grounding),
        "currency": _fv("USD", grounding),
        "line_items": line_items or [],
    }


def _rubric(rec: dict):
    record = InvoiceRecord(**rec)
    _, flags = _check_rules(record)
    return build_rubric(record, flags), flags


# ---------------------------------------------------------------------------
# Rubric / primary trigger
# ---------------------------------------------------------------------------


def test_rubric_single_over_limit_trigger_is_primary():
    (rules, primary), flags = _rubric(_record())
    assert primary == "within_limit"
    flagged = [r for r in rules if r["status"] == "flag"]
    assert [r["id"] for r in flagged] == ["within_limit"]
    assert flagged[0]["rank"] == 1
    # Facts and criterion are both present — the chain, not just the flag.
    assert "12,500.00" in flagged[0]["observed"]
    assert "4,000" in flagged[0]["criterion"]
    assert flagged[0]["finding"] in flags[0]
    assert flagged[0]["headline"] == "Over the auto-approval limit"
    assert "≈" not in flagged[0]["observed"]  # USD: no redundant conversion


def test_over_limit_flag_shows_usd_conversion_only_for_foreign_currency():
    _, usd_flags = _check_rules(InvoiceRecord(**_record(total="12500.00")))
    assert "approx" not in usd_flags[0]
    assert usd_flags[0].startswith("Invoice total USD 12,500.00 exceeds")

    eur = _record(total="12500.00")
    eur["currency"] = _fv("EUR")
    _, eur_flags = _check_rules(InvoiceRecord(**eur))
    assert "EUR 12,500.00 (approx. $13,500.00) exceeds" in eur_flags[0]


def test_rubric_passing_rules_have_no_rank_or_finding():
    (rules, _), _ = _rubric(_record())
    passes = [r for r in rules if r["status"] == "pass"]
    assert passes and all(r["rank"] is None and r["finding"] is None for r in passes)


def test_rubric_ranks_integrity_issue_above_routine_limit():
    # Missing PO (data integrity) AND over limit: the integrity finding leads.
    (rules, primary), _ = _rubric(_record(po=None))
    assert primary == "required_fields"
    ranked = {r["id"]: r["rank"] for r in rules if r["status"] == "flag"}
    assert ranked["required_fields"] == 1
    assert ranked["within_limit"] == 2


def test_rubric_no_flags_has_no_primary_trigger():
    (rules, primary), flags = _rubric(_record(total="500.00"))
    assert flags == []
    assert primary is None
    assert all(r["status"] == "pass" for r in rules)


def test_rubric_unmapped_flag_is_surfaced_not_hidden():
    record = InvoiceRecord(**_record(total="500.00"))
    rules, primary = build_rubric(record, ["Something unforeseen happened"])
    assert primary == "other_0"
    assert rules[-1]["finding"] == "Something unforeseen happened"


# ---------------------------------------------------------------------------
# Decision audit capture
# ---------------------------------------------------------------------------


def _ctx(state: dict | None = None, resume: dict | None = None) -> Any:
    return SimpleNamespace(state=dict(state or {}), resume_inputs=dict(resume or {}))


async def _audit_of(ctx) -> dict:
    events = [e async for e in n.human_approval_gate._func(ctx, None)]
    return events[-1].actions.state_delta["approval_audit"]


def _gate_state(rec: dict) -> dict:
    (rules, primary), flags = _rubric(rec)
    return {
        "invoice_record": rec,
        "validation_flags": flags,
        "validation_rubric": rules,
        "validation_primary_trigger": primary,
    }


@pytest.fixture(autouse=True)
def _no_memory_writes(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(n.memory, "remember_decision", _noop)


@pytest.mark.asyncio
async def test_audit_captures_structured_approval():
    ctx = _ctx(
        state=_gate_state(_record()),
        resume={"approval_decision": {
            "decision": "approve",
            "reason_code": "WITHIN_DELEGATED_AUTHORITY",
            "note": "  Confirmed with requester.  ",
            "attested_source_review": True,
            "source_document_opened": True,
            "reviewer": "ap-copilot-user-x",
        }},
    )
    audit = await _audit_of(ctx)
    assert audit["decision"] == "approve"
    assert audit["reason_code"] == "WITHIN_DELEGATED_AUTHORITY"
    assert audit["note"] == "Confirmed with requester."
    assert audit["primary_trigger"] == "within_limit"
    assert audit["flagged_rules"] == ["within_limit"]
    assert audit["override"] is False  # routine limit trigger is not an override
    assert audit["attested_source_review"] is True
    assert audit["source_document_opened"] is True
    assert audit["reviewer"] == "ap-copilot-user-x"
    assert audit["decided_at"]


@pytest.mark.asyncio
async def test_audit_marks_integrity_approval_as_override():
    ctx = _ctx(
        state=_gate_state(_record(po=None)),
        resume={"approval_decision": {
            "decision": "approve", "reason_code": "OTHER", "note": "PO on paper copy.",
        }},
    )
    audit = await _audit_of(ctx)
    assert audit["override"] is True
    assert audit["primary_trigger"] == "required_fields"


@pytest.mark.asyncio
async def test_audit_rejection_is_never_an_override():
    ctx = _ctx(
        state=_gate_state(_record(po=None)),
        resume={"approval_decision": {"decision": "reject", "reason_code": "PO_MISSING_OR_INVALID"}},
    )
    audit = await _audit_of(ctx)
    assert audit["decision"] == "reject"
    assert audit["override"] is False


@pytest.mark.asyncio
async def test_audit_rejects_reason_code_from_wrong_decision():
    # An approve-only code on a rejection must not be recorded as valid.
    ctx = _ctx(
        state=_gate_state(_record()),
        resume={"approval_decision": {"decision": "reject", "reason_code": "VERIFIED_AGAINST_SOURCE"}},
    )
    assert (await _audit_of(ctx))["reason_code"] == "UNSPECIFIED"


@pytest.mark.asyncio
async def test_audit_bare_string_decision_is_unspecified():
    ctx = _ctx(state=_gate_state(_record()), resume={"approval_decision": "approve"})
    audit = await _audit_of(ctx)
    assert audit["decision"] == "approve"
    assert audit["reason_code"] == "UNSPECIFIED"
    assert audit["note"] == ""


@pytest.mark.asyncio
async def test_audit_rationale_is_optional_everywhere():
    # Neither options nor a note are required — even for an override or "Other".
    ctx = _ctx(
        state=_gate_state(_record(po=None)),
        resume={"approval_decision": {"decision": "approve", "reason_code": "OTHER"}},
    )
    audit = await _audit_of(ctx)
    assert audit["decision"] == "approve"
    assert audit["reason_code"] == "OTHER"
    assert audit["override"] is True
    assert audit["note"] == ""
    assert audit["rationale_options"] == []


@pytest.mark.asyncio
async def test_audit_records_selected_rationale_options():
    ctx = _ctx(
        state=_gate_state(_record()),
        resume={"approval_decision": {
            "decision": "approve",
            "reason_code": "VERIFIED_AGAINST_SOURCE",
            "rationale_options": ["WITHIN_BUDGET", "DOC_MATCHES_EXTRACTION", "WITHIN_BUDGET"],
        }},
    )
    # De-duplicated and sorted; no free-text note needed.
    assert (await _audit_of(ctx))["rationale_options"] == ["DOC_MATCHES_EXTRACTION", "WITHIN_BUDGET"]


@pytest.mark.asyncio
async def test_audit_drops_unknown_or_wrong_decision_rationale_options():
    ctx = _ctx(
        state=_gate_state(_record()),
        resume={"approval_decision": {
            "decision": "reject",
            "reason_code": "AMOUNT_INCORRECT",
            # NO_VALID_PO is valid for reject; WITHIN_BUDGET is approve-only; the rest is junk.
            "rationale_options": ["NO_VALID_PO", "WITHIN_BUDGET", "MADE_UP", 7],
        }},
    )
    assert (await _audit_of(ctx))["rationale_options"] == ["NO_VALID_PO"]


@pytest.mark.asyncio
async def test_audit_non_list_rationale_options_is_ignored():
    ctx = _ctx(
        state=_gate_state(_record()),
        resume={"approval_decision": {"decision": "approve", "reason_code": "OTHER", "rationale_options": "WITHIN_BUDGET"}},
    )
    assert (await _audit_of(ctx))["rationale_options"] == []


@pytest.mark.asyncio
async def test_audit_note_is_length_capped():
    ctx = _ctx(
        state=_gate_state(_record()),
        resume={"approval_decision": {"decision": "approve", "reason_code": "OTHER", "note": "x" * 5000}},
    )
    assert len((await _audit_of(ctx))["note"]) == 1000


# ---------------------------------------------------------------------------
# PO rules in the rubric
# ---------------------------------------------------------------------------


def _po(**over) -> dict:
    return {
        "matched": True,
        "po_number": "PO-2026-1010",
        "po_status": "open",
        "vendor_name": "Meridian Steel & Alloys",
        "po_amount": 50000.0,
        "currency": "USD",
        **over,
    }


def test_rubric_omits_po_rules_without_a_po_match():
    record = InvoiceRecord(**_record())
    rules, _ = build_rubric(record, [], po_match=None)
    assert not any(r["id"].startswith("po_") for r in rules)


def test_rubric_po_rules_pass_when_po_is_clean():
    record = InvoiceRecord(**_record(total="100.00"))
    po = _po()
    _, flags = _check_rules(record, po_match=po)
    rules, primary = build_rubric(record, flags, po_match=po)
    po_rules = [r for r in rules if r["id"].startswith("po_")]
    assert {r["id"] for r in po_rules} == {"po_status", "po_vendor", "po_amount"}
    assert all(r["status"] == "pass" for r in po_rules)
    assert primary is None


def test_rubric_flags_po_amount_and_status():
    record = InvoiceRecord(**_record(total="100.00"))
    po = _po(po_amount=50.0, po_status="closed")
    _, flags = _check_rules(record, po_match=po)
    rules, primary = build_rubric(record, flags, po_match=po)
    by_id = {r["id"]: r for r in rules}
    assert by_id["po_status"]["status"] == "flag"
    assert by_id["po_amount"]["status"] == "flag"
    assert by_id["po_vendor"]["status"] == "pass"
    assert primary == "po_status"  # severity order: status before amount
    assert not any(r["id"].startswith("other_") for r in rules)


@pytest.mark.asyncio
async def test_gate_writes_a_memory_summary_only_when_there_is_precedent(monkeypatch):
    calls: list[str] = []

    async def fake_review(*a, **k):
        calls.append("review")
        return {"text": "briefing", "source": "fallback", "model": None, "latency_ms": None}

    async def fake_memory(precedent, *a, **k):
        calls.append("memory")
        return {"text": "memory note", "source": "llm", "model": "m", "latency_ms": 5}

    import app.nodes.approval as approval_mod

    monkeypatch.setattr(approval_mod, "generate_review_summary", fake_review)
    monkeypatch.setattr(approval_mod, "generate_memory_summary", fake_memory)

    rec = _record(total="12500.00")
    base = _gate_state(rec)

    # No precedent: only the briefing is generated and no memory_summary is stored.
    events = [e async for e in n.human_approval_gate._func(_ctx(base), None)]
    delta = events[0].actions.state_delta
    assert "memory_summary" not in delta and calls == ["review"]

    # Real precedent: both are generated and both land in state.
    calls.clear()
    precedent = {"vendor": "V", "total_count": 2, "narrative": "t", "recommendation": None}
    events = [e async for e in n.human_approval_gate._func(_ctx({**base, "decision_precedents": precedent}), None)]
    delta = events[0].actions.state_delta
    assert delta["memory_summary"]["text"] == "memory note" and sorted(calls) == ["memory", "review"]

    # Re-entering the node (state already holds both) does not call the model again.
    calls.clear()
    held = {**base, "decision_precedents": precedent, "review_summary": delta["review_summary"], "memory_summary": delta["memory_summary"]}
    events = [e async for e in n.human_approval_gate._func(_ctx(held), None)]
    assert calls == [] and events[0].actions.state_delta["memory_summary"]["text"] == "memory note"
