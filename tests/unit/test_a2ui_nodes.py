"""Regression tests for the A2UI-emitting HITL nodes and emit_extraction_card.

These drive the node functions directly with a minimal fake Context, asserting:
  * the nodes emit an A2UI inline-data part (when ENABLE_A2UI is on),
  * the existing string-based routing still works (backward compatible),
  * the new structured (dict) resume payloads route correctly and persist edits,
  * emit_extraction_card forwards the InvoiceRecord dict downstream.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app import a2ui_views as v
from app import nodes as n

RECORD = {
    "vendor": {"value": "Acme Corp", "confidence": 0.98},
    "invoice_no": {"value": "INV-2024-001", "confidence": 0.95},
    "po_ref": {"value": "PO-2024-100", "confidence": 0.60},
    "total": {"value": "12500.00", "confidence": 0.90},
    "currency": {"value": "USD", "confidence": 0.99},
    "overall_confidence": 0.88,
    "line_items": [],
}


def _ctx(state: dict | None = None, resume: dict | None = None) -> Any:
    return SimpleNamespace(state=dict(state or {}), resume_inputs=dict(resume or {}))


async def _drain_async(node, ctx, node_input=None) -> list:
    return [item async for item in node._func(ctx, node_input)]


def _drain_sync(node, ctx, node_input=None) -> list:
    return list(node._func(ctx, node_input))


def _has_a2ui_part(event) -> bool:
    content = getattr(event, "content", None)
    if not content or not getattr(content, "parts", None):
        return False
    return any(
        getattr(p, "inline_data", None) and p.inline_data.mime_type == v.A2UI_MIME
        for p in content.parts
    )


def _route_of(events) -> str | None:
    for e in events:
        actions = getattr(e, "actions", None)
        if actions is not None and getattr(actions, "route", None):
            return actions.route
        # RequestInput and bare Events without a route are skipped.
    return None


# ---------------------------------------------------------------------------
# emit_extraction_card
# ---------------------------------------------------------------------------


def test_emit_extraction_card_forwards_record_and_emits_surface(monkeypatch):
    monkeypatch.setenv("ENABLE_A2UI", "true")
    events = _drain_sync(n.emit_extraction_card, _ctx(), RECORD)
    assert len(events) == 1
    assert events[0].output == RECORD  # forwarded for validate_invoice
    assert _has_a2ui_part(events[0])


def test_emit_extraction_card_falls_back_to_state(monkeypatch):
    monkeypatch.setenv("ENABLE_A2UI", "true")
    events = _drain_sync(n.emit_extraction_card, _ctx(state={"invoice_record": RECORD}), None)
    assert events[0].output == RECORD


def test_emit_extraction_card_disabled_emits_no_surface(monkeypatch):
    monkeypatch.setenv("ENABLE_A2UI", "false")
    events = _drain_sync(n.emit_extraction_card, _ctx(), RECORD)
    assert events[0].output == RECORD
    assert not _has_a2ui_part(events[0])


def _ctx_with_pdf(inv_id: str, state: dict | None = None) -> Any:
    """Context carrying an invocation_id + a stashed PDF for that invocation."""
    n._INVOICE_PDF[inv_id] = (b"%PDF-1.4 fake", "application/pdf")
    return SimpleNamespace(
        state=dict(state or {}), resume_inputs={}, invocation_id=inv_id
    )


def test_line_items_no_fallback_when_main_pass_succeeded(monkeypatch):
    # Main pass returned line items and did NOT truncate -> no second model call.
    called = []
    monkeypatch.setattr(n.extraction, "_extract_line_items_direct", lambda *a, **k: called.append(1) or ([], 0, 0, 0, 0, False))
    record = {**RECORD, "line_items": [{"name": "Widget", "amount": "10.00", "confidence": 0.9}]}
    ctx = _ctx_with_pdf("inv-ok", state={"extraction_output_tokens": 200})
    events = _drain_sync(n.emit_extraction_card, ctx, record)
    assert called == []  # fallback skipped
    assert events[0].output["line_items"] == record["line_items"]  # main-pass items kept
    assert "inv-ok" not in n._INVOICE_PDF  # PDF still popped (no leak)


def test_line_items_fallback_fires_on_truncation(monkeypatch):
    # A MAX_TOKENS stop reason means the structured output truncated -> fallback runs.
    recovered = [{"name": "Recovered", "amount": "99.00", "confidence": 0.8}]
    monkeypatch.setattr(n.extraction, "_extract_line_items_direct", lambda *a, **k: (recovered, 111, 222, 33, 0, False))
    ctx = _ctx_with_pdf(
        "inv-trunc",
        state={"extraction_finish_reason": "FinishReason.MAX_TOKENS"},
    )
    events = _drain_sync(n.emit_extraction_card, ctx, {**RECORD, "line_items": []})
    assert events[0].output["line_items"] == recovered
    # fallback tokens folded into the extraction step totals (base 0 + 222/33)
    assert events[0].actions.state_delta["extraction_output_tokens"] == 222
    assert events[0].actions.state_delta["extraction_thinking_tokens"] == 33
    # A complete pass marks the record complete and flags state as not-incomplete.
    assert events[0].output["line_items_complete"] is True
    assert events[0].actions.state_delta["line_items_incomplete"] is False


def test_line_items_incomplete_flag_surfaces(monkeypatch):
    # A truncated multi-call extraction that never finished -> incomplete=True must
    # surface on the record (for validate_invoice) and in state (for the UI).
    partial = [{"name": "Partial", "amount": "12.00"}]
    monkeypatch.setattr(
        n.extraction, "_extract_line_items_direct",
        lambda *a, **k: (partial, 10, 20, 0, 0, True),
    )
    ctx = _ctx_with_pdf("inv-incomplete", state={"extraction_strategy": "multipass"})
    events = _drain_sync(n.emit_extraction_card, ctx, {**RECORD, "line_items": []})
    assert events[0].output["line_items"] == partial
    assert events[0].output["line_items_complete"] is False
    assert events[0].actions.state_delta["line_items_incomplete"] is True


def test_line_items_pass_latency_folded_into_extraction_latency_ms(monkeypatch):
    # emit_extraction_card must time the free-form line-items pass itself: the
    # model callback in app/agent.py only times extraction_agent's OWN call, so
    # without this fold-in the UI badge silently omits the (often much longer)
    # line-items pass entirely -- a real bug this regression-tests against.
    import time as time_mod

    def _slow_line_items(*a, **k):
        time_mod.sleep(0.05)
        return ([{"name": "A", "amount": "1.00"}], 5, 5, 0, 0, False)

    monkeypatch.setattr(n.extraction, "_extract_line_items_direct", _slow_line_items)
    ctx = _ctx_with_pdf(
        "inv-slow",
        state={"extraction_strategy": "multipass", "extraction_latency_ms": 1000},
    )
    events = _drain_sync(n.emit_extraction_card, ctx, {**RECORD, "line_items": []})
    new_latency = events[0].actions.state_delta["extraction_latency_ms"]
    # base (1000ms) + most of the real ~50ms sleep, tolerant of scheduling jitter.
    assert new_latency >= 1000 + 30


def test_extraction_token_folding_is_additive_not_overwritten(monkeypatch):
    # Regression proof that the combined in/out/thinking token counts reflect
    # BOTH the main extraction_agent call and the line-items pass -- addresses
    # the suspicion that pricing undercounts a long-running multi-call extraction.
    monkeypatch.setattr(
        n.extraction, "_extract_line_items_direct",
        lambda *a, **k: ([{"name": "A", "amount": "1.00"}], 1000, 2000, 300, 0, False),
    )
    ctx = _ctx_with_pdf(
        "inv-tokens",
        state={
            "extraction_strategy": "multipass",
            "extraction_input_tokens": 5000,
            "extraction_output_tokens": 400,
            "extraction_thinking_tokens": 50,
        },
    )
    events = _drain_sync(n.emit_extraction_card, ctx, {**RECORD, "line_items": []})
    delta = events[0].actions.state_delta
    assert delta["extraction_input_tokens"] == 5000 + 1000
    assert delta["extraction_output_tokens"] == 400 + 2000
    assert delta["extraction_thinking_tokens"] == 50 + 300


def test_extraction_cost_usd_blends_per_call_model_rates(monkeypatch):
    # The main extraction_agent call and the line-items fallback pass can run
    # on DIFFERENT models (main call dynamically tiered, line-items pinned to
    # MODEL_EXTRACTION) -- extraction_cost_usd must price each call's tokens
    # at ITS OWN model's rate rather than the folded total at one rate.
    from app.cost_calculator import compute_cost
    from app.models import MODEL_EXTRACTION, MODEL_EXTRACTION_PRO

    monkeypatch.setattr(
        n.extraction, "_extract_line_items_direct",
        lambda *a, **k: ([{"name": "A", "amount": "1.00"}], 1000, 2000, 300, 0, False),
    )
    ctx = _ctx_with_pdf(
        "inv-blended-cost",
        state={
            "extraction_strategy": "multipass",
            "extraction_model": MODEL_EXTRACTION_PRO,
            "extraction_input_tokens": 5000,
            "extraction_output_tokens": 400,
            "extraction_thinking_tokens": 50,
        },
    )
    events = _drain_sync(n.emit_extraction_card, ctx, {**RECORD, "line_items": []})
    delta = events[0].actions.state_delta
    expected = compute_cost(MODEL_EXTRACTION_PRO, 5000, 400, 50) + compute_cost(
        MODEL_EXTRACTION, 1000, 2000, 300
    )
    assert delta["extraction_cost_usd"] == pytest.approx(expected)


def test_extraction_cost_usd_is_main_call_only_without_line_items_pass(monkeypatch):
    monkeypatch.setattr(
        n.extraction, "_extract_line_items_direct",
        lambda *a, **k: ([], 0, 0, 0, 0, False),
    )
    from app.cost_calculator import compute_cost
    from app.models import MODEL_EXTRACTION_PRO

    ctx = _ctx(
        state={
            "extraction_model": MODEL_EXTRACTION_PRO,
            "extraction_input_tokens": 2000,
            "extraction_output_tokens": 300,
            "extraction_thinking_tokens": 20,
        }
    )
    events = _drain_sync(n.emit_extraction_card, ctx, {**RECORD, "line_items": []})
    delta = events[0].actions.state_delta
    assert delta["extraction_cost_usd"] == pytest.approx(
        compute_cost(MODEL_EXTRACTION_PRO, 2000, 300, 20)
    )


def test_line_items_pass_stays_on_flash_even_when_main_call_is_tiered_to_pro(monkeypatch):
    # Regression guard: the line-items fallback must NOT follow the main
    # call's dynamically-selected tier. It's a throughput/coverage-bound
    # enumeration task under a hard shared thinking+output token ceiling --
    # tiering it to pro was tried and directly regressed completeness (a
    # 500+-item multipass invoice fell to ~33 captured items, since pro
    # consumes more of that shared budget per call than flash, leaving far
    # less room for actual item JSON). Flash stays the deliberate, pinned
    # choice for this call regardless of what tier the main call used.
    from app.cost_calculator import compute_cost
    from app.models import MODEL_EXTRACTION, MODEL_EXTRACTION_PRO

    monkeypatch.setattr(n.extraction, "EXTRACTION_DYNAMIC_MODEL_TIERING", True)
    calls = []

    def _fake_line_items(*a, **k):
        calls.append(k.get("model"))
        return ([{"name": "A", "amount": "1.00"}], 1000, 2000, 300, 0, False)

    monkeypatch.setattr(n.extraction, "_extract_line_items_direct", _fake_line_items)
    ctx = _ctx_with_pdf(
        "inv-tiered-line-items",
        state={
            "extraction_strategy": "multipass",
            "extraction_recommended_model": "pro",
            "extraction_model": MODEL_EXTRACTION_PRO,
            "extraction_input_tokens": 5000,
            "extraction_output_tokens": 400,
            "extraction_thinking_tokens": 50,
        },
    )
    events = _drain_sync(n.emit_extraction_card, ctx, {**RECORD, "line_items": []})
    assert calls == [MODEL_EXTRACTION]
    delta = events[0].actions.state_delta
    expected = compute_cost(MODEL_EXTRACTION_PRO, 5000, 400, 50) + compute_cost(
        MODEL_EXTRACTION, 1000, 2000, 300
    )
    assert delta["extraction_cost_usd"] == pytest.approx(expected)


def test_line_items_no_fallback_on_genuinely_empty(monkeypatch):
    # Empty line items WITHOUT any truncation signal is a valid item-less invoice;
    # the 10s fallback must NOT run just because the list is empty.
    called = []
    monkeypatch.setattr(n.extraction, "_extract_line_items_direct", lambda *a, **k: called.append(1) or ([], 0, 0, 0, 0, False))
    ctx = _ctx_with_pdf("inv-empty", state={"extraction_output_tokens": 300})
    events = _drain_sync(n.emit_extraction_card, ctx, {**RECORD, "line_items": []})
    assert called == []
    assert events[0].output["line_items"] == []


def _li(name: str, amount: str = "1.00") -> dict:
    return {"name": name, "amount": amount}


def _script_single_calls(monkeypatch, *results):
    """Feed _extract_line_items_direct a scripted sequence of single-call results,
    each a (items, in_tok, out_tok, think_tok, cache_tok, truncated) tuple."""
    seq = list(results)
    monkeypatch.setattr(
        n.extraction, "_line_items_single_call",
        lambda att, mime, prompt, model=None: seq.pop(0),
    )


def test_line_items_loop_concatenates_across_truncation(monkeypatch):
    _script_single_calls(
        monkeypatch,
        ([_li("A"), _li("B"), _li("C")], 5, 10, 0, 0, True),
        ([_li("D"), _li("E")], 5, 10, 0, 0, False),
    )
    items, tin, tout, tthink, _cache, incomplete = n.extraction._extract_line_items_direct(b"pdf", "application/pdf")
    assert [i["name"] for i in items] == ["A", "B", "C", "D", "E"]
    assert (tin, tout, tthink) == (10, 20, 0)  # summed across both calls
    assert incomplete is False


def test_line_items_loop_strips_anchor_overlap(monkeypatch):
    # The continuation re-emits the last anchor items; the leading overlap run is
    # stripped so nothing is double-counted.
    _script_single_calls(
        monkeypatch,
        ([_li("A"), _li("B"), _li("C")], 0, 0, 0, 0, True),
        ([_li("B"), _li("C"), _li("D"), _li("E")], 0, 0, 0, 0, False),
    )
    items, *_rest, incomplete = n.extraction._extract_line_items_direct(b"pdf", "application/pdf")
    assert [i["name"] for i in items] == ["A", "B", "C", "D", "E"]
    assert incomplete is False


def test_line_items_loop_preserves_interior_duplicates(monkeypatch):
    # Legitimately identical rows inside a batch must survive (only a LEADING
    # overlap run is stripped, never the interior).
    _script_single_calls(
        monkeypatch,
        ([_li("A"), _li("B")], 0, 0, 0, 0, True),
        ([_li("B"), _li("C"), _li("C")], 0, 0, 0, 0, False),
    )
    items, *_rest = n.extraction._extract_line_items_direct(b"pdf", "application/pdf")
    assert [i["name"] for i in items] == ["A", "B", "C", "C"]


def test_line_items_loop_full_restart_guard(monkeypatch):
    # K=3 anchors cannot cover a 5-item full restart; the head-match guard must.
    monkeypatch.setattr(n.extraction, "LINE_ITEM_CONTINUATION_ANCHOR_K", 3)
    _script_single_calls(
        monkeypatch,
        ([_li("A"), _li("B"), _li("C"), _li("D"), _li("E")], 0, 0, 0, 0, True),
        ([_li("A"), _li("B"), _li("C"), _li("D"), _li("E"), _li("F")], 0, 0, 0, 0, False),
    )
    items, *_rest = n.extraction._extract_line_items_direct(b"pdf", "application/pdf")
    assert [i["name"] for i in items] == ["A", "B", "C", "D", "E", "F"]


def test_line_items_loop_cap_marks_incomplete(monkeypatch):
    monkeypatch.setattr(n.extraction, "MAX_LINE_ITEM_CALLS", 2)
    _script_single_calls(
        monkeypatch,
        ([_li("A"), _li("B")], 5, 7, 1, 0, True),
        ([_li("C"), _li("D")], 5, 7, 1, 0, True),  # still truncated at the cap
    )
    items, tin, tout, tthink, _cache, incomplete = n.extraction._extract_line_items_direct(b"pdf", "application/pdf")
    assert [i["name"] for i in items] == ["A", "B", "C", "D"]
    assert (tin, tout, tthink) == (10, 14, 2)
    assert incomplete is True


def test_line_items_loop_stops_when_no_progress(monkeypatch):
    # Truncated but the continuation adds nothing new -> stop (no infinite loop),
    # marked incomplete.
    _script_single_calls(
        monkeypatch,
        ([_li("A"), _li("B")], 0, 0, 0, 0, True),
        ([_li("A"), _li("B")], 0, 0, 0, 0, True),  # pure overlap -> zero new
    )
    items, *_rest, incomplete = n.extraction._extract_line_items_direct(b"pdf", "application/pdf")
    assert [i["name"] for i in items] == ["A", "B"]
    assert incomplete is True


def test_line_items_loop_continues_on_natural_stop_short_of_total(monkeypatch):
    # A non-truncated first call (no MAX_TOKENS, valid JSON) can still be a
    # PREMATURE stop on a long repetitive list -- the API gives no truncation
    # signal for that. expected_total must drive a continuation anyway.
    _script_single_calls(
        monkeypatch,
        ([_li("A", "20.00"), _li("B", "20.00")], 0, 0, 0, 0, False),  # natural stop, only 40
        ([_li("C", "60.00")], 0, 0, 0, 0, False),  # continuation reconciles it
    )
    items, *_rest, incomplete = n.extraction._extract_line_items_direct(
        b"pdf", "application/pdf", expected_total="100.00"
    )
    assert [i["name"] for i in items] == ["A", "B", "C"]
    assert incomplete is False


def test_line_items_loop_gives_up_when_retry_adds_nothing(monkeypatch):
    # Natural stop short of the total, but a retry adds nothing new -> the
    # model insists it's done. Accept it; do NOT claim "incomplete" for a gap
    # reconcile_totals (downstream, same tolerance) is better placed to flag.
    _script_single_calls(
        monkeypatch,
        ([_li("A", "20.00")], 0, 0, 0, 0, False),
        ([_li("A", "20.00")], 0, 0, 0, 0, False),  # re-emits the same item, nothing new
    )
    items, *_rest, incomplete = n.extraction._extract_line_items_direct(
        b"pdf", "application/pdf", expected_total="100.00"
    )
    assert [i["name"] for i in items] == ["A"]
    assert incomplete is False


def test_line_items_loop_no_expected_total_keeps_prior_behavior(monkeypatch):
    # Without a parseable expected_total, a non-truncated first call is trusted
    # immediately, exactly as before this fix (no reconciliation-driven retry).
    called = []
    monkeypatch.setattr(
        n.extraction, "_line_items_single_call",
        lambda *a, **k: called.append(1) or ([_li("A", "1.00")], 0, 0, 0, 0, False),
    )
    items, *_rest, incomplete = n.extraction._extract_line_items_direct(
        b"pdf", "application/pdf"
    )
    assert len(called) == 1  # no continuation attempted
    assert incomplete is False


def test_invoice_record_accepts_line_items_and_ignores_legacy_fields():
    from app.schemas import InvoiceRecord

    rec = InvoiceRecord(
        line_items=[{"name": "x", "amount": "1.00", "confidence": 0.9, "fields": {"a": "b"}}]
    )
    assert rec.line_items[0].name == "x"
    assert not hasattr(rec.line_items[0], "fields")  # legacy key dropped, not an error


# ---------------------------------------------------------------------------
# recall_precedents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_precedents_noop_when_memory_disabled():
    # ENABLE_MEMORY defaults to false — the node must be a complete no-op.
    ctx = _ctx(state={"invoice_record": RECORD, "validation_flags": ["Total exceeds threshold"]})
    result = await n.recall_precedents._func(ctx, None)
    assert result is None


@pytest.mark.asyncio
async def test_recall_precedents_emits_card_when_precedent_found(monkeypatch):
    monkeypatch.setattr(n.memory, "ENABLE_MEMORY", True)
    monkeypatch.setattr(n.memory, "_service", None)
    monkeypatch.setattr(n.memory, "MEMORY_PRECEDENT_MIN", 1)
    monkeypatch.setenv("ENABLE_A2UI", "true")

    flag = "Invoice total USD 12,500.00 exceeds the AP auto-approval limit of $4,000.00."
    await n.memory.remember_decision(
        "Acme Corp", n.memory.flag_kinds([flag]), "approve", amount="12500.00", currency="USD"
    )

    ctx = _ctx(state={"invoice_record": RECORD, "validation_flags": [flag]})
    event = await n.recall_precedents._func(ctx, None)

    assert event is not None
    precedent = event.actions.state_delta["decision_precedents"]
    assert precedent["vendor"] == "Acme Corp"
    assert precedent["total_count"] == 1
    assert precedent["approved_amounts"] == ["USD 12500.00"]
    assert "USD 12500.00" in precedent["narrative"]
    assert precedent["recommendation"] == "approve"
    assert _has_a2ui_part(event)


@pytest.mark.asyncio
async def test_recall_precedents_emits_empty_when_memory_ran_but_no_precedent(monkeypatch):
    # Memory is on and the vendor is known, but nothing was recalled: the node
    # still emits a zero-count summary so the approval card can show its empty
    # state — no A2UI card, no re-introduced standalone surface.
    monkeypatch.setattr(n.memory, "ENABLE_MEMORY", True)
    monkeypatch.setattr(n.memory, "_service", None)  # fresh, empty store

    ctx = _ctx(state={"invoice_record": RECORD, "validation_flags": ["Total exceeds threshold"]})
    event = await n.recall_precedents._func(ctx, None)

    assert event is not None
    precedent = event.actions.state_delta["decision_precedents"]
    assert precedent["vendor"] == "Acme Corp"
    assert precedent["total_count"] == 0
    assert not _has_a2ui_part(event)


# ---------------------------------------------------------------------------
# human_approval_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_prompt_emits_a2ui(monkeypatch):
    monkeypatch.setenv("ENABLE_A2UI", "true")
    ctx = _ctx(state={"invoice_record": RECORD, "validation_flags": ["High value"]})
    events = await _drain_async(n.human_approval_gate, ctx)
    assert any(_has_a2ui_part(e) for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,expected", [("approve", "approve"), ("reject", "reject")])
async def test_approval_resume_string(decision, expected):
    ctx = _ctx(resume={"approval_decision": decision})
    assert _route_of(await _drain_async(n.human_approval_gate, ctx)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,expected", [("approve", "approve"), ("reject", "reject")])
async def test_approval_resume_structured(decision, expected):
    ctx = _ctx(resume={"approval_decision": {"interrupt_id": "approval_decision", "decision": decision}})
    assert _route_of(await _drain_async(n.human_approval_gate, ctx)) == expected


@pytest.mark.asyncio
async def test_approval_resume_persists_decision_to_memory(monkeypatch):
    calls: list[tuple] = []

    async def _fake_remember(vendor, kinds, decision, amount=None, currency=None):
        calls.append((vendor, kinds, decision, amount, currency))

    monkeypatch.setattr(n.memory, "remember_decision", _fake_remember)
    ctx = _ctx(
        state={"invoice_record": RECORD, "validation_flags": ["Total exceeds threshold"]},
        resume={"approval_decision": "approve"},
    )
    await _drain_async(n.human_approval_gate, ctx)

    assert len(calls) == 1
    vendor, _kinds, decision, amount, currency = calls[0]
    assert vendor == "Acme Corp"
    assert decision == "approve"
    assert amount == RECORD["total"]["value"]
    assert currency == RECORD["currency"]["value"]


@pytest.mark.asyncio
async def test_approval_resume_skips_memory_write_when_vendor_unknown(monkeypatch):
    calls: list[tuple] = []

    async def _fake_remember(vendor, kinds, decision, amount=None, currency=None):
        calls.append((vendor, kinds, decision, amount, currency))

    monkeypatch.setattr(n.memory, "remember_decision", _fake_remember)
    ctx = _ctx(resume={"approval_decision": "approve"})  # no invoice_record in state
    await _drain_async(n.human_approval_gate, ctx)

    assert calls == []


# ---------------------------------------------------------------------------
# review_email_draft
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_review_prompt_emits_a2ui(monkeypatch):
    monkeypatch.setenv("ENABLE_A2UI", "true")
    ctx = _ctx(state={"rejection_email_draft": {"subject": "S", "body": "B"}, "invoice_sender_email": "v@x.com"})
    events = await _drain_async(n.review_email_draft, ctx)
    assert any(_has_a2ui_part(e) for e in events)


@pytest.mark.asyncio
async def test_email_review_resume_string_send():
    ctx = _ctx(state={"email_review_count": 0}, resume={"email_review_0": "send"})
    assert _route_of(await _drain_async(n.review_email_draft, ctx)) == "send"


@pytest.mark.asyncio
async def test_email_review_resume_string_feedback_revises():
    ctx = _ctx(state={"email_review_count": 0}, resume={"email_review_0": "make it shorter"})
    events = await _drain_async(n.review_email_draft, ctx)
    assert _route_of(events) == "revise"
    # feedback + incremented count are written to state via the event's state_delta
    sd = events[-1].actions.state_delta
    assert sd["email_feedback"] == "make it shorter"
    assert sd["email_review_count"] == 1


@pytest.mark.asyncio
async def test_email_review_structured_send_persists_edits():
    ctx = _ctx(
        state={"email_review_count": 0, "rejection_email_draft": {"subject": "old", "body": "old"}},
        resume={"email_review_0": {"decision": "send", "subject": "New subject", "body": "Edited body"}},
    )
    events = await _drain_async(n.review_email_draft, ctx)
    assert _route_of(events) == "send"
    sd = events[-1].actions.state_delta
    assert sd["rejection_email_draft"] == {"subject": "New subject", "body": "Edited body"}


@pytest.mark.asyncio
async def test_email_review_structured_revise():
    ctx = _ctx(state={"email_review_count": 1}, resume={"email_review_1": {"feedback": "soften the tone"}})
    events = await _drain_async(n.review_email_draft, ctx)
    assert _route_of(events) == "revise"
    assert events[-1].actions.state_delta["email_feedback"] == "soften the tone"
