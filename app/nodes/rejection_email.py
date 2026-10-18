"""Rejection-email path: grounding brief, HITL review loop, and send."""

import asyncio
import logging
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import node

from app import agentmail_mcp, agentmail_tools, model_armor
from app.a2ui_views import a2ui_enabled, build_email_review_surface
from app.app_utils.tracing import enrich_node_span
from app.config import AGENTMAIL_SEND_VIA_MCP, VENDOR_FALLBACK_EMAIL
from app.nodes._helpers import _HR, _content_msg, _content_with_a2ui, _normalize_resume
from app.schemas import NotificationResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 6a. prepare_rejection_brief  (grounds email_draft_agent with exact invoice data)
# ---------------------------------------------------------------------------

_EMAIL_INTERNAL_PATTERNS = (
    "human approval required",
    "hitl threshold",
    "ap threshold",
)


@node(name="prepare_rejection_brief")
def prepare_rejection_brief(ctx: Context, node_input: Any) -> str:
    """Build an explicit, grounded prompt for email_draft_agent.

    Passes exact invoice values to prevent hallucination, and includes the
    previous draft + feedback on revision rounds so the model revises rather
    than rewrites from scratch.
    """
    record = ctx.state.get("invoice_record") or {}
    flags = ctx.state.get("validation_flags") or []
    previous_draft = ctx.state.get("rejection_email_draft")
    feedback = ctx.state.get("email_feedback")

    vendor = (record.get("vendor") or {}).get("value") or "Vendor"
    inv_no = (record.get("invoice_no") or {}).get("value") or "UNKNOWN"
    po_ref = (record.get("po_ref") or {}).get("value") or ""
    total = (record.get("total") or {}).get("value") or "N/A"
    currency = (record.get("currency") or {}).get("value") or "USD"

    # Fallback chain: invoice_no → po_ref → amount-only (last resort).
    if inv_no and inv_no != "UNKNOWN":
        ref_lines = f"  Invoice no.  : {inv_no}"
        if po_ref:
            ref_lines += f"\n  PO Reference : {po_ref}"
    elif po_ref:
        ref_lines = f"  PO Reference : {po_ref}"
    else:
        ref_lines = f"  Invoice total: {currency} {total} (invoice number not legible — request vendor reference on reply)"

    clean_flags = [
        f for f in flags
        if not any(p in f.lower() for p in _EMAIL_INTERNAL_PATTERNS)
    ] or ["The invoice could not be validated at this time."]

    flag_lines = "\n".join(f"- {f}" for f in clean_flags)

    prompt = (
        "INVOICE DETAILS — use these exact values, do not invent or alter them:\n"
        f"  Vendor name  : {vendor}\n"
        f"{ref_lines}\n"
        f"  Total        : {currency} {total}\n\n"
        f"REJECTION REASONS:\n{flag_lines}\n"
    )

    if previous_draft and feedback:
        prev_subj = previous_draft.get("subject", "") if isinstance(previous_draft, dict) else ""
        prev_body = previous_draft.get("body", "") if isinstance(previous_draft, dict) else ""
        prompt += (
            f"\nPREVIOUS DRAFT — revise this, do not start from scratch:\n"
            f"[Previous subject — do NOT echo into body: {prev_subj}]\n\n"
            f"{prev_body}\n\n"
            f"HUMAN FEEDBACK — apply exactly as requested, change only what is asked:\n"
            f"{feedback}\n\n"
            "TASK: Return a revised version of the draft above that incorporates "
            "the feedback. Keep all unchanged sections verbatim."
        )
    else:
        prompt += (
            "\nTASK: Write a professional, polite rejection email to this vendor "
            "based on the rejection reasons above."
        )

    return prompt


# ---------------------------------------------------------------------------
# 6b. review_email_draft  (HITL feedback loop for rejection email draft)
#     Routes -> "send" | "revise" (cycle back to email_draft_agent)
# ---------------------------------------------------------------------------


@node(name="review_email_draft", rerun_on_resume=True)
async def review_email_draft(ctx: Context, node_input: Any):
    """Show the LLM-drafted rejection email to AP staff; route on send or feedback."""
    review_count = ctx.state.get("email_review_count", 0)
    interrupt_id = f"email_review_{review_count}"  # unique per loop iteration

    if interrupt_id not in ctx.resume_inputs:
        draft = ctx.state.get("rejection_email_draft") or {}
        subject = draft.get("subject", "N/A") if isinstance(draft, dict) else "N/A"
        body = draft.get("body", "N/A") if isinstance(draft, dict) else "N/A"
        recipient = ctx.state.get("invoice_sender_email") or VENDOR_FALLBACK_EMAIL

        iteration_note = (
            f"\n\n> *Revision {review_count} — updated based on your feedback.*"
            if review_count > 0 else ""
        )

        # Model Armor checks the outbound draft (sensitive-data leaks, malicious
        # links, unsafe content) before a human sees it, so the warning is on the
        # card; send_rejection_email re-checks whatever the human finally sends.
        armor = await model_armor.ascan(
            "rejection_email", direction="outbound", text=f"{subject}\n\n{body}", labels=["Rejection email draft"]
        )
        armor_notice = None
        if armor.blocked:
            armor_notice = (
                f"⚠️ {armor.reason()}. Edit the draft or ask for a revision — "
                "it will not be sent if it is still flagged when you send it."
            )
            iteration_note += f"\n\n> {armor_notice}"

        msg = (
            "## Rejection Email Draft\n\n"
            f"**To:** {recipient}  \n"
            f"**Subject:** {subject}"
            f"{iteration_note}\n\n"
            f"{_HR}\n\n"
            f"{body}\n\n"
            f"{_HR}\n\n"
            "Reply **`send`** to send this email, or type feedback to revise it."
        )
        draft_for_ui = draft if isinstance(draft, dict) else {"subject": subject, "body": body}
        messages = (
            build_email_review_surface(draft_for_ui, recipient, review_count, armor_notice)
            if a2ui_enabled()
            else None
        )
        with enrich_node_span("review_email_draft") as span:
            span.set("hitl.review_count", review_count)
            span.event("hitl.email_review", {"interrupt_id": interrupt_id, "review_count": review_count})
            if armor.scanned or armor.failed:
                span.set("armor.matched", armor.matched)
                span.set("armor.blocked", armor.blocked)
        review_event: dict[str, Any] = {"content": _content_with_a2ui(msg, messages)}
        if model_armor.is_enabled("rejection_email"):
            review_event["state"] = {"armor_rejection_email": armor.to_state()}
        yield Event(**review_event)
        yield RequestInput(interrupt_id=interrupt_id, message="Send or provide feedback to revise:")
        return

    # Resume may be a bare string (free-text feedback or "send") or a structured
    # A2UI action payload (dict). Structured "send" carries the human-edited
    # subject/body, which we persist before sending; structured "revise" carries
    # feedback text for the next draft loop.
    _SEND_WORDS = frozenset(["send", "yes", "y", "ok", "okay", "looks good", "approve", "approved", "good", "send it"])
    raw = _normalize_resume(ctx.resume_inputs.get(interrupt_id, ""))

    if isinstance(raw, dict):
        action = str(raw.get("decision") or raw.get("action") or "").lower().strip()
        feedback = str(raw.get("feedback") or "").strip()
        is_send = action in _SEND_WORDS or (not feedback and not action)

        if is_send:
            # Persist any human edits to the draft before sending.
            edited = {}
            if raw.get("subject") is not None:
                edited["subject"] = str(raw.get("subject"))
            if raw.get("body") is not None:
                edited["body"] = str(raw.get("body"))
            logger.info("review_email_draft: send (structured, edited=%s)", list(edited))
            with enrich_node_span("review_email_draft") as span:
                span.set("decision.route", "send")
                span.set("hitl.review_count", review_count)
            if edited:
                existing = ctx.state.get("rejection_email_draft") or {}
                merged = {**existing, **edited} if isinstance(existing, dict) else edited
                yield Event(route="send", state={"rejection_email_draft": merged})  # ty: ignore[unknown-argument]
            else:
                yield Event(route="send")  # ty: ignore[unknown-argument]
            return

        logger.info("review_email_draft: revise (structured, count=%d)", review_count)
        with enrich_node_span("review_email_draft") as span:
            span.set("decision.route", "revise")
            span.set("hitl.review_count", review_count)
        # Persist any hand-edited subject/body before looping so the revision
        # builds on the human's current text, not the last model draft.
        edited = {}
        if raw.get("subject") is not None:
            edited["subject"] = str(raw.get("subject"))
        if raw.get("body") is not None:
            edited["body"] = str(raw.get("body"))
        revise_state: dict[str, Any] = {
            "email_feedback": feedback or "Please revise the email.",
            "email_review_count": review_count + 1,
        }
        if edited:
            existing = ctx.state.get("rejection_email_draft") or {}
            revise_state["rejection_email_draft"] = (
                {**existing, **edited} if isinstance(existing, dict) else edited
            )
        yield Event(
            route="revise",  # ty: ignore[unknown-argument]
            state=revise_state,  # ty: ignore[unknown-argument]
        )
        return

    decision = str(raw).strip()
    first_word = decision.lower().split()[0] if decision.split() else ""

    if first_word in _SEND_WORDS or decision.lower() in _SEND_WORDS:
        logger.info("review_email_draft: approved for sending (count=%d)", review_count)
        with enrich_node_span("review_email_draft") as span:
            span.set("decision.route", "send")
            span.set("hitl.review_count", review_count)
        yield Event(route="send")  # ty: ignore[unknown-argument]
    else:
        logger.info("review_email_draft: feedback received, revising (count=%d)", review_count)
        with enrich_node_span("review_email_draft") as span:
            span.set("decision.route", "revise")
            span.set("hitl.review_count", review_count)
        yield Event(
            route="revise",  # ty: ignore[unknown-argument]
            state={  # ty: ignore[unknown-argument]
                "email_feedback": decision,
                "email_review_count": review_count + 1,
            },
        )


# ---------------------------------------------------------------------------
# 10. send_rejection_email  (sends the approved draft via AgentMail)
# ---------------------------------------------------------------------------


@node(name="send_rejection_email")
async def send_rejection_email(ctx: Context, node_input: Any):  # type: ignore[return]
    """Send the human-approved rejection email draft to the invoice sender."""
    draft = ctx.state.get("rejection_email_draft") or {}
    subject = draft.get("subject", "Invoice Rejected") if isinstance(draft, dict) else "Invoice Rejected"
    body = draft.get("body", "") if isinstance(draft, dict) else ""
    recipient = ctx.state.get("invoice_sender_email") or VENDOR_FALLBACK_EMAIL
    record = ctx.state.get("invoice_record") or {}
    inv_no = (record.get("invoice_no") or {}).get("value") or "UNKNOWN"
    po_ref = (record.get("po_ref") or {}).get("value") or ""

    # Build a human-readable audit reference: prefer invoice number, then PO,
    # then note that no identifier was recoverable (so AP staff can investigate).
    if inv_no != "UNKNOWN":
        audit_ref = inv_no
        if po_ref:
            audit_ref += f" / PO {po_ref}"
    elif po_ref:
        audit_ref = f"PO {po_ref} (invoice number not legible)"
    else:
        audit_ref = "no invoice or PO number found on document"

    # Last gate before anything leaves the building: scan the FINAL text, which
    # may have been hand-edited since the review card was scanned.
    armor = await model_armor.ascan(
        "rejection_email_send", direction="outbound", text=f"{subject}\n\n{body}", labels=["Final email text"]
    )
    if armor.blocked:
        logger.warning("send_rejection_email: blocked by Model Armor: %s", armor.reason())
        blocked = NotificationResult(
            email_sent=False,
            recipient=recipient,
            subject=subject,
            summary=(
                f"📧 Rejection email for invoice `{audit_ref}` was **not sent**: "
                f"{armor.reason()}. The draft is recorded for audit; AP staff should "
                "contact the vendor directly."
            ),
        )
        with enrich_node_span("send_rejection_email") as span:
            span.set("posting.system", "agentmail")
            span.set("posting.status", "blocked")
            span.set("armor.filters", ",".join(armor.filters))
            span.set("invoice.ref", audit_ref)
        yield Event(
            content=_content_msg(blocked.summary),
            state={  # ty: ignore[unknown-argument]
                "notification_result": blocked.model_dump(),
                "pipeline_status": "rejected",
                "armor_rejection_email": armor.to_state(),
            },
        )
        return

    sent = False
    if AGENTMAIL_SEND_VIA_MCP:
        sent = await agentmail_mcp.send_email(recipient, subject, body)
        if not sent:
            logger.warning("send_rejection_email: MCP send failed, falling back to SDK")
    if not sent:
        sent = await asyncio.to_thread(agentmail_tools.send_email, recipient, subject, body)
    if sent:
        summary = (
            f"📧 **Rejection email sent** to {recipient} regarding invoice "
            f"`{audit_ref}`. The vendor has been asked to correct and resubmit."
        )
    else:
        summary = (
            f"📧 Rejection email for invoice `{audit_ref}` was prepared for "
            f"{recipient} but could not be delivered (mailbox unavailable in this "
            "environment). The drafted message is recorded for audit."
        )
    result = NotificationResult(
        email_sent=sent,
        recipient=recipient,
        subject=subject,
        summary=summary,
    )
    logger.info("send_rejection_email: sent=%s recipient=%s ref=%s", sent, recipient, audit_ref)

    with enrich_node_span("send_rejection_email") as span:
        span.set("posting.system", "agentmail")
        span.set("posting.status", "sent" if sent else "queued")
        span.set("invoice.ref", audit_ref)
        span.set_verbose("email.recipient", recipient)

    yield Event(
        content=_content_msg(result.summary),
        state={  # ty: ignore[unknown-argument]
            "notification_result": result.model_dump(),
            "pipeline_status": "rejected",
        },
    )
