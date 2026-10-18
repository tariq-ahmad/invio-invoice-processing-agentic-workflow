"""PII masking via Model Armor's de-identified text: the scan, the intake step that
records masked text for the LLMs, and the model-request swap.

No test calls Google Cloud: the Model Armor client is faked with real
``modelarmor_v1`` response objects, so the parsing runs against the actual types.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.cloud import modelarmor_v1 as ma
from google.genai import types as genai_types

from app import agent, agentmail_tools, config, model_armor
from app import nodes as n
from app.model_callbacks import _apply_masked_text

ACCOUNT = "000123456789"
BODY = f"Please pay invoice INV-2026-0901 (PO-2026-1003, total 3250.00). Account no.: {ACCOUNT}"
PDF = b"%PDF-1.4 " + b"x" * 200

_MATCH = ma.FilterMatchState.MATCH_FOUND
_NO_MATCH = ma.FilterMatchState.NO_MATCH_FOUND
_OK = ma.FilterExecutionState.EXECUTION_SUCCESS


# ---------------------------------------------------------------------------
# Fake client returning real response objects
# ---------------------------------------------------------------------------


def _deid_filter(masked: str | None, types: list[str] | None = None) -> ma.FilterResult:
    """Advanced SDP: the de-identify step ran (``masked`` None = nothing sensitive found)."""
    return ma.FilterResult(
        sdp_filter_result=ma.SdpFilterResult(
            deidentify_result=ma.SdpDeidentifyResult(
                execution_state=_OK,
                match_state=_MATCH if masked is not None else _NO_MATCH,
                data=ma.DataItem(text=masked or ""),
                info_types=types or [],
            )
        )
    )


def _inspect_only_filter(found: bool = False) -> ma.FilterResult:
    """SDP with only an inspect result. With nothing sensitive that is what even an
    advanced template returns (verified live); with ``found`` it is a basic-mode
    template that detected PII but has no de-identify step to mask it."""
    return ma.FilterResult(
        sdp_filter_result=ma.SdpFilterResult(
            inspect_result=ma.SdpInspectResult(execution_state=_OK, match_state=_MATCH if found else _NO_MATCH)
        )
    )


def _result(*filters: tuple[str, ma.FilterResult], invocation=ma.InvocationResult.SUCCESS):
    return ma.SanitizationResult(
        filter_match_state=_NO_MATCH,
        filter_results=dict(filters),
        invocation_result=invocation,
    )


def _item_of(request) -> ma.DataItem:
    if isinstance(request, ma.SanitizeUserPromptRequest):
        return request.user_prompt_data
    return request.model_response_data


class FakeClient:
    def __init__(self, respond):
        self.respond = respond
        self.calls: list = []

    def sanitize_user_prompt(self, request, timeout=None):
        self.calls.append(request)
        return ma.SanitizeUserPromptResponse(sanitization_result=self.respond(request))

    def sanitize_model_response(self, request, timeout=None):
        self.calls.append(request)
        return ma.SanitizeModelResponseResponse(sanitization_result=self.respond(request))


def _masking_respond(replace: dict[str, str]):
    """An advanced-SDP template: replaces each ``find`` with ``[label]`` in the text."""

    def respond(request):
        item = _item_of(request)
        text = "" if item.byte_item.byte_data else item.text
        if not text:  # an attachment call: a file gets no de-identification
            return _result(("sdp", _inspect_only_filter()))
        masked, types = text, []
        for find, label in replace.items():
            if find in masked:
                masked = masked.replace(find, f"[{label}]")
                types.append(label)
        if not types:  # nothing sensitive: the template returns only an inspect result
            return _result(("sdp", _inspect_only_filter()))
        return _result(("sdp", _deid_filter(masked, types)))

    return respond


@pytest.fixture
def armor(monkeypatch):
    """Model Armor + PII masking on, fail-closed, against an advanced-SDP fake template."""
    client = FakeClient(_masking_respond({ACCOUNT: "BANK_ACCOUNT_NUMBER"}))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", True)
    monkeypatch.setattr(config, "MODEL_ARMOR_ENFORCE", True)
    monkeypatch.setattr(config, "MODEL_ARMOR_FAIL_CLOSED", True)
    monkeypatch.setattr(config, "PII_MASKING_ENABLED", True)
    monkeypatch.setattr(config, "MODEL_ARMOR_SCOPES", frozenset({"intake", "extraction", "outbound"}))
    monkeypatch.setattr(model_armor, "_get_client", lambda: client)
    return client


# ---------------------------------------------------------------------------
# scan(mask=True)
# ---------------------------------------------------------------------------


def test_masking_needs_both_flags(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", True)
    monkeypatch.setattr(config, "PII_MASKING_ENABLED", False)
    assert not model_armor.masking_enabled()
    monkeypatch.setattr(config, "PII_MASKING_ENABLED", True)
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", False)
    assert not model_armor.masking_enabled()
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", True)
    assert model_armor.masking_enabled()


def test_the_templates_deidentified_text_is_returned_per_input_text(armor):
    verdict = model_armor.scan("intake", texts=["header", BODY], mask=True)
    assert verdict.masked_texts == ["header", BODY.replace(ACCOUNT, "[BANK_ACCOUNT_NUMBER]")]
    assert verdict.masked_info_types == ["BANK_ACCOUNT_NUMBER"]
    assert not verdict.failed and not verdict.blocked


def test_text_with_nothing_sensitive_comes_back_unchanged(armor):
    verdict = model_armor.scan("intake", texts=["Please find the attached invoice."], mask=True)
    assert verdict.masked_texts == ["Please find the attached invoice."] and not verdict.masked_info_types


def test_an_sdp_match_in_advanced_mode_is_still_an_sdp_match(armor):
    """Advanced mode reports the match on deidentify_result, not inspect_result."""
    inbound = model_armor.scan("intake", text=BODY, mask=True)
    assert inbound.ignored_filters == ["sdp"] and not inbound.matched and not inbound.blocked  # recorded, not blocked
    outbound = model_armor.scan("rejection_email", direction="outbound", text=BODY)
    assert outbound.filters == ["sdp"] and outbound.blocked  # outbound still blocks


def test_text_with_nothing_sensitive_returns_only_an_inspect_result_and_is_not_an_error(armor):
    """Verified live: a clean text gets no de-identify result. That means 'clean', not 'broken'."""
    verdict = model_armor.scan("intake", texts=["Please find the attached invoice."], mask=True)
    assert not verdict.failed and verdict.masked_texts == ["Please find the attached invoice."]


def test_pii_found_but_not_deidentified_is_an_error_not_a_silent_passthrough(armor):
    """A basic-mode template detects the PII but has nothing to mask it with."""
    armor.respond = lambda request: _result(("sdp", _inspect_only_filter(found=True)))
    verdict = model_armor.scan("intake", texts=[BODY], mask=True)
    assert verdict.masked_texts == [None]
    assert verdict.failed and "no de-identified text" in verdict.error and "roles/dlp.user" in verdict.error
    assert verdict.blocked  # fail-closed holds the email


def test_a_template_with_no_sdp_filter_cannot_be_trusted_to_mask(armor):
    armor.respond = lambda request: _result(("pi_and_jailbreak", ma.FilterResult(
        pi_and_jailbreak_filter_result=ma.PiAndJailbreakFilterResult(execution_state=_OK, match_state=_NO_MATCH))))
    verdict = model_armor.scan("intake", texts=[BODY], mask=True)
    assert verdict.failed and verdict.masked_texts == [None]


def test_a_basic_template_is_fine_when_masking_is_not_requested(armor):
    armor.respond = lambda request: _result(("sdp", _inspect_only_filter(found=True)))
    verdict = model_armor.scan("intake", texts=[BODY], mask=False)
    assert not verdict.failed and verdict.masked_texts == [None]


def test_a_failed_deidentify_step_is_an_error(armor):
    failed = ma.FilterResult(
        sdp_filter_result=ma.SdpFilterResult(
            deidentify_result=ma.SdpDeidentifyResult(
                execution_state=ma.FilterExecutionState.EXECUTION_SKIPPED, match_state=_NO_MATCH
            )
        )
    )
    armor.respond = lambda request: _result(("sdp", failed))
    verdict = model_armor.scan("intake", texts=[BODY], mask=True)
    assert verdict.failed and verdict.masked_texts == [None]


def test_a_match_with_no_returned_text_is_an_error(armor):
    armor.respond = lambda request: _result(("sdp", _deid_filter("", ["IBAN_CODE"])))  # match, empty text
    armor.respond = lambda request: _result(
        (
            "sdp",
            ma.FilterResult(
                sdp_filter_result=ma.SdpFilterResult(
                    deidentify_result=ma.SdpDeidentifyResult(execution_state=_OK, match_state=_MATCH, info_types=["IBAN_CODE"])
                )
            ),
        )
    )
    verdict = model_armor.scan("intake", texts=[BODY], mask=True)
    assert verdict.failed and verdict.masked_texts == [None]


def test_the_attachment_is_scanned_but_never_needs_a_masked_result(armor):
    verdict = model_armor.scan("intake", texts=[BODY], attachment=(PDF, "application/pdf"), mask=True)
    assert len(armor.calls) == 2  # the text and the file
    assert not verdict.failed  # a file call returns no de-identified text, and that is fine


def test_long_text_is_masked_chunk_by_chunk_and_reassembled(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_MAX_TEXT_CHARS", 60)
    text = "\n".join(f"row {i} account {ACCOUNT}" for i in range(8))
    verdict = model_armor.scan("intake", text=text, mask=True)
    assert len(armor.calls) > 1
    assert verdict.masked_texts == [text.replace(ACCOUNT, "[BANK_ACCOUNT_NUMBER]")]


def test_text_too_long_to_mask_is_an_error_not_a_partial_mask(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_MAX_TEXT_CHARS", 10)
    verdict = model_armor.scan("intake", text="x" * 1000, mask=True)
    assert verdict.masked_texts == [None] and "too long to mask" in verdict.error
    assert armor.calls == []  # nothing sent: a prefix-only mask would leak the rest


def test_empty_texts_keep_their_position(armor):
    verdict = model_armor.scan("intake", texts=["", BODY], mask=True)
    assert verdict.masked_texts == ["", BODY.replace(ACCOUNT, "[BANK_ACCOUNT_NUMBER]")]


# ---------------------------------------------------------------------------
# Intake: fetch_invoice_email records masked text, leaves the original event alone
# ---------------------------------------------------------------------------


def _invoice(**over):
    invoice = {
        "message_id": "m1",
        "subject": "Invoice INV-2026-0901",
        "sender": "dana@bluepeak.com",
        "date": "",
        "body_text": BODY,
        "attachment_bytes": PDF,
        "attachment_filename": "inv.pdf",
        "attachment_mime": "application/pdf",
        "pdf_text": "",
    }
    invoice.update(over)
    return invoice


def _fetch(monkeypatch, invocation_id="pii-inv"):
    monkeypatch.setattr(agentmail_tools, "fetch_invoice", lambda _id: _invoice())
    monkeypatch.setattr(agentmail_tools, "label_in_progress", lambda _id: True)
    n._INVOICE_PDF.pop(invocation_id, None)
    n._MASKED_TEXT.pop(invocation_id, None)
    ctx = SimpleNamespace(invocation_id=invocation_id, state={})
    return list(n.fetch_invoice_email._func(ctx, "MSG_ID=m1"))[-1], invocation_id


def _cleanup(inv):
    n._MASKED_TEXT.pop(inv, None)
    n._INVOICE_PDF.pop(inv, None)


def _texts(event):
    return [p.text for p in event.content.parts if getattr(p, "text", None)]


def test_intake_records_masked_text_but_the_event_the_ui_shows_stays_original(armor, monkeypatch):
    event, inv = _fetch(monkeypatch)
    assert any(ACCOUNT in t for t in _texts(event))  # the event (UI / session) is NOT masked
    mapping = n._MASKED_TEXT[inv]
    assert len(mapping) == 1  # only the body carries the account number
    ((original, masked),) = mapping.items()
    assert ACCOUNT in original and ACCOUNT not in masked and "[BANK_ACCOUNT_NUMBER]" in masked
    _cleanup(inv)


def test_intake_state_records_info_type_names_never_values(armor, monkeypatch):
    event, inv = _fetch(monkeypatch)
    state = event.actions.state_delta
    assert state["pii_masking"]["masked"] is True and state["pii_masking"]["info_types"] == ["BANK_ACCOUNT_NUMBER"]
    assert ACCOUNT not in repr(state["pii_masking"]) and ACCOUNT not in repr(state["armor_intake"])
    assert "armor_quarantine" not in state
    _cleanup(inv)


def test_the_reply_address_and_pdf_are_untouched(armor, monkeypatch):
    event, inv = _fetch(monkeypatch)
    assert event.actions.state_delta["invoice_sender_email"] == "dana@bluepeak.com"
    assert any(getattr(p, "inline_data", None) for p in event.content.parts)  # the attachment still goes to extraction
    assert n._INVOICE_PDF[inv][0] == PDF
    _cleanup(inv)


def test_an_unconfigured_template_holds_the_email_when_fail_closed(armor, monkeypatch):
    armor.respond = lambda request: _result(("sdp", _inspect_only_filter(found=True)))
    event, inv = _fetch(monkeypatch)
    quarantine = event.actions.state_delta["armor_quarantine"]
    assert quarantine["kind"] == "scan_error" and "no de-identified text" in quarantine["reason"]
    assert inv not in n._MASKED_TEXT and inv not in n._INVOICE_PDF  # nothing for an LLM to read
    assert not any(getattr(p, "inline_data", None) for p in event.content.parts)
    assert not any("Email body" in t for t in _texts(event))


def test_an_unconfigured_template_sends_unmasked_text_when_fail_open(armor, monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_FAIL_CLOSED", False)
    armor.respond = lambda request: _result(("sdp", _inspect_only_filter(found=True)))
    event, inv = _fetch(monkeypatch)
    state = event.actions.state_delta
    assert "armor_quarantine" not in state and state["pii_masking"]["error"]
    assert inv not in n._MASKED_TEXT  # nothing masked, so the LLM reads the original
    _cleanup(inv)


def test_a_threat_takes_precedence_over_masking(armor, monkeypatch):
    armor.respond = lambda request: _result(
        ("pi_and_jailbreak", ma.FilterResult(pi_and_jailbreak_filter_result=ma.PiAndJailbreakFilterResult(execution_state=_OK, match_state=_MATCH))),
        ("sdp", _deid_filter(None)),
    )
    event, inv = _fetch(monkeypatch)
    assert event.actions.state_delta["armor_quarantine"]["kind"] == "threat"
    assert inv not in n._MASKED_TEXT
    _cleanup(inv)


def test_with_masking_off_the_llm_texts_are_not_masked_and_no_deidentify_is_needed(armor, monkeypatch):
    monkeypatch.setattr(config, "PII_MASKING_ENABLED", False)
    armor.respond = lambda request: _result(("sdp", _inspect_only_filter(found=True)))  # a basic template is fine
    event, inv = _fetch(monkeypatch)
    state = event.actions.state_delta
    assert "pii_masking" not in state and "armor_quarantine" not in state and inv not in n._MASKED_TEXT
    _cleanup(inv)


def test_intake_does_nothing_extra_when_model_armor_is_off(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ARMOR_ENABLED", False)
    monkeypatch.setattr(config, "PII_MASKING_ENABLED", True)
    event, inv = _fetch(monkeypatch)
    assert "pii_masking" not in event.actions.state_delta and inv not in n._MASKED_TEXT
    _cleanup(inv)


def test_the_hold_card_says_it_could_not_scan_or_mask():
    ctx = SimpleNamespace(
        state={
            "armor_quarantine": {
                "reason": "Model Armor could not scan the content: no de-identified text",
                "kind": "scan_error",
                "stage": "intake",
                "filters": [],
            }
        }
    )
    (event,) = list(n.quarantine_invoice._func(ctx, None))
    assert "scan or mask" in event.content.parts[0].text
    assert event.actions.state_delta["pipeline_error_node"] == "model_armor_scan"  # AP-Error, retried


# ---------------------------------------------------------------------------
# The model request: the LLM reads the masked text
# ---------------------------------------------------------------------------


def _request(*texts):
    return SimpleNamespace(
        contents=[genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=t) for t in texts])]
    )


def _request_texts(req):
    return [p.text for c in req.contents for p in c.parts if getattr(p, "text", None)]


def test_masked_text_is_swapped_into_the_request_and_other_parts_are_kept():
    inv = "swap-1"
    n._MASKED_TEXT[inv] = {"original body": "masked body"}
    req = _request("original body", "unrelated instruction")
    original_content = req.contents[0]
    _apply_masked_text(SimpleNamespace(invocation_id=inv), req)
    assert _request_texts(req) == ["masked body", "unrelated instruction"]
    assert original_content.parts[0].text == "original body"  # shared session objects are not mutated
    n._MASKED_TEXT.pop(inv)


def test_binary_parts_and_unrelated_contents_pass_through():
    inv = "swap-2"
    n._MASKED_TEXT[inv] = {"x": "y"}
    pdf = genai_types.Part.from_bytes(data=PDF, mime_type="application/pdf")
    req = SimpleNamespace(
        contents=[
            genai_types.Content(role="user", parts=[genai_types.Part.from_text(text="x"), pdf]),
            genai_types.Content(role="user", parts=[genai_types.Part.from_text(text="other")]),
        ]
    )
    other = req.contents[1]
    _apply_masked_text(SimpleNamespace(invocation_id=inv), req)
    assert req.contents[0].parts[0].text == "y" and req.contents[0].parts[1] is pdf and req.contents[1] is other
    n._MASKED_TEXT.pop(inv)


def test_no_mapping_means_no_change():
    req = _request("original body")
    _apply_masked_text(SimpleNamespace(invocation_id="swap-none"), req)
    assert _request_texts(req) == ["original body"]


@pytest.mark.parametrize("cb", ["_screening_before", "_extraction_before"])
def test_the_screening_and_extraction_agents_read_the_masked_text(cb):
    inv = "swap-agent"
    n._MASKED_TEXT[inv] = {"original body": "masked body"}
    n._INVOICE_PDF.pop(inv, None)
    req = _request("original body")
    getattr(agent, cb)(SimpleNamespace(invocation_id=inv, state={}), req)
    texts = _request_texts(req)
    assert "masked body" in texts and "original body" not in texts
    n._MASKED_TEXT.pop(inv)


def test_the_rejection_email_agent_is_not_touched():
    inv = "swap-draft"
    n._MASKED_TEXT[inv] = {"original body": "masked body"}
    req = _request("original body")
    agent._draft_before(SimpleNamespace(invocation_id=inv, state={}), req)
    assert _request_texts(req) == ["original body"]
    n._MASKED_TEXT.pop(inv)


def test_pipeline_complete_drops_the_runs_masked_text(monkeypatch):
    inv = "swap-complete"
    n._MASKED_TEXT[inv] = {"a": "b"}
    monkeypatch.setattr(n.agentmail_tools, "mark_processed", lambda m: True)
    ctx = SimpleNamespace(
        invocation_id=inv, state={"invoice_message_id": "m1", "pipeline_status": "posted_erp"}, resume_inputs={}
    )
    list(n.pipeline_complete._func(ctx, None))
    assert inv not in n._MASKED_TEXT
