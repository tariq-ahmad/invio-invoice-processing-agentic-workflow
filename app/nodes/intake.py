"""AgentMail intake + inbox gating nodes."""

import logging
from typing import Any
from urllib.parse import unquote

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node
from google.genai import types as genai_types

from app import agentmail_tools, model_armor
from app.app_utils.tracing import enrich_node_span
from app.config import AGENTMAIL_INBOX_ID, PRODUCT_NAME
from app.nodes._helpers import _MSG_ID_RE, _brand_footer, _content_msg, _extract_text
from app.nodes._state import _INVOICE_PDF, _MASKED_TEXT
from app.policy import get_policy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 3b. check_inbox  (gate between intake_agent and extraction_agent)
# ---------------------------------------------------------------------------

_NO_EMAIL_SIGNAL = "NO_INVOICE_FOUND"

_ERROR_PATTERNS = (
    "cannot fulfill",
    "does not have permission",
    "permission denied",
    "access denied",
    "caller does not have",
    "not authorized",
    "unauthorized",
    "failed to retrieve",
    "unable to access",
    "malformed function call",
    "default_api.",
    "print(",
    "error:",
)


@node(name="check_inbox")
def check_inbox(ctx: Context, node_input: Any) -> Event:
    """Halt the pipeline if intake_agent found no invoice email or returned an error."""
    # Model Armor flagged (or could not scan) the email at intake: nothing was
    # handed to the model, so stop before screening/extraction ever run.
    if ctx.state.get("armor_quarantine"):
        logger.info("check_inbox: Model Armor quarantine, halting pipeline")
        with enrich_node_span("check_inbox") as span:
            span.set("decision.route", "quarantined")
        return Event(route="quarantined")  # ty: ignore[unknown-argument]

    raw = str(ctx.state.get("invoice_email_raw", "") or "")

    if _NO_EMAIL_SIGNAL in raw.upper():
        logger.info("check_inbox: NO_INVOICE_FOUND signal, halting pipeline")
        with enrich_node_span("check_inbox") as span:
            span.set("decision.route", "invoice_not_found")
        return Event(route="invoice_not_found")  # ty: ignore[unknown-argument]

    raw_lower = raw.lower()
    matched = next((p for p in _ERROR_PATTERNS if p in raw_lower), None)
    if matched:
        logger.info(
            "check_inbox: intake_agent error detected (%r), halting pipeline", matched
        )
        with enrich_node_span("check_inbox") as span:
            span.set("decision.route", "invoice_not_found")
            span.event("intake.error_detected", {"pattern": matched})
        return Event(route="invoice_not_found")  # ty: ignore[unknown-argument]

    logger.info("check_inbox: invoice email present, proceeding to extraction")
    with enrich_node_span("check_inbox") as span:
        span.set("decision.route", "invoice_found")
    return Event(route="invoice_found")  # ty: ignore[unknown-argument]


# ---------------------------------------------------------------------------
# 3c. inbox_empty  (terminal; inbox was empty)
# ---------------------------------------------------------------------------


@node(name="inbox_empty")
def inbox_empty(ctx: Context, node_input: Any):  # type: ignore[return]
    """Inform the user that no invoice email was found in the mailbox."""
    mailbox = f"**{AGENTMAIL_INBOX_ID}**" if AGENTMAIL_INBOX_ID else "the monitored mailbox"
    msg = (
        "## No Invoice to Process\n\n"
        f"I checked {mailbox} and found no unprocessed invoice "
        "email in the monitored window.\n\n"
        "Send an invoice email with a PDF or image attachment to that mailbox, then start a "
        "new session and I'll pick it up.\n\n"
        f"{_brand_footer()}"
    )
    logger.info("inbox_empty: inbox empty, pipeline halted")
    yield Event(content=_content_msg(msg))


# ---------------------------------------------------------------------------
# 3d. quarantine_invoice  (terminal; Model Armor stopped the email / its content)
# ---------------------------------------------------------------------------


@node(name="quarantine_invoice")
def quarantine_invoice(ctx: Context, node_input: Any):  # type: ignore[return]
    """Stop an invoice Model Armor flagged, before it can be approved or posted.

    Reached from two places, recorded in ``state['armor_quarantine']['stage']``:
      - ``intake`` (``check_inbox``): the email/attachment was flagged before any
        model saw it.
      - ``extraction`` (``validate_invoice``): the model had read the document, but
        the text it extracted was flagged, so none of its values can be trusted.

    Two kinds, in either stage:
      - ``threat``: a filter matched (e.g. prompt injection). The email is labelled
        AP-Quarantined so it leaves the retry loop and waits in the inbox for AP
        staff.
      - ``scan_error``: Model Armor could not give a verdict and MODEL_ARMOR_FAIL_CLOSED
        is on. Recorded as a pipeline error so ``pipeline_complete`` labels the
        message AP-Error and the next catch-up retries it.
    """
    quarantine = ctx.state.get("armor_quarantine") or {}
    reason = quarantine.get("reason") or "Model Armor stopped this invoice."
    scan_error = quarantine.get("kind") == "scan_error"
    extraction_stage = quarantine.get("stage") == "extraction"

    if scan_error:
        target = "content extracted from this invoice" if extraction_stage else "this email"
        what = (
            f"Model Armor could not scan or mask {target}, and the pipeline is configured to "
            "stop rather than process unchecked content."
        )
        msg = (
            "## Invoice Held — Security Check Unavailable\n\n"
            f"{what} Nothing was approved or posted.\n\n"
            f"**Detail:** {reason}\n\n"
            "The email will be retried on the next intake catch-up.\n\n"
            f"{_brand_footer()}"
        )
        state: dict[str, Any] = {
            "pipeline_error": reason,
            "pipeline_error_node": "model_armor_scan",
        }
    else:
        if extraction_stage:
            what = (
                "Model Armor flagged text the extraction model read out of this invoice, so "
                "its values can't be trusted. It was **not** validated, sent for approval or "
                "posted."
            )
        else:
            what = (
                "Model Armor flagged this email or its attachment, so its content was **not** "
                "passed to any AI agent. Nothing was extracted, validated or posted."
            )
        msg = (
            "## Invoice Quarantined — Security Screening\n\n"
            f"{what}\n\n"
            f"**{reason}**\n\n"
            "The email has been labelled **AP-Quarantined** and left in the inbox for the "
            "accounts payable team to review manually. If it is a genuine invoice, ask the "
            "vendor to resend a clean copy.\n\n"
            f"{_brand_footer()}"
        )
        state = {"pipeline_status": "quarantined"}

    logger.warning(
        "quarantine_invoice: %s (kind=%s stage=%s)",
        reason,
        quarantine.get("kind"),
        quarantine.get("stage", "intake"),
    )
    with enrich_node_span("quarantine_invoice") as span:
        span.set("quarantine.kind", quarantine.get("kind", "threat"))
        span.set("quarantine.stage", quarantine.get("stage", "intake"))
        span.set("quarantine.filters", ",".join(quarantine.get("filters") or []))
        span.set("pipeline.status", state.get("pipeline_status", "error"))
    yield Event(
        content=_content_msg(msg),
        state=state,  # ty: ignore[unknown-argument]
    )


# ---------------------------------------------------------------------------
# 3. fetch_invoice_email  (deterministic AgentMail intake; replaces intake_agent)
# ---------------------------------------------------------------------------


@node(name="fetch_invoice_email")
def fetch_invoice_email(ctx: Context, node_input: Any):  # type: ignore[return]
    """Fetch the latest invoice email + PDF via AgentMail and hand it to the
    extraction model.

    Emits a user-role content event carrying an email summary and the raw PDF
    bytes as inline data, so the downstream extraction LlmAgent (Gemini) reads
    the document natively and handles varied vendor formats / data drift.
    Records presence in state['invoice_email_raw'] for check_inbox to gate on.
    """
    # Check if a specific AgentMail message_id was threaded in by the SSE auto-trigger.
    # autoStart() in usePipelineStore embeds "MSG_ID=<url-encoded id>" in the run text.
    node_text = _extract_text(node_input) if node_input else ""
    m = _MSG_ID_RE.search(node_text)
    forced_msg_id = unquote(m.group(1)) if m else None

    if forced_msg_id:
        # SSE-triggered: fetch the exact message identified by the push handler.
        try:
            invoice = agentmail_tools.fetch_invoice(forced_msg_id)
            msg_id = forced_msg_id
            logger.info(
                "fetch_invoice_email: fetching forced message_id=%s", forced_msg_id
            )
        except Exception as exc:
            logger.error(
                "fetch_invoice_email: forced fetch failed for %s: %s", forced_msg_id, exc
            )
            with enrich_node_span("fetch_invoice_email") as span:
                span.set("agentmail.message_id", forced_msg_id)
                span.error(exc)
                span.event("intake.no_invoice_found", {"cause": "forced_fetch_failed"})
            # The message was claimed (AP-InProgress) before this fetch; hand it back
            # as AP-Error so it is retried instead of staying stuck in progress.
            agentmail_tools.mark_error(forced_msg_id)
            error_text = f"ERROR: {exc}\n\n{_NO_EMAIL_SIGNAL}"
            yield Event(
                content=_content_msg(error_text),
                state={"invoice_email_raw": error_text},  # ty: ignore[unknown-argument]
            )
            return
    else:
        try:
            msg_id = agentmail_tools.search_latest_invoice()
        except Exception as exc:
            logger.error("fetch_invoice_email: AgentMail search failed: %s", exc)
            with enrich_node_span("fetch_invoice_email") as span:
                span.error(exc)
                span.event("intake.no_invoice_found", {"cause": "search_failed"})
            error_text = f"ERROR: {exc}\n\n{_NO_EMAIL_SIGNAL}"
            yield Event(
                content=_content_msg(error_text),
                state={"invoice_email_raw": error_text},  # ty: ignore[unknown-argument]
            )
            return

        if not msg_id:
            logger.info("fetch_invoice_email: no invoice email found")
            with enrich_node_span("fetch_invoice_email") as span:
                span.event("intake.no_invoice_found", {"cause": "search_empty"})
            yield Event(
                content=_content_msg(_NO_EMAIL_SIGNAL),
                state={"invoice_email_raw": _NO_EMAIL_SIGNAL},  # ty: ignore[unknown-argument]
            )
            return

        invoice = agentmail_tools.fetch_invoice(msg_id)

    has_pdf = bool(invoice["attachment_bytes"])
    body_text = (invoice["body_text"] or "").strip()

    # Human-readable display summary (no raw PDF text dump)
    body_display = body_text if body_text else "*(no message body)*"
    source = "AgentMail · live inbox"

    def _display_summary(held: bool) -> str:
        if held:
            pdf_status = "**Content withheld from the AI agents** — held by security screening."
        elif has_pdf:
            pdf_status = "Attachment confirmed and queued for extraction."
        else:
            pdf_status = "No PDF or image attachment detected on this email."
        return (
            "## Invoice Received\n\n"
            f"An invoice has arrived in the accounts payable inbox and is now being processed through the {PRODUCT_NAME}.\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            f"| Subject | {invoice['subject'] or 'N/A'} |\n"
            f"| From | {invoice['sender'] or 'N/A'} |\n"
            f"| Received | {invoice['date'] or 'N/A'} |\n"
            f"| Attachment | {invoice['attachment_filename'] or 'None'} |\n"
            f"| Source | {source} |\n\n"
            f"**Attachment status:** {pdf_status}"
        )

    display_summary = _display_summary(False)
    # The email body is a separate part: screening weighs it as a signal, extraction
    # uses it as supplementary context. Kept out of the display_summary card so the
    # intake UI stays a clean header table.
    body_part_text = f"--- Email body ---\n{body_display}"
    llm_texts = [display_summary, body_part_text]  # exactly what the LLMs are shown of the email

    # Model Armor screens everything the sender controls BEFORE any model sees it:
    # the text the LLMs will read (this header summary carries the subject) and the
    # attachment. When it blocks, the body and attachment are withheld from the
    # content event below and the PDF is not stashed, so no downstream agent can
    # read them.
    #
    # PII masking rides on the same calls: with PII_MASKING_ENABLED the template's
    # de-identified text comes back for each text and is used for the LLM request
    # (model_callbacks._apply_masked_text, from this exact-string map). The UI,
    # session state and reply address keep the original. A template that returns no
    # de-identified text is an error (fail-closed holds the email), never a silent
    # pass-through of raw PII. The attachment itself cannot be masked.
    mask = model_armor.masking_enabled()
    armor = model_armor.scan(
        "intake",
        texts=llm_texts,
        attachment=(invoice["attachment_bytes"], invoice["attachment_mime"]) if has_pdf else None,
        mask=mask,
        labels=["Email header and subject", "Email body"],
        attachment_name=invoice["attachment_filename"] or None,
    )
    quarantined = armor.blocked
    if quarantined:
        display_summary = _display_summary(True)

    mask_map: dict[str, str] = {}
    pii_state: dict[str, Any] | None = None
    if mask:
        if armor.failed and not quarantined:
            logger.warning("fetch_invoice_email: PII masking failed, sending UNMASKED text (%s)", armor.error)
        mask_map = {
            original: masked
            for original, masked in zip(llm_texts, armor.masked_texts, strict=False)
            if masked is not None and masked != original
        }
        pii_state = {  # info-type names only, never the values
            "masked": bool(mask_map),
            "info_types": list(armor.masked_info_types),
            "error": armor.error,
            "latency_ms": armor.latency_ms or 0,
        }

    parts = [genai_types.Part.from_text(text=display_summary)]
    if not quarantined:
        parts.append(genai_types.Part.from_text(text=body_part_text))
    if invoice["attachment_bytes"] and not quarantined:
        parts.append(
            genai_types.Part.from_bytes(
                data=invoice["attachment_bytes"], mime_type=invoice["attachment_mime"]
            )
        )

    labeled = agentmail_tools.label_in_progress(msg_id)
    logger.info(
        "fetch_invoice_email: msg=%s attachment=%s mime=%s labeled=%s",
        msg_id,
        bool(invoice["attachment_bytes"]),
        invoice["attachment_mime"] or "none",
        labeled,
    )

    with enrich_node_span("fetch_invoice_email") as span:
        span.set("agentmail.message_id", msg_id)
        span.set("invoice.has_attachment", has_pdf)
        span.set("agentmail.labeled", labeled)
        if armor.scanned or armor.failed:
            span.set("armor.matched", armor.matched)
            span.set("armor.blocked", quarantined)
            span.set("armor.filters", ",".join(armor.filters))
            span.set("armor.latency_ms", armor.latency_ms or 0)

    # Stash the attachment for the extraction agent to re-inject (see _INVOICE_PDF note).
    if invoice["attachment_bytes"] and not quarantined:
        if len(_INVOICE_PDF) > 32:  # guard against leaks from crashed runs
            _INVOICE_PDF.clear()
        _INVOICE_PDF[ctx.invocation_id] = (invoice["attachment_bytes"], invoice["attachment_mime"])

    if mask_map and not quarantined:
        if len(_MASKED_TEXT) > 32:  # guard against leaks from crashed runs
            _MASKED_TEXT.clear()
        _MASKED_TEXT[ctx.invocation_id] = mask_map

    state: dict[str, Any] = {
        "invoice_email_raw": display_summary,
        "invoice_message_id": msg_id,
        "invoice_sender_email": invoice["sender"],
        # Policy values in force at the start of the run, so the UI can label
        # them (screening minimum, required fields) from Firestore rather than
        # hard-coded text. validate_invoice re-stamps the decision-time copy.
        "policy_snapshot": get_policy().audit(),
    }
    if model_armor.is_enabled():
        state["armor_intake"] = armor.to_state()
    if pii_state is not None:
        state["pii_masking"] = pii_state
    if quarantined:
        # check_inbox routes on this; quarantine_invoice reads it. A failed scan
        # with nothing matched is retryable (AP-Error); a real match is not.
        state["armor_quarantine"] = {
            "reason": armor.reason(),
            "kind": "threat" if armor.matched else "scan_error",
            "stage": "intake",
            "filters": list(armor.filters),
        }

    yield Event(
        content=genai_types.Content(role="user", parts=parts),
        state=state,  # ty: ignore[unknown-argument]
    )
