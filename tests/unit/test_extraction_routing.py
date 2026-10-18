"""Unit tests for route_extraction_strategy (Phase 2 size/complexity router).

decide_extraction_strategy / decide_extraction_tier are pure functions over
precomputed scalars (byte size, page count, native-text flag) — no LLM, no
PDF parsing. _pdf_page_count_and_native_text and the route_extraction_strategy
node itself are exercised against small in-memory PDFs built with reportlab
(native-text) and pypdf (blank/no-text, standing in for a scanned page).
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from pypdf import PdfWriter
from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

from app.config import (
    FILES_API_SIZE_THRESHOLD_BYTES,
    MULTIPASS_PAGE_THRESHOLD,
    SMALL_INVOICE_PAGE_THRESHOLD,
    SMALL_INVOICE_SIZE_THRESHOLD_BYTES,
)
from app.models import EXTRACTION_THINKING_TIERS, MODEL_EXTRACTION_LITE, MODEL_EXTRACTION_PRO
from app.nodes._state import _INVOICE_PDF
from app.nodes.extraction import (
    _pdf_page_count_and_native_text,
    decide_extraction_strategy,
    decide_extraction_tier,
    route_extraction_strategy,
)

# ---------------------------------------------------------------------------
# decide_extraction_strategy — pure function over (byte_size, page_count)
# ---------------------------------------------------------------------------


def test_strategy_small_invoice_is_inline():
    assert decide_extraction_strategy(byte_size=200_000, page_count=2) == "inline"


def test_strategy_over_size_threshold_is_files_api():
    assert (
        decide_extraction_strategy(byte_size=FILES_API_SIZE_THRESHOLD_BYTES + 1, page_count=5)
        == "files_api"
    )


def test_strategy_over_page_threshold_is_multipass():
    assert (
        decide_extraction_strategy(byte_size=500_000, page_count=MULTIPASS_PAGE_THRESHOLD + 1)
        == "multipass"
    )


def test_strategy_multipass_takes_priority_over_files_api():
    """A document that is both very large AND very long is multipass, not
    files_api — the long-document line-item pass is the more specific need."""
    assert (
        decide_extraction_strategy(
            byte_size=FILES_API_SIZE_THRESHOLD_BYTES + 1,
            page_count=MULTIPASS_PAGE_THRESHOLD + 1,
        )
        == "multipass"
    )


def test_strategy_at_exact_page_threshold_is_not_multipass():
    assert decide_extraction_strategy(byte_size=500_000, page_count=MULTIPASS_PAGE_THRESHOLD) == "inline"


# ---------------------------------------------------------------------------
# decide_extraction_tier — driven by COMPLEXITY, not size alone
# ---------------------------------------------------------------------------


def test_tier_small_native_text_invoice_is_lite():
    assert (
        decide_extraction_tier(
            byte_size=100_000, page_count=1, has_native_text=True, strategy="inline"
        )
        == "lite"
    )


def test_tier_normal_native_text_invoice_is_flash():
    assert (
        decide_extraction_tier(
            byte_size=SMALL_INVOICE_SIZE_THRESHOLD_BYTES + 1, page_count=10,
            has_native_text=True, strategy="inline",
        )
        == "flash"
    )


def test_tier_small_scanned_invoice_escalates_to_pro():
    """The key size-vs-complexity case: a SMALL, single-page, byte-count-tiny
    invoice with NO native text (a blurry phone-photo scan) must escalate to
    the strongest tier, not drop to the cheapest one size alone would suggest."""
    assert (
        decide_extraction_tier(
            byte_size=50_000, page_count=1, has_native_text=False, strategy="inline"
        )
        == "pro"
    )


def test_tier_multipass_strategy_is_always_pro():
    """Even a native-text document routes to pro once it's long enough to need
    the multipass strategy."""
    assert (
        decide_extraction_tier(
            byte_size=500_000, page_count=40, has_native_text=True, strategy="multipass"
        )
        == "pro"
    )


def test_tier_page_threshold_boundary_is_lite():
    assert (
        decide_extraction_tier(
            byte_size=SMALL_INVOICE_SIZE_THRESHOLD_BYTES,
            page_count=SMALL_INVOICE_PAGE_THRESHOLD,
            has_native_text=True,
            strategy="inline",
        )
        == "lite"
    )


# ---------------------------------------------------------------------------
# _pdf_page_count_and_native_text — reads real PDF bytes
# ---------------------------------------------------------------------------


def _native_text_pdf(pages: int = 1) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    for _ in range(pages):
        c.drawString(72, 700, "INVOICE  Vendor: Acme Corp  Total: 100.00")
        c.showPage()
    c.save()
    return buf.getvalue()


def _blank_pdf(pages: int = 1) -> bytes:
    """A PDF with genuinely blank pages — no extractable text, standing in for
    a scanned/image-only page (pypdf's extract_text() returns "" either way)."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _photographed_scan_pdf(pages: int = 1) -> bytes:
    """A PDF that LOOKS digital to extract_text() (a scanning app's invisible
    OCR text layer) but is actually a full-page photograph — the exact shape
    of a phone-scanned receipt. Adobe Scan / Google Drive scan / iOS Notes /
    CamScanner all produce this: real extractable text UNDER a dominant image."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    img = Image.new("RGB", (1700, 2200), color=(200, 200, 200))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    w, h = LETTER
    for _ in range(pages):
        c.drawImage(ImageReader(img), 0, 0, width=w, height=h)
        c.drawString(72, 700, "STARBUCKS INVOICE  Total: 425.00")  # the OCR text layer
        c.showPage()
    c.save()
    return buf.getvalue()


def _native_text_pdf_with_small_logo(pages: int = 1) -> bytes:
    """A clean digital invoice that also carries a small letterhead logo —
    must NOT be mistaken for a photographed scan (size/aspect gates)."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    logo = Image.new("RGB", (80, 40), color=(10, 10, 200))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    for _ in range(pages):
        c.drawImage(ImageReader(logo), 72, 740, width=80, height=40)
        c.drawString(72, 700, "INVOICE  Vendor: Acme Corp  Total: 100.00")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_pdf_reader_detects_native_text():
    page_count, has_native_text = _pdf_page_count_and_native_text(_native_text_pdf(2), "application/pdf")
    assert page_count == 2
    assert has_native_text is True


def test_pdf_reader_detects_no_native_text():
    page_count, has_native_text = _pdf_page_count_and_native_text(_blank_pdf(1), "application/pdf")
    assert page_count == 1
    assert has_native_text is False


def test_non_pdf_attachment_is_single_page_no_native_text():
    """A scanned invoice emailed as a JPEG is treated as one image page."""
    page_count, has_native_text = _pdf_page_count_and_native_text(b"\xff\xd8\xff", "image/jpeg")
    assert page_count == 1
    assert has_native_text is False


def test_unreadable_pdf_fails_safe_to_pro_eligible_defaults():
    """A corrupt/unparseable PDF must fail toward the strongest tier, not the
    cheapest — 1 page / no native text routes decide_extraction_tier to pro."""
    page_count, has_native_text = _pdf_page_count_and_native_text(b"not a pdf", "application/pdf")
    assert page_count == 1
    assert has_native_text is False


def test_pdf_reader_detects_photographed_scan_despite_ocr_text_layer():
    """A phone-scanning app embeds a real, extractable OCR text layer UNDER a
    full-page photograph — extract_text() alone would say has_native_text=True,
    but the dominant full-page image means this is really a scan and must NOT
    be treated as a clean born-digital document."""
    page_count, has_native_text = _pdf_page_count_and_native_text(
        _photographed_scan_pdf(1), "application/pdf"
    )
    assert page_count == 1
    assert has_native_text is False


def test_pdf_reader_small_logo_image_does_not_trigger_false_positive():
    """An ordinary born-digital invoice with a small letterhead logo must still
    be recognized as native text — the size/aspect gates on the dominant-image
    check must not over-trigger on this common case."""
    page_count, has_native_text = _pdf_page_count_and_native_text(
        _native_text_pdf_with_small_logo(1), "application/pdf"
    )
    assert page_count == 1
    assert has_native_text is True


# ---------------------------------------------------------------------------
# route_extraction_strategy — the node itself
# ---------------------------------------------------------------------------


def _ctx(invocation_id: str) -> SimpleNamespace:
    return SimpleNamespace(invocation_id=invocation_id, state={})


def test_route_extraction_strategy_no_stash_defaults_inline():
    inv = "inv-route-missing"
    _INVOICE_PDF.pop(inv, None)
    event = route_extraction_strategy._func(_ctx(inv), None)
    state = event.actions.state_delta
    assert state["extraction_strategy"] == "inline"
    assert state["extraction_recommended_model"] == "flash"
    assert state["extraction_thinking_level"] == EXTRACTION_THINKING_TIERS["flash"]
    # Always present so the frontend can tell recommendation from reality even
    # when there's no PDF to reason about.
    assert "extraction_dynamic_tiering_enabled" in state


def test_route_extraction_strategy_small_native_text_recommends_lite():
    inv = "inv-route-lite"
    _INVOICE_PDF[inv] = (_native_text_pdf(1), "application/pdf")
    try:
        event = route_extraction_strategy._func(_ctx(inv), None)
        state = event.actions.state_delta
        assert state["extraction_strategy"] == "inline"
        assert state["extraction_recommended_model"] == "lite"
        # peeked, not popped — the extraction before_model_callback and
        # emit_extraction_card still need the stashed bytes downstream.
        assert inv in _INVOICE_PDF
    finally:
        _INVOICE_PDF.pop(inv, None)


def test_route_extraction_strategy_escalates_photographed_receipt_to_pro():
    """A blurry phone-photo receipt (1 page, small, OCR text layer under a
    full-page image) must escalate to pro, not fall through to lite/flash on
    size alone -- the exact bug this fix addresses."""
    inv = "inv-route-photo-scan"
    _INVOICE_PDF[inv] = (_photographed_scan_pdf(1), "application/pdf")
    try:
        event = route_extraction_strategy._func(_ctx(inv), None)
        state = event.actions.state_delta
        assert state["extraction_recommended_model"] == "pro"
        assert state["extraction_has_native_text"] is False
    finally:
        _INVOICE_PDF.pop(inv, None)


def test_route_extraction_strategy_emits_document_signals_for_ui_transparency(monkeypatch):
    """The frontend can only explain 'why' a tier was picked if the raw size/
    complexity signals actually reach state — regression guard for that."""
    import app.nodes.extraction as extraction_module

    monkeypatch.setattr(extraction_module, "EXTRACTION_DYNAMIC_MODEL_TIERING", False)
    inv = "inv-route-signals"
    pdf_bytes = _native_text_pdf(1)
    _INVOICE_PDF[inv] = (pdf_bytes, "application/pdf")
    try:
        event = route_extraction_strategy._func(_ctx(inv), None)
        state = event.actions.state_delta
        assert state["extraction_byte_size"] == len(pdf_bytes)
        assert state["extraction_has_native_text"] is True
        assert state["extraction_thinking_level"] == EXTRACTION_THINKING_TIERS["lite"]
        assert state["extraction_dynamic_tiering_enabled"] is False
    finally:
        _INVOICE_PDF.pop(inv, None)


def test_route_extraction_strategy_scanned_reports_no_native_text():
    inv = "inv-route-scanned-signal"
    _INVOICE_PDF[inv] = (_blank_pdf(1), "application/pdf")
    try:
        event = route_extraction_strategy._func(_ctx(inv), None)
        state = event.actions.state_delta
        assert state["extraction_has_native_text"] is False
    finally:
        _INVOICE_PDF.pop(inv, None)


def test_route_extraction_strategy_scanned_recommends_pro():
    inv = "inv-route-scanned"
    _INVOICE_PDF[inv] = (_blank_pdf(1), "application/pdf")
    try:
        event = route_extraction_strategy._func(_ctx(inv), None)
        state = event.actions.state_delta
        assert state["extraction_recommended_model"] == "pro"
    finally:
        _INVOICE_PDF.pop(inv, None)


def test_route_extraction_strategy_long_document_recommends_multipass_and_pro():
    inv = "inv-route-multipass"
    _INVOICE_PDF[inv] = (_native_text_pdf(MULTIPASS_PAGE_THRESHOLD + 1), "application/pdf")
    try:
        event = route_extraction_strategy._func(_ctx(inv), None)
        state = event.actions.state_delta
        assert state["extraction_strategy"] == "multipass"
        assert state["extraction_recommended_model"] == "pro"
        assert state["extraction_page_count"] == MULTIPASS_PAGE_THRESHOLD + 1
        # Regression guard: multipass gets a dedicated line-items pass, so the
        # main call only needs MEDIUM thinking on the header fields — HIGH
        # thinking here was what caused a real production truncation bug on
        # an 80-page invoice (see app/nodes/extraction.py's
        # decide_extraction_thinking_level docstring).
        assert state["extraction_thinking_level"] == "MEDIUM"
    finally:
        _INVOICE_PDF.pop(inv, None)


def test_decide_extraction_thinking_level_multipass_overrides_pro_to_medium():
    from app.nodes.extraction import decide_extraction_thinking_level

    assert decide_extraction_thinking_level("pro", "multipass") == "MEDIUM"
    # Non-multipass "pro" (e.g. a short scanned document) keeps its full HIGH
    # depth — the override is specific to multipass's token-budget pressure.
    assert decide_extraction_thinking_level("pro", "inline") == "HIGH"
    assert decide_extraction_thinking_level("lite", "inline") == "MEDIUM"
    assert decide_extraction_thinking_level("flash", "inline") == "MEDIUM"


def test_route_extraction_strategy_never_mutates_shared_agent(monkeypatch):
    """route_extraction_strategy must be a pure state-writer: mutating the
    shared extraction_agent singleton directly here was the root cause of a
    real production race (two concurrent invoices stomping each other's model/
    thinking-level choice). Guard that neither mutator is ever called from here."""
    import app.agent as agent_module

    model_calls = []
    monkeypatch.setattr(agent_module, "set_agent_model", lambda *a: model_calls.append(a))

    inv = "inv-route-no-mutation"
    _INVOICE_PDF[inv] = (_native_text_pdf(1), "application/pdf")
    try:
        route_extraction_strategy._func(_ctx(inv), None)
        assert model_calls == []
        assert not hasattr(agent_module, "set_agent_thinking_level")
    finally:
        _INVOICE_PDF.pop(inv, None)


# ---------------------------------------------------------------------------
# app.agent._extraction_before / _extraction_after — the per-invocation
# injection point that replaced the shared-singleton mutation above. These
# read/write llm_request directly (a fresh object per call, never shared),
# so two concurrent invocations can never see each other's tier/model.
# ---------------------------------------------------------------------------


def _llm_request(model="gemini-3.8-flash", thinking_level=None):
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types as genai_types

    config = genai_types.GenerateContentConfig(
        thinking_config=genai_types.ThinkingConfig(
            thinking_level=thinking_level or genai_types.ThinkingLevel.MEDIUM
        )
    )
    return LlmRequest(model=model, config=config)


def _cb_ctx(invocation_id: str, state: dict) -> SimpleNamespace:
    return SimpleNamespace(invocation_id=invocation_id, state=state)


def test_extraction_before_applies_tier_model_and_thinking_when_enabled(monkeypatch):
    import app.agent as agent_module

    monkeypatch.setattr(agent_module, "EXTRACTION_DYNAMIC_MODEL_TIERING", True)
    req = _llm_request()
    ctx = _cb_ctx(
        "inv-before-on",
        {"extraction_recommended_model": "lite", "extraction_thinking_level": "MINIMAL"},
    )
    agent_module._extraction_before(ctx, req)
    assert req.model == MODEL_EXTRACTION_LITE
    assert req.config.thinking_config.thinking_level == agent_module.genai_types.ThinkingLevel.MINIMAL


def test_extraction_before_leaves_model_alone_when_tiering_disabled(monkeypatch):
    import app.agent as agent_module

    monkeypatch.setattr(agent_module, "EXTRACTION_DYNAMIC_MODEL_TIERING", False)
    req = _llm_request(model="gemini-3.8-flash")
    ctx = _cb_ctx(
        "inv-before-off",
        {"extraction_recommended_model": "lite", "extraction_thinking_level": "MINIMAL"},
    )
    agent_module._extraction_before(ctx, req)
    # Model swap is gated off; thinking-level is NOT — it's not a model swap,
    # so it still applies (matches route_extraction_strategy's own docstring).
    assert req.model == "gemini-3.8-flash"
    assert req.config.thinking_config.thinking_level == agent_module.genai_types.ThinkingLevel.MINIMAL


def test_extraction_before_after_no_race_between_concurrent_invocations(monkeypatch):
    """The direct regression test for the production bug: two invocations
    processed with overlapping before/after calls must never see each other's
    model. Interleaved on purpose — A's `before` then B's `before` then A's
    `after` then B's `after` — to simulate a request landing mid-flight."""
    import app.agent as agent_module

    monkeypatch.setattr(agent_module, "EXTRACTION_DYNAMIC_MODEL_TIERING", True)

    req_a = _llm_request(model="gemini-3.8-flash")
    ctx_a = _cb_ctx("inv-race-a", {"extraction_recommended_model": "lite", "extraction_thinking_level": "MINIMAL"})
    req_b = _llm_request(model="gemini-3.8-flash")
    ctx_b = _cb_ctx("inv-race-b", {"extraction_recommended_model": "pro", "extraction_thinking_level": "HIGH"})

    agent_module._extraction_before(ctx_a, req_a)
    agent_module._extraction_before(ctx_b, req_b)
    assert req_a.model == MODEL_EXTRACTION_LITE
    assert req_b.model == MODEL_EXTRACTION_PRO

    class _Resp:
        usage_metadata = None
        finish_reason = None
        candidates = []

    agent_module._extraction_after(ctx_a, _Resp())
    agent_module._extraction_after(ctx_b, _Resp())
    # Each invocation's own state must report ITS OWN model, never the other's.
    assert ctx_a.state["extraction_model"] == MODEL_EXTRACTION_LITE
    assert ctx_b.state["extraction_model"] == MODEL_EXTRACTION_PRO
