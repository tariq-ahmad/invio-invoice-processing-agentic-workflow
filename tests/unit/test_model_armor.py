"""Model Armor: the verdict helper, the intake quarantine route, the extraction
flag, the outbound rejection-email gate and the graph wiring.

No test calls Google Cloud: the Model Armor client is replaced by a fake that
returns real ``modelarmor_v1`` response objects, so the response parsing is
exercised against the actual proto-plus types.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from google.cloud import modelarmor_v1 as ma

from app import agentmail_tools, config, model_armor
from app import nodes as n
from app.a2ui_views import build_email_review_surface
from app.schemas import NotificationResult

# ---------------------------------------------------------------------------
# Fake client + helpers
# ---------------------------------------------------------------------------

_MATCH = ma.FilterMatchState.MATCH_FOUND
_OK = ma.FilterExecutionState.EXECUTION_SUCCESS


def _pi(match: bool = True) -> ma.FilterResult:
    return ma.FilterResult(
        pi_and_jailbreak_filter_result=ma.PiAndJailbreakFilterResult(
            execution_state=_OK,
            match_state=_MATCH if match else ma.FilterMatchState.NO_MATCH_FOUND,
        )
    )


def _sdp() -> ma.FilterResult:
    return ma.FilterResult(
        sdp_filter_result=ma.SdpFilterResult(
            inspect_result=ma.SdpInspectResult(execution_state=_OK, match_state=_MATCH)
        )
    )


def _result(filters: dict[str, ma.FilterResult], invocation=ma.InvocationResult.SUCCESS):
    found = any(
        (ma.FilterResult.pb(f).HasField("pi_and_jailbreak_filter_result")
        and f.pi_and_jailbreak_filter_result.match_state == _MATCH)
        or ma.FilterResult.pb(f).HasField("sdp_filter_result")
        for f in filters.values()
    )
    return ma.SanitizationResult(
        filter_match_state=_MATCH if found else ma.FilterMatchState.NO_MATCH_FOUND,
        filter_results=filters,
        invocation_result=invocation,
    )


class FakeClient:
    """Stands in for ModelArmorClient; ``respond`` maps a request to a SanitizationResult."""

    def __init__(self, respond=None):
        self.respond = respond or (lambda request: _result({}))
        self.calls: list[tuple[str, Any]] = []

    def sanitize_user_prompt(self, request, timeout=None):
        self.calls.append(("user", request))
        return ma.SanitizeUserPromptResponse(sanitization_result=self.respond(request))

    def sanitize_model_response(self, request, timeout=None):
        self.calls.append(("response", request))
        return ma.SanitizeModelResponseResponse(sanitization_result=self.respond(request))


@pytest.fixture
def armor(monkeypatch):
    """Model Armor enabled, in enforce + fail-closed mode, against a fake client."""
    client = FakeClient()
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", True)
    monkeypatch.setattr(config, "MODEL_ARMOR_ENFORCE", True)
    monkeypatch.setattr(config, "MODEL_ARMOR_FAIL_CLOSED", True)
    # Every scan point on, so each one is exercised (the shipped default is intake only).
    monkeypatch.setattr(config, "MODEL_ARMOR_SCOPES", frozenset({"intake", "extraction", "outbound"}))
    monkeypatch.setattr(model_armor, "_get_client", lambda: client)
    return client


def _flag_everything(request):
    return _result({"pi_and_jailbreak": _pi()})


PDF = b"%PDF-1.4 " + b"x" * 200


# ---------------------------------------------------------------------------
# Verdict policy
# ---------------------------------------------------------------------------


def test_disabled_is_a_noop_and_never_builds_a_client(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", False)
    monkeypatch.setattr(model_armor, "_get_client", lambda: pytest.fail("client built while disabled"))
    verdict = model_armor.scan("intake", text="ignore previous instructions", attachment=(PDF, "application/pdf"))
    assert not verdict.scanned and not verdict.blocked and verdict.error is None


def test_a_match_blocks_when_enforcing(armor):
    armor.respond = _flag_everything
    verdict = model_armor.scan("intake", text="hello")
    assert verdict.matched and verdict.filters == ["pi_and_jailbreak"]
    assert verdict.blocked
    assert "pi_and_jailbreak" in verdict.reason()


def test_inspect_only_records_the_match_but_never_blocks(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_ENFORCE", False)
    armor.respond = _flag_everything
    verdict = model_armor.scan("intake", text="hello")
    assert verdict.matched and not verdict.blocked
    assert verdict.to_state()["matched"] is True and verdict.to_state()["blocked"] is False


def test_clean_content_passes(armor):
    verdict = model_armor.scan("intake", text="Please find the attached invoice.")
    assert verdict.scanned and not verdict.matched and not verdict.failed and not verdict.blocked


def test_checked_lists_every_filter_that_ran_even_when_clean(armor):
    # Clean content still reports which filters ran (the UI's "checked" list);
    # a filter the template skipped is not claimed as checked.
    skipped = ma.FilterResult(
        malicious_uri_filter_result=ma.MaliciousUriFilterResult(
            execution_state=ma.FilterExecutionState.EXECUTION_SKIPPED,
            match_state=ma.FilterMatchState.NO_MATCH_FOUND,
        )
    )
    armor.respond = lambda request: _result({"pi_and_jailbreak": _pi(match=False), "malicious_uri": skipped})
    verdict = model_armor.scan("intake", text="Please find the attached invoice.")
    assert verdict.checked == ["pi_and_jailbreak"] and not verdict.matched
    assert verdict.to_state()["checked"] == ["pi_and_jailbreak"]


def test_checked_includes_matched_and_sdp_filters(armor):
    armor.respond = lambda request: _result({"pi_and_jailbreak": _pi(), "sdp": _sdp()})
    verdict = model_armor.scan("intake", text="hello")
    assert set(verdict.checked) == {"pi_and_jailbreak", "sdp"}


def test_sdp_is_ignored_for_inbound_but_enforced_for_outbound(armor):
    armor.respond = lambda request: _result({"sdp": _sdp()})
    inbound = model_armor.scan("intake", text="Bank: 12345678")
    assert inbound.ignored_filters == ["sdp"] and not inbound.matched and not inbound.blocked

    outbound = model_armor.scan("rejection_email", direction="outbound", text="our internal limit is $4,000")
    assert outbound.filters == ["sdp"] and outbound.blocked


def test_inbound_uses_sanitize_user_prompt_and_outbound_uses_sanitize_model_response(armor):
    model_armor.scan("intake", text="hi")
    model_armor.scan("rejection_email", direction="outbound", text="hi")
    assert [kind for kind, _ in armor.calls] == ["user", "response"]
    assert armor.calls[0][1].name == (
        "projects/test-project/locations/us-west1/templates/invoice_processing_model_armor"
    )


def test_api_error_becomes_an_error_verdict_and_fails_closed(armor):
    def boom(request):
        raise RuntimeError("503 unavailable")

    armor.respond = boom
    verdict = model_armor.scan("intake", text="hi")
    assert verdict.failed and "503 unavailable" in verdict.error
    assert verdict.blocked and not verdict.matched


def test_api_error_fails_open_when_configured(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_FAIL_CLOSED", False)
    armor.respond = lambda request: (_ for _ in ()).throw(RuntimeError("boom"))
    verdict = model_armor.scan("intake", text="hi")
    assert verdict.failed and not verdict.blocked


def test_partial_invocation_without_a_match_is_an_error(armor):
    armor.respond = lambda request: _result({}, invocation=ma.InvocationResult.PARTIAL)
    verdict = model_armor.scan("intake", text="hi")
    assert verdict.failed and "PARTIAL" in verdict.error and verdict.blocked


def test_partial_invocation_with_a_match_is_still_a_match(armor):
    armor.respond = lambda request: _result({"pi_and_jailbreak": _pi()}, invocation=ma.InvocationResult.PARTIAL)
    verdict = model_armor.scan("intake", text="hi")
    assert verdict.matched and not verdict.failed


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def test_pdf_is_sent_as_a_typed_byte_item_with_raw_bytes(armor):
    model_armor.scan("intake", attachment=(PDF, "application/pdf"))
    item = armor.calls[0][1].user_prompt_data.byte_item
    assert item.byte_data_type == ma.ByteDataItem.ByteItemType.PDF
    # raw bytes: the client library does the base64 encoding for the REST call
    assert item.byte_data == PDF


def test_text_and_attachment_are_scanned_in_separate_calls_and_merged(armor):
    armor.respond = lambda request: (
        _result({"pi_and_jailbreak": _pi()}) if request.user_prompt_data.byte_item.byte_data else _result({})
    )
    verdict = model_armor.scan("intake", text="hi", attachment=(PDF, "application/pdf"))
    assert len(armor.calls) == 2
    assert verdict.filters == ["pi_and_jailbreak"]


def test_oversize_attachment_is_a_scan_error_not_silently_skipped(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_MAX_FILE_BYTES", 100)
    verdict = model_armor.scan("intake", attachment=(PDF, "application/pdf"))
    assert armor.calls == [] and verdict.failed and "scan limit" in verdict.error and verdict.blocked


def test_image_attachments_are_not_scanned_by_default(armor):
    verdict = model_armor.scan("intake", text="hi", attachment=(PDF, "image/png"))
    assert len(armor.calls) == 1  # the text only
    assert any("image scanning is off" in note for note in verdict.notes)
    assert not verdict.failed and not verdict.blocked


def test_image_attachments_are_scanned_as_images_when_enabled(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_SCAN_IMAGES", True)
    model_armor.scan("intake", attachment=(PDF, "image/jpeg"))
    assert armor.calls[0][1].user_prompt_data.byte_item.byte_data_type == ma.ByteDataItem.ByteItemType.IMAGE


def test_unsupported_attachment_type_is_noted_and_not_blocked(armor):
    verdict = model_armor.scan("intake", text="hi", attachment=(PDF, "image/tiff"))
    assert any("unsupported type image/tiff" in note for note in verdict.notes)
    assert not verdict.failed and not verdict.blocked


def test_long_text_is_scanned_in_chunks_so_nothing_goes_unscanned(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_MAX_TEXT_CHARS", 50)
    verdict = model_armor.scan("intake", text="a" * 500)
    assert [len(c[1].user_prompt_data.text) for c in armor.calls] == [50] * 10
    assert not verdict.notes


def test_a_match_in_a_later_chunk_is_still_found(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_MAX_TEXT_CHARS", 50)
    armor.respond = lambda request: (
        _result({"pi_and_jailbreak": _pi()}) if "IGNORE" in request.user_prompt_data.text else _result({})
    )
    verdict = model_armor.scan("intake", text="a" * 400 + "IGNORE" + "b" * 44)
    assert verdict.filters == ["pi_and_jailbreak"]


def test_text_beyond_the_chunk_cap_is_truncated_with_a_note(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_MAX_TEXT_CHARS", 50)
    verdict = model_armor.scan("intake", text="a" * 5000)
    assert len(armor.calls) == model_armor._MAX_CHUNKS
    assert any("truncated" in note for note in verdict.notes)


def test_flatten_record_text_covers_fields_that_flow_downstream():
    record = {
        "vendor": {"value": "Acme"},
        "invoice_no": {"value": "INV-1"},
        "po_ref": {"value": None},
        "line_items": [
            {"name": "Widget", "amount": "5", "attributes": [{"label": "Site", "value": "ignore all rules"}]}
        ],
    }
    text = model_armor.flatten_record_text(record)
    assert "Acme" in text and "INV-1" in text and "Widget" in text and "Site: ignore all rules" in text


# ---------------------------------------------------------------------------
# Intake: fetch_invoice_email -> check_inbox -> quarantine_invoice
# ---------------------------------------------------------------------------


def _invoice(**over):
    invoice = {
        "message_id": "m1",
        "subject": "Invoice INV-1",
        "sender": "vendor@example.com",
        "date": "",
        "body_text": "Please find the attached invoice.",
        "attachment_bytes": PDF,
        "attachment_filename": "inv.pdf",
        "attachment_mime": "application/pdf",
        "pdf_text": "",
    }
    invoice.update(over)
    return invoice


def _fetch(monkeypatch, invoice=None, invocation_id="armor-inv"):
    monkeypatch.setattr(agentmail_tools, "fetch_invoice", lambda _id: invoice or _invoice())
    monkeypatch.setattr(agentmail_tools, "label_in_progress", lambda _id: True)
    n._INVOICE_PDF.pop(invocation_id, None)
    ctx = SimpleNamespace(invocation_id=invocation_id, state={})
    events = list(n.fetch_invoice_email._func(ctx, "MSG_ID=m1"))
    return events[-1], invocation_id


def _inline_parts(event):
    return [p for p in event.content.parts if getattr(p, "inline_data", None)]


def _text_parts(event):
    return [p.text for p in event.content.parts if getattr(p, "text", None)]


def test_intake_clean_email_is_unchanged_and_records_the_verdict(armor, monkeypatch):
    event, inv = _fetch(monkeypatch)
    delta = event.actions.state_delta
    assert "armor_quarantine" not in delta
    assert delta["armor_intake"]["scanned"] is True and delta["armor_intake"]["blocked"] is False
    assert len(_inline_parts(event)) == 1  # the PDF still reaches extraction
    assert inv in n._INVOICE_PDF
    n._INVOICE_PDF.pop(inv)


def test_intake_flagged_email_is_withheld_from_the_model_and_not_stashed(armor, monkeypatch):
    armor.respond = _flag_everything
    event, inv = _fetch(monkeypatch)
    delta = event.actions.state_delta
    assert delta["armor_quarantine"]["kind"] == "threat"
    assert _inline_parts(event) == []  # no PDF part
    assert not any("Email body" in t for t in _text_parts(event))  # no body part
    assert inv not in n._INVOICE_PDF  # extraction could never re-inject it
    assert delta["invoice_message_id"] == "m1"  # still labelled/handled by message id


def test_intake_scan_failure_fails_closed_as_a_retryable_scan_error(armor, monkeypatch):
    armor.respond = lambda request: (_ for _ in ()).throw(RuntimeError("down"))
    event, inv = _fetch(monkeypatch)
    assert event.actions.state_delta["armor_quarantine"]["kind"] == "scan_error"
    assert inv not in n._INVOICE_PDF


def test_intake_does_nothing_extra_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", False)
    event, inv = _fetch(monkeypatch)
    delta = event.actions.state_delta
    assert "armor_intake" not in delta and "armor_quarantine" not in delta
    n._INVOICE_PDF.pop(inv, None)


def test_check_inbox_routes_a_quarantined_email_before_anything_else():
    ctx = SimpleNamespace(state={"armor_quarantine": {"reason": "x", "kind": "threat"}, "invoice_email_raw": "ok"})
    assert n.check_inbox._func(ctx, None).actions.route == "quarantined"


def test_quarantine_threat_sets_a_terminal_status_without_echoing_content():
    ctx = SimpleNamespace(
        state={
            "armor_quarantine": {"reason": "Model Armor flagged: pi_and_jailbreak", "kind": "threat"},
            "armor_intake": {"filters": ["pi_and_jailbreak"]},
        }
    )
    (event,) = list(n.quarantine_invoice._func(ctx, None))
    assert event.actions.state_delta == {"pipeline_status": "quarantined"}
    assert "AP-Quarantined" in event.content.parts[0].text


def test_quarantine_scan_error_is_recorded_as_a_pipeline_error_for_retry():
    ctx = SimpleNamespace(
        state={"armor_quarantine": {"reason": "Model Armor could not scan the content: down", "kind": "scan_error"}}
    )
    (event,) = list(n.quarantine_invoice._func(ctx, None))
    delta = event.actions.state_delta
    assert delta["pipeline_error_node"] == "model_armor_scan" and "pipeline_status" not in delta


def _complete(monkeypatch, state):
    calls: dict[str, list] = {"quarantined": [], "error": [], "processed": [], "no_action": []}
    monkeypatch.setattr(n.agentmail_tools, "mark_quarantined", lambda m: calls["quarantined"].append(m))
    monkeypatch.setattr(n.agentmail_tools, "mark_error", lambda m: calls["error"].append(m))
    monkeypatch.setattr(n.agentmail_tools, "mark_processed", lambda m: calls["processed"].append(m))
    monkeypatch.setattr(n.agentmail_tools, "mark_no_action", lambda m: calls["no_action"].append(m))
    events = list(n.pipeline_complete._func(SimpleNamespace(state=state, resume_inputs={}), None))
    return calls, events


def test_pipeline_complete_labels_a_quarantined_email_and_keeps_it_out_of_retry(monkeypatch):
    calls, events = _complete(monkeypatch, {"invoice_message_id": "m1", "pipeline_status": "quarantined"})
    assert calls["quarantined"] == ["m1"] and not calls["processed"] and not calls["error"]
    assert "Quarantined by Model Armor" in events[0].content.parts[0].text
    assert agentmail_tools.is_handled([agentmail_tools.QUARANTINED_LABEL]) is True


def test_pipeline_complete_labels_a_scan_error_as_a_retryable_ap_error(monkeypatch):
    calls, _ = _complete(
        monkeypatch,
        {"invoice_message_id": "m1", "pipeline_error": "down", "pipeline_error_node": "model_armor_scan"},
    )
    assert calls["error"] == ["m1"] and not calls["quarantined"]


def test_mark_quarantined_swaps_the_in_progress_label(monkeypatch):
    updates: list[dict] = []

    class _Messages:
        def update(self, inbox_id, message_id, **kwargs):
            updates.append(kwargs)

    monkeypatch.setattr(agentmail_tools, "_client", SimpleNamespace(inboxes=SimpleNamespace(messages=_Messages())))
    monkeypatch.setattr(agentmail_tools, "AGENTMAIL_INBOX_ID", "ap@agentmail.to")
    assert agentmail_tools.mark_quarantined("m1") is True
    assert updates == [
        {"add_labels": [agentmail_tools.QUARANTINED_LABEL], "remove_labels": [agentmail_tools.IN_PROGRESS_LABEL]}
    ]


# ---------------------------------------------------------------------------
# Extraction output -> validation flag
# ---------------------------------------------------------------------------


def _fv(value):
    return {"value": value, "source_grounding": "grounded"}


RECORD = {
    "vendor": _fv("BluePeak Plastics"),
    "invoice_no": _fv("INV-2026-0301"),
    "po_ref": _fv("PO-2026-1003"),
    "total": _fv("3250.00"),
    "currency": _fv("USD"),
    "line_items": [{"name": "Resin", "amount": "3250.00", "attributes": [], "source_grounding": "grounded"}],
}
PO = {
    "po_number": "PO-2026-1003",
    "po_type": "direct",
    "route": "direct",
    "matched": True,
    "vendor_name": "BluePeak Plastics",
    "po_amount": 3250.0,
    "currency": "USD",
    "po_status": "open",
    "target_system": "SAP",
}


def test_extraction_scan_is_recorded_in_state(armor):
    ctx = SimpleNamespace(state={}, resume_inputs={})
    (event,) = list(n.emit_extraction_card._func(ctx, dict(RECORD)))
    verdict = event.actions.state_delta["armor_extraction"]
    assert verdict["scope"] == "extraction" and verdict["scanned"] is True and verdict["blocked"] is False
    assert "Resin" in armor.calls[0][1].user_prompt_data.text


def test_extraction_scan_never_runs_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", False)
    ctx = SimpleNamespace(state={}, resume_inputs={})
    (event,) = list(n.emit_extraction_card._func(ctx, dict(RECORD)))
    assert "armor_extraction" not in event.actions.state_delta


def _validate(state_extra):
    ctx = SimpleNamespace(state={"invoice_record": RECORD, "po_match": PO, **state_extra}, resume_inputs={})
    return n.validate_invoice._func(ctx, dict(RECORD))


def test_clean_invoice_still_proceeds_with_a_clean_armor_verdict():
    event = _validate({"armor_extraction": {"blocked": False, "filters": []}})
    assert event.actions.route == "proceed" and event.actions.state_delta["validation_flags"] == []


def test_blocked_extraction_scan_quarantines_an_otherwise_clean_invoice():
    event = _validate({"armor_extraction": {"blocked": True, "filters": ["pi_and_jailbreak"]}})
    assert event.actions.route == "quarantined"  # never reaches the approval gate
    quarantine = event.actions.state_delta["armor_quarantine"]
    assert quarantine["kind"] == "threat" and quarantine["stage"] == "extraction"
    assert quarantine["filters"] == ["pi_and_jailbreak"] and "pi_and_jailbreak" in quarantine["reason"]
    # none of the review machinery ran: no flags, rubric or policy snapshot to approve against
    assert "validation_flags" not in event.actions.state_delta
    assert "validation_rubric" not in event.actions.state_delta


def test_failed_extraction_scan_is_a_retryable_scan_error():
    event = _validate({"armor_extraction": {"blocked": True, "filters": [], "error": "down"}})
    assert event.actions.route == "quarantined"
    quarantine = event.actions.state_delta["armor_quarantine"]
    assert quarantine["kind"] == "scan_error" and "could not scan" in quarantine["reason"]


def test_quarantine_takes_precedence_over_a_duplicate():
    rec = dict(RECORD, invoice_no=_fv("INV-2024-001"))  # already in the seeded posted_invoices history
    ctx = SimpleNamespace(
        state={"invoice_record": rec, "po_match": PO, "armor_extraction": {"blocked": True, "filters": ["x"]}},
        resume_inputs={},
    )
    assert n.validate_invoice._func(ctx, rec).actions.route == "quarantined"


def test_an_unblocked_verdict_does_not_quarantine_even_when_a_filter_was_ignored():
    event = _validate({"armor_extraction": {"blocked": False, "filters": [], "ignored_filters": ["sdp"]}})
    assert event.actions.route == "proceed"


def test_extraction_stage_quarantine_card_says_the_model_had_read_it():
    ctx = SimpleNamespace(
        state={"armor_quarantine": {"reason": "Model Armor flagged: pi_and_jailbreak", "kind": "threat", "stage": "extraction", "filters": ["pi_and_jailbreak"]}}
    )
    (event,) = list(n.quarantine_invoice._func(ctx, None))
    text = event.content.parts[0].text
    assert "not** validated, sent for approval or posted" in text and "not** passed to any AI agent" not in text
    assert event.actions.state_delta == {"pipeline_status": "quarantined"}


def test_extraction_stage_scan_error_is_recorded_for_retry():
    ctx = SimpleNamespace(
        state={"armor_quarantine": {"reason": "Model Armor could not scan the content: down", "kind": "scan_error", "stage": "extraction", "filters": []}}
    )
    (event,) = list(n.quarantine_invoice._func(ctx, None))
    assert event.actions.state_delta["pipeline_error_node"] == "model_armor_scan"
    assert "content extracted from this invoice" in event.content.parts[0].text


def test_summary_describes_an_extraction_stage_quarantine(monkeypatch):
    _, events = _complete(
        monkeypatch,
        {
            "invoice_message_id": "m1",
            "pipeline_status": "quarantined",
            "armor_quarantine": {"kind": "threat", "stage": "extraction", "filters": ["pi_and_jailbreak"]},
        },
    )
    text = events[0].content.parts[0].text
    assert "pi_and_jailbreak" in text and "not validated, approved or posted" in text


def test_graph_quarantines_after_validation_instead_of_asking_for_approval():
    from app.agent import root_agent

    edges = {(e.from_node.name, e.to_node.name, getattr(e, "route", None)) for e in root_agent.graph.edges}
    assert ("validate_invoice", "quarantine_invoice", "quarantined") in edges
    assert ("validate_invoice", "recall_precedents", "human_review") in edges  # normal review path unchanged


# ---------------------------------------------------------------------------
# Outbound rejection email
# ---------------------------------------------------------------------------

_DRAFT = {"subject": "Invoice INV-1 needs correction", "body": "Dear Vendor, please resubmit."}


def _review(state):
    async def run():
        return [e async for e in n.review_email_draft._func(SimpleNamespace(state=state, resume_inputs={}), None)]

    return asyncio.run(run())


def _send(monkeypatch, state):
    sent: list = []

    async def fake_mcp(to, subject, body):
        sent.append((to, subject, body))
        return True

    monkeypatch.setattr(n.agentmail_tools, "send_email", lambda *a: sent.append(a) or True)
    monkeypatch.setattr("app.nodes.rejection_email.agentmail_mcp.send_email", fake_mcp)

    async def run():
        return [e async for e in n.send_rejection_email._func(SimpleNamespace(state=state, resume_inputs={}), None)]

    return asyncio.run(run()), sent


def test_review_card_warns_when_the_draft_is_flagged(armor):
    armor.respond = _flag_everything
    events = _review({"rejection_email_draft": dict(_DRAFT), "invoice_sender_email": "v@example.com"})
    text = events[0].content.parts[0].text
    assert "pi_and_jailbreak" in text and "still flagged" in text
    assert events[0].actions.state_delta["armor_rejection_email"]["blocked"] is True


def test_review_card_is_unchanged_when_the_draft_is_clean(armor):
    events = _review({"rejection_email_draft": dict(_DRAFT), "invoice_sender_email": "v@example.com"})
    assert "Model Armor" not in events[0].content.parts[0].text


def test_review_card_does_not_call_armor_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", False)
    events = _review({"rejection_email_draft": dict(_DRAFT), "invoice_sender_email": "v@example.com"})
    assert events[0].actions.state_delta in (None, {}) or "armor_rejection_email" not in events[0].actions.state_delta


def test_a_flagged_draft_is_not_sent(armor, monkeypatch):
    armor.respond = _flag_everything
    events, sent = _send(monkeypatch, {"rejection_email_draft": dict(_DRAFT), "invoice_sender_email": "v@example.com"})
    assert sent == []
    delta = events[-1].actions.state_delta
    assert delta["notification_result"]["email_sent"] is False
    assert delta["pipeline_status"] == "rejected" and "not sent" in delta["notification_result"]["summary"]
    assert NotificationResult(**delta["notification_result"]).email_sent is False
    assert delta["armor_rejection_email"]["blocked"] is True
    assert delta["armor_rejection_email"]["scope"] == "rejection_email_send"


def test_a_clean_draft_is_sent(armor, monkeypatch):
    events, sent = _send(monkeypatch, {"rejection_email_draft": dict(_DRAFT), "invoice_sender_email": "v@example.com"})
    assert len(sent) == 1 and events[-1].actions.state_delta["notification_result"]["email_sent"] is True


def test_email_review_surface_shows_the_notice_only_when_given():
    plain = build_email_review_surface(dict(_DRAFT), "v@example.com", 0)
    warned = build_email_review_surface(dict(_DRAFT), "v@example.com", 0, "careful")

    def components(messages):
        return next(m for m in messages if "updateComponents" in m)["updateComponents"]["components"]

    assert not any(c["id"] == "notice" for c in components(plain))
    comps = components(warned)
    root = next(c for c in comps if c["id"] == "root")
    assert any(c["id"] == "notice" and c["text"] == "careful" for c in comps)
    assert root["children"][:2] == ["title", "notice"]


# ---------------------------------------------------------------------------
# Graph wiring
# ---------------------------------------------------------------------------


def test_graph_routes_a_quarantined_email_to_a_terminal_node():
    from app.agent import root_agent

    edges = {(e.from_node.name, e.to_node.name, getattr(e, "route", None)) for e in root_agent.graph.edges}
    assert ("check_inbox", "quarantine_invoice", "quarantined") in edges
    assert ("quarantine_invoice", "pipeline_complete", None) in edges
    # the model-facing path is untouched
    assert ("check_inbox", "screening_agent", "invoice_found") in edges


# ---------------------------------------------------------------------------
# /config/security (lets the UI show the Model Armor steps before a run starts)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_security_config_reports_whether_model_armor_is_on(monkeypatch, enabled):
    from app.fast_api_app import get_security_config

    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", enabled)
    assert get_security_config()["model_armor"]["enabled"] is enabled


# ---------------------------------------------------------------------------
# Why a filter fired: findings (names / levels only, tagged with where found)
# ---------------------------------------------------------------------------


def _uri(*uris: str) -> ma.FilterResult:
    return ma.FilterResult(
        malicious_uri_filter_result=ma.MaliciousUriFilterResult(
            execution_state=_OK,
            match_state=_MATCH,
            malicious_uri_matched_items=[
                ma.MaliciousUriFilterResult.MaliciousUriMatchedItem(uri=u) for u in uris
            ],
        )
    )


def _sdp_findings(*types: tuple[str, ma.SdpFindingLikelihood]) -> ma.FilterResult:
    return ma.FilterResult(
        sdp_filter_result=ma.SdpFilterResult(
            inspect_result=ma.SdpInspectResult(
                execution_state=_OK,
                match_state=_MATCH,
                findings=[ma.SdpFinding(info_type=t, likelihood=lk) for t, lk in types],
            )
        )
    )


def test_prompt_injection_finding_carries_confidence_and_source(armor):
    pi = ma.FilterResult(
        pi_and_jailbreak_filter_result=ma.PiAndJailbreakFilterResult(
            execution_state=_OK, match_state=_MATCH, confidence_level=ma.DetectionConfidenceLevel.HIGH
        )
    )
    armor.respond = lambda request: _result({"pi_and_jailbreak": pi})
    verdict = model_armor.scan("intake", texts=["header", "body"], labels=["Email header and subject", "Email body"])
    # One entry per (filter, source): both texts matched, so both sources are named.
    assert [(f["filter"], f["source"], f["confidence"]) for f in verdict.findings] == [
        ("pi_and_jailbreak", "Email header and subject", "high"),
        ("pi_and_jailbreak", "Email body", "high"),
    ]
    assert verdict.to_state()["findings"][0]["ignored"] is False


def test_malicious_link_finding_lists_the_matched_urls_and_the_attachment_source(armor):
    armor.respond = lambda request: _result({"malicious_uri": _uri("http://evil.example/login")})
    verdict = model_armor.scan("intake", attachment=(PDF, "application/pdf"))
    assert verdict.findings == [
        {"filter": "malicious_uri", "uris": ["http://evil.example/login"], "source": "PDF attachment", "ignored": False}
    ]


def test_sensitive_data_finding_has_info_type_names_only_and_is_marked_ignored_inbound(armor):
    armor.respond = lambda request: _result(
        {"sdp": _sdp_findings(("BANK_ACCOUNT_NUMBER", ma.SdpFindingLikelihood.LIKELY),
                              ("BANK_ACCOUNT_NUMBER", ma.SdpFindingLikelihood.POSSIBLE),
                              ("EMAIL_ADDRESS", ma.SdpFindingLikelihood.VERY_LIKELY))}
    )
    verdict = model_armor.scan("intake", text="Acct 12345678 a@b.com", labels=["Email body"])
    (finding,) = verdict.findings
    assert finding["info_types"] == [
        {"name": "BANK_ACCOUNT_NUMBER", "likelihood": "likely"},  # highest likelihood wins
        {"name": "EMAIL_ADDRESS", "likelihood": "very_likely"},
    ]
    assert finding["ignored"] is True  # sdp is exempt for inbound content
    assert "12345678" not in repr(verdict.to_state())  # the values never ride along
    # ...but it is enforced outbound.
    out = model_armor.scan("rejection_email", direction="outbound", text="x", labels=["Rejection email draft"])
    assert out.findings[0]["ignored"] is False


def test_clean_scan_has_no_findings_and_unlabeled_scan_still_works(armor):
    armor.respond = _flag_everything
    verdict = model_armor.scan("intake", text="hello")  # no labels given
    assert verdict.findings == [{"filter": "pi_and_jailbreak", "ignored": False}]
    armor.respond = lambda request: _result({"pi_and_jailbreak": _pi(match=False)})
    assert model_armor.scan("intake", text="hello").findings == []


# ---------------------------------------------------------------------------
# Scan points (MODEL_ARMOR_SCOPES): the shipped default is the intake scan only
# ---------------------------------------------------------------------------


def test_default_scope_is_intake_only(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", True)
    monkeypatch.setattr(config, "MODEL_ARMOR_SCOPES", frozenset({"intake"}))
    assert model_armor.is_enabled("intake") and model_armor.is_enabled()
    assert not model_armor.is_enabled("extraction")
    assert not model_armor.is_enabled("rejection_email") and not model_armor.is_enabled("rejection_email_send")


def test_outbound_scope_switches_both_email_scans_together(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", True)
    monkeypatch.setattr(config, "MODEL_ARMOR_SCOPES", frozenset({"intake", "outbound"}))
    assert model_armor.is_enabled("rejection_email") and model_armor.is_enabled("rejection_email_send")
    assert not model_armor.is_enabled("extraction")


def test_a_scan_point_that_is_off_never_calls_model_armor_and_records_no_verdict(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_SCOPES", frozenset({"intake"}))
    armor.respond = _flag_everything  # would block if it were consulted
    verdict = model_armor.scan("rejection_email", direction="outbound", text="our limit is $4,000")
    assert not verdict.scanned and not verdict.blocked and armor.calls == []
    # ...so the extraction and email nodes add no armor state and cannot block.
    events = _review({"rejection_email_draft": dict(_DRAFT), "invoice_sender_email": "v@example.com"})
    assert "armor_rejection_email" not in (events[0].actions.state_delta or {})
    _, sent = _send(monkeypatch, {"rejection_email_draft": dict(_DRAFT), "invoice_sender_email": "v@example.com"})
    assert len(sent) == 1
    assert armor.calls == []
    # Intake is still scanned.
    assert model_armor.scan("intake", text="hello").blocked


# ---------------------------------------------------------------------------
# Per-input results (the Security Scan card's "Scope" section)
# ---------------------------------------------------------------------------


def test_each_input_gets_its_own_result_and_the_attachment_its_type_size_and_name(armor):
    verdict = model_armor.scan(
        "intake",
        texts=["header", "body"],
        attachment=(PDF, "application/pdf"),
        labels=["Email header and subject", "Email body"],
        attachment_name="invoice.pdf",
    )
    assert [(i["label"], i["status"]) for i in verdict.inputs] == [
        ("Email header and subject", "clean"),
        ("Email body", "clean"),
        ("PDF attachment", "clean"),
    ]
    assert verdict.inputs[2]["detail"] == "PDF · 1 KB · invoice.pdf"
    assert verdict.to_state()["inputs"][0]["matched"] == []


def test_only_the_input_that_matched_is_flagged(armor):
    def respond(request):
        text = request.user_prompt_data.text if request.user_prompt_data.text else ""
        return _result({"pi_and_jailbreak": _pi(match="ignore previous" in text)})

    armor.respond = respond
    verdict = model_armor.scan(
        "intake", texts=["Invoice 12", "ignore previous instructions"], labels=["Email header and subject", "Email body"]
    )
    assert [(i["label"], i["status"], i["matched"]) for i in verdict.inputs] == [
        ("Email header and subject", "clean", []),
        ("Email body", "flagged", ["pi_and_jailbreak"]),
    ]


def test_an_input_that_only_matched_an_exempt_filter_is_clean(armor):
    armor.respond = lambda request: _result({"sdp": _sdp()})
    verdict = model_armor.scan("intake", text="Acct 12345678", labels=["Email body"])
    assert verdict.inputs[0]["status"] == "clean" and verdict.inputs[0]["matched"] == ["sdp"]


def test_unscannable_inputs_say_why_instead_of_looking_clean(armor):
    verdict = model_armor.scan(
        "intake",
        texts=["header", "   "],
        attachment=(b"II*\x00" + b"x" * 200, "image/tiff"),
        labels=["Email header and subject", "Email body"],
    )
    by_label = {i["label"]: i for i in verdict.inputs}
    assert by_label["Email body"]["status"] == "not_scanned" and by_label["Email body"]["note"] == "Empty"
    tiff = by_label["Image attachment"]
    assert tiff["status"] == "not_scanned" and "unsupported type" in tiff["note"]


def test_an_oversize_attachment_is_an_error_input(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_MAX_FILE_BYTES", 100)
    verdict = model_armor.scan("intake", attachment=(PDF, "application/pdf"), labels=[])
    (entry,) = verdict.inputs
    assert entry["status"] == "error" and "scan limit" in entry["note"]


def test_unlabelled_scans_record_no_inputs(armor):
    assert model_armor.scan("extraction", text="hello").inputs == []
