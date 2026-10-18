"""Unit tests for the screening nodes.

Tests screen_email_signal routing logic and discard_non_invoice terminal
behaviour. No LLM is called — assessments are injected directly as dicts.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import nodes as n
from app.nodes.screening import route_screening_assessment
from app.schemas import InvoiceScreeningAssessment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(state: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(state=dict(state or {}), resume_inputs={})


def _assessment(
    *,
    attachment: str = "absent",
    body: str = "absent",
    subject: str = "absent",
    signals=None,
    rationale="",
) -> dict:
    return {
        "attachment_signal": attachment,
        "body_signal": body,
        "subject_signal": subject,
        "observed_signals": signals or [],
        "assessment_rationale": rationale,
    }


def _route_of(event) -> str | None:
    """Pull the route from a sync-returning Event."""
    actions = getattr(event, "actions", None)
    if actions is not None:
        return getattr(actions, "route", None)
    return None


# ---------------------------------------------------------------------------
# InvoiceScreeningAssessment schema
# ---------------------------------------------------------------------------


def test_screening_assessment_signal_fields_required():
    """The three signal fields have no default — the model must commit a value
    per source so an empty response can't silently read as all-'absent'."""
    with pytest.raises(ValidationError):
        InvoiceScreeningAssessment()


def test_screening_assessment_audit_fields_default():
    a = InvoiceScreeningAssessment(
        attachment_signal="absent", body_signal="absent", subject_signal="absent"
    )
    assert a.observed_signals == []
    assert a.assessment_rationale == ""


def test_screening_assessment_round_trip():
    a = InvoiceScreeningAssessment(
        attachment_signal="strong",
        body_signal="weak",
        subject_signal="strong",
        observed_signals=["PDF attached", "Invoice number present"],
        assessment_rationale="Clear vendor invoice with corroborating signals.",
    )
    assert a.attachment_signal == "strong"
    assert len(a.observed_signals) == 2


# ---------------------------------------------------------------------------
# route_screening_assessment — deterministic policy
# ---------------------------------------------------------------------------


def test_policy_two_strong_proceeds():
    assert route_screening_assessment(_assessment(attachment="strong", subject="strong")) == "invoice"


def test_policy_single_strong_discards():
    """A lone strong signal is NOT enough on its own: one signal (e.g. an
    invoice-sounding filename) is too easy to spoof or coincidental in
    isolation — at least two corroborating signals are required to proceed."""
    assert route_screening_assessment(_assessment(body="strong")) == "not_invoice"


def test_policy_two_weak_discards():
    """Weak signals never independently proceed, regardless of count: two
    generic/ambiguous cues don't corroborate each other the way two strong
    cues do."""
    assert route_screening_assessment(_assessment(attachment="weak", subject="weak")) == "not_invoice"


def test_policy_three_weak_discards():
    """Even all three signals weak still discards — weak count never
    substitutes for strong corroboration."""
    assert (
        route_screening_assessment(_assessment(attachment="weak", body="weak", subject="weak"))
        == "not_invoice"
    )


def test_policy_all_absent_discards():
    assert route_screening_assessment(_assessment()) == "not_invoice"


def test_policy_single_weak_discards():
    """A single weak hint with no strong signal is treated as noise."""
    assert route_screening_assessment(_assessment(subject="weak")) == "not_invoice"


def test_policy_one_strong_one_weak_discards():
    """A strong+weak mix doesn't meet either corroboration bar (needs 2 strong
    or 2 weak of the same tier) — a real boundary the old single-strong-proceeds
    policy never had to consider."""
    assert route_screening_assessment(_assessment(attachment="strong", subject="weak")) == "not_invoice"


# ---------------------------------------------------------------------------
# screen_email_signal — routing
# ---------------------------------------------------------------------------


def test_screen_strong_signals_route_invoice():
    assessment = _assessment(attachment="strong", body="strong", signals=["PDF attached", "Invoice #INV-001"])
    event = n.screen_email_signal._func(_ctx(), assessment)
    assert _route_of(event) == "invoice"


def test_screen_no_signals_route_not_invoice():
    event = n.screen_email_signal._func(_ctx(), _assessment())
    assert _route_of(event) == "not_invoice"


def test_screen_persists_assessment_to_state():
    """screen_email_signal writes the assessment dict via Event state delta."""
    assessment = _assessment(attachment="strong", subject="weak")
    event = n.screen_email_signal._func(_ctx(), assessment)
    state = getattr(event, "state", None) or {}
    actions = getattr(event, "actions", None)
    if not state and actions:
        state = getattr(actions, "state_delta", {}) or {}
    assert state.get("screening_assessment") is not None
    assert state["screening_assessment"]["attachment_signal"] == "strong"


def test_screen_fallback_reads_from_state():
    """When node_input is not a dict, falls back to ctx.state['screening_assessment']."""
    assessment = _assessment(attachment="strong", body="strong")
    ctx = _ctx(state={"screening_assessment": assessment})
    event = n.screen_email_signal._func(ctx, None)
    assert _route_of(event) == "invoice"


def test_screen_emits_content():
    """Event carries a content text summarising the assessment."""
    assessment = _assessment(attachment="strong", signals=["PDF attached"])
    event = n.screen_email_signal._func(_ctx(), assessment)
    content = getattr(event, "content", None)
    assert content is not None
    text = " ".join(
        (getattr(p, "text", "") or "") for p in getattr(content, "parts", []) if hasattr(p, "text")
    )
    assert "Screening" in text


# ---------------------------------------------------------------------------
# discard_non_invoice — terminal node
# ---------------------------------------------------------------------------


def test_discard_emits_content():
    """discard_non_invoice yields at least one event with content."""
    ctx = _ctx(state={"screening_assessment": _assessment(rationale="OOO email")})
    events = list(n.discard_non_invoice._func(ctx, None))
    assert len(events) >= 1
    content = getattr(events[0], "content", None)
    assert content is not None


def test_discard_sets_pipeline_status():
    """discard_non_invoice writes pipeline_status='discarded' to state."""
    ctx = _ctx()
    events = list(n.discard_non_invoice._func(ctx, None))
    statuses = []
    for e in events:
        state = getattr(e, "state", None) or getattr(getattr(e, "actions", None), "state_delta", None) or {}
        if isinstance(state, dict):
            statuses.append(state.get("pipeline_status"))
    assert any(s == "discarded" for s in statuses)


def test_discard_includes_rationale_when_present():
    """When a rationale note exists it should appear in the emitted content."""
    ctx = _ctx(state={
        "screening_assessment": _assessment(rationale="This is an OOO auto-reply")
    })
    events = list(n.discard_non_invoice._func(ctx, None))
    all_text = ""
    for e in events:
        content = getattr(e, "content", None)
        if content:
            all_text += " ".join(
                getattr(p, "text", "") for p in getattr(content, "parts", []) if hasattr(p, "text")
            )
    assert "OOO auto-reply" in all_text
