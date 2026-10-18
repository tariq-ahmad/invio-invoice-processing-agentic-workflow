"""Unit tests for app/memory.py — Agent Memory Bank advisory precedent recall.

No LLM calls; exercises flag_kinds(), remember_decision(), and
recall_precedents() directly against the in-memory fallback service.
"""

from __future__ import annotations

import pytest

from app import memory as m


@pytest.fixture(autouse=True)
def _fresh_memory_service(monkeypatch):
    """Fresh in-memory service + memory enabled for every test in this file."""
    monkeypatch.setattr(m, "_service", None)
    monkeypatch.setattr(m, "ENABLE_MEMORY", True)
    monkeypatch.setattr(m, "MEMORY_PRECEDENT_MIN", 2)
    yield


# ---------------------------------------------------------------------------
# flag_kinds
# ---------------------------------------------------------------------------


def test_flag_kinds_classifies_known_patterns():
    flags = [
        "PO number already invoiced (duplicate): PO-2026-0098",
        "Invoice total USD 12,500.00 (approx. $12,500.00) exceeds the AP "
        "auto-approval limit of $4,000.00.",
        "Required field 'total' is not grounded in the document",
    ]
    assert m.flag_kinds(flags) == ["duplicate", "over_limit", "ungrounded_field"]


def test_flag_kinds_classifies_reconciliation_and_format():
    flags = [
        "Line items (60.00) do not reconcile with the invoice total (100.00)",
        "Currency 'US$' is not a valid ISO 4217 code",
    ]
    assert m.flag_kinds(flags) == ["total_mismatch", "invalid_format"]


def test_flag_kinds_falls_back_to_other():
    assert m.flag_kinds(["Some unrecognized exception message"]) == ["other"]


def test_flag_kinds_empty_list():
    assert m.flag_kinds([]) == ["other"]


# ---------------------------------------------------------------------------
# remember_decision / recall_precedents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_returns_none_when_no_precedent():
    assert await m.recall_precedents("Acme Corp", ["over_limit"]) is None


@pytest.mark.asyncio
async def test_remember_and_recall_tally():
    await m.remember_decision("Acme Corp", ["over_limit"], "approve")
    await m.remember_decision("Acme Corp", ["over_limit"], "approve")

    summary = await m.recall_precedents("Acme Corp", ["over_limit"])

    assert summary is not None
    assert summary.vendor == "Acme Corp"
    assert summary.total_count == 2
    assert summary.approve_count == 2
    assert summary.reject_count == 0
    assert summary.recommendation == "approve"


@pytest.mark.asyncio
async def test_recommendation_requires_min_threshold():
    await m.remember_decision("Acme Corp", ["over_limit"], "approve")

    summary = await m.recall_precedents("Acme Corp", ["over_limit"])

    assert summary.total_count == 1
    assert summary.recommendation is None  # below MEMORY_PRECEDENT_MIN=2


@pytest.mark.asyncio
async def test_recommendation_requires_unanimous_decisions():
    await m.remember_decision("Acme Corp", ["over_limit"], "approve")
    await m.remember_decision("Acme Corp", ["over_limit"], "reject")

    summary = await m.recall_precedents("Acme Corp", ["over_limit"])

    assert summary.approve_count == 1
    assert summary.reject_count == 1
    assert summary.recommendation is None  # not unanimous


@pytest.mark.asyncio
async def test_vendor_isolation():
    await m.remember_decision("Acme Corp", ["over_limit"], "approve")
    await m.remember_decision("Acme Corp", ["over_limit"], "approve")

    assert await m.recall_precedents("Other Vendor", ["over_limit"]) is None


@pytest.mark.asyncio
async def test_flag_kind_isolation():
    await m.remember_decision("Acme Corp", ["duplicate"], "reject")
    await m.remember_decision("Acme Corp", ["duplicate"], "reject")

    # Same vendor, but no overlap with the queried flag kind.
    assert await m.recall_precedents("Acme Corp", ["over_limit"]) is None


@pytest.mark.asyncio
async def test_flag_kinds_never_leak_from_the_query_into_the_result():
    """Regression test: a multi-flag invoice must not have its own flag list
    echoed back as if memory confirmed all of them. Only kinds actually
    present on a matched historical fact may appear in flag_kinds/narrative.

    Scenario: current invoice trips 3 rules at once (over_limit,
    missing_field, low_confidence). Two DIFFERENT single-issue historical
    facts match (one over_limit-only approved, one missing_field-only
    rejected) — low_confidence was never actually recorded for this vendor.
    """
    await m.remember_decision("Acme Corp", ["over_limit"], "approve", amount="9789.00", currency="USD")
    await m.remember_decision("Acme Corp", ["missing_field"], "reject", amount="5750.00", currency="USD")

    summary = await m.recall_precedents(
        "Acme Corp", ["over_limit", "missing_field", "low_confidence"]
    )

    assert summary is not None
    assert summary.total_count == 2
    assert summary.approve_count == 1
    assert summary.reject_count == 1
    # Only the kinds actually confirmed by a matched fact — order follows the
    # query (the invoice's own flag order), never invented set-iteration order.
    assert summary.flag_kinds == ["over_limit", "missing_field"]
    assert "low_confidence" not in summary.flag_kinds
    assert "low extraction confidence" not in summary.narrative
    assert "an amount above the auto-approval limit" in summary.narrative
    assert "a missing required field" in summary.narrative


@pytest.mark.asyncio
async def test_disabled_is_a_full_noop(monkeypatch):
    monkeypatch.setattr(m, "ENABLE_MEMORY", False)

    await m.remember_decision("Acme Corp", ["over_limit"], "approve")
    assert await m.recall_precedents("Acme Corp", ["over_limit"]) is None


@pytest.mark.asyncio
async def test_empty_vendor_is_a_noop():
    await m.remember_decision("", ["over_limit"], "approve")
    assert await m.recall_precedents("", ["over_limit"]) is None


# ---------------------------------------------------------------------------
# Amount/currency capture + narrative (schema v2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_and_recall_include_amount():
    await m.remember_decision("Acme Corp", ["over_limit"], "approve", amount="4750.00", currency="USD")

    summary = await m.recall_precedents("Acme Corp", ["over_limit"])

    assert summary.approved_amounts == ["USD 4750.00"]
    assert "USD 4750.00" in summary.narrative


@pytest.mark.asyncio
async def test_narrative_unanimous_reject_with_amounts():
    await m.remember_decision("Acme Corp", ["over_limit"], "reject", amount="5200.00", currency="USD")
    await m.remember_decision("Acme Corp", ["over_limit"], "reject", amount="6100.00", currency="USD")

    summary = await m.recall_precedents("Acme Corp", ["over_limit"])

    assert summary.recommendation == "reject"
    assert summary.rejected_amounts == ["USD 5200.00", "USD 6100.00"]
    assert "rejected every time before (2 of 2)" in summary.narrative
    assert "USD 5200.00" in summary.narrative
    assert "USD 6100.00" in summary.narrative
    assert "recommends **reject**" in summary.narrative


@pytest.mark.asyncio
async def test_narrative_split_decisions_mentions_both_amounts():
    await m.remember_decision("Acme Corp", ["over_limit"], "approve", amount="4750.00", currency="USD")
    await m.remember_decision("Acme Corp", ["over_limit"], "reject", amount="9000.00", currency="USD")

    summary = await m.recall_precedents("Acme Corp", ["over_limit"])

    assert summary.recommendation is None
    assert "Reviewers have been split" in summary.narrative
    assert "USD 4750.00" in summary.narrative
    assert "USD 9000.00" in summary.narrative


@pytest.mark.asyncio
async def test_decode_tolerates_pre_v2_facts_without_amount():
    """The 2 facts already written to the live Memory Bank engine predate the
    amount/currency fields — decode must not choke on them."""
    from google.adk.events.event import Event
    from google.genai import types as genai_types

    legacy_fact = "vendor=Acme Corp|flags=over_limit|decision=approve|ts=2026-07-01T00:00:00Z"
    event = Event(
        id="legacy-1",
        author="user",
        content=genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=legacy_fact)]),
    )
    await m._get_service().add_events_to_memory(app_name="app", user_id="ap-team", events=[event])

    summary = await m.recall_precedents("Acme Corp", ["over_limit"])

    assert summary is not None
    assert summary.total_count == 1
    assert summary.approved_amounts == []  # amount unknown for the legacy fact
    assert "approved every time before (1 of 1)." in summary.narrative  # no amount clause


# ---------------------------------------------------------------------------
# _kind_phrase / _narrate — plain-English rendering, no LLM
# ---------------------------------------------------------------------------


def test_kind_phrase_maps_jargon_to_plain_english():
    assert m._kind_phrase(["over_limit"]) == "an amount above the auto-approval limit"
    assert m._kind_phrase(["duplicate"]) == "a duplicate invoice or PO flag"
    combined = m._kind_phrase(["duplicate", "over_limit"])
    assert "duplicate invoice or PO flag" in combined
    assert "amount above the auto-approval limit" in combined


def test_narrative_never_leaks_internal_kind_token():
    narrative = m._narrate("Acme Corp", ["over_limit"], 1, 1, 0, None, ["USD 100.00"], [])
    assert "over_limit" not in narrative
