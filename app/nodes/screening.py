"""Screening nodes (after screening_agent)."""

import logging
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

from app.a2ui_views import a2ui_enabled, build_screening_surface
from app.app_utils.tracing import enrich_node_span
from app.nodes._helpers import _brand_footer, _content_msg, _content_with_a2ui
from app.policy import get_policy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 3a. screen_email_signal  (router after screening_agent)
#     Routes -> "invoice" | "not_invoice"
# ---------------------------------------------------------------------------


def route_screening_assessment(assessment: dict[str, Any]) -> str:
    """Deterministic proceed/discard policy over the screening evidence.

    The model rates each surface signal; this policy — not the model — owns the
    decision. Only at least ``screening_min_strong_signals`` (policy, default
    two) independently strong signals constitute
    sufficient evidence of a genuine invoice — a single strong signal is too
    easy to spoof or coincidental in isolation (e.g. an invoice-sounding
    filename alone). Weak (generic/ambiguous) signals never independently
    justify the cost of extraction, however many are present: two or more weak
    cues do not corroborate each other the way two strong cues do. Downstream
    validation and the HITL amount gate remain the backstop for anything that
    does proceed.
    """
    strengths = (
        assessment.get("attachment_signal", "absent"),
        assessment.get("body_signal", "absent"),
        assessment.get("subject_signal", "absent"),
    )
    strong = sum(s == "strong" for s in strengths)
    return "invoice" if strong >= get_policy().screening_min_strong_signals else "not_invoice"


@node(name="screen_email_signal")
def screen_email_signal(ctx: Context, node_input: Any) -> Event:
    """Route on the screening_agent's InvoiceScreeningAssessment.

    Reads the assessment from node_input (the agent's structured output) with a
    fallback to state['screening_assessment']. Emits a markdown screening card
    and persists the assessment to state so downstream nodes can inspect it.
    """
    assessment = (
        node_input if isinstance(node_input, dict) else (ctx.state.get("screening_assessment") or {})
    )

    signals = assessment.get("observed_signals") or []
    rationale = assessment.get("assessment_rationale", "")

    route = route_screening_assessment(assessment)

    # The screening agent's job is evidence only — it never decides. The card
    # therefore shows signal strengths, not a verdict; the deterministic
    # "Screening Decision" step (screen_email_signal's own route output,
    # rendered downstream) is the sole place the Invoice/Not-an-invoice
    # decision is displayed.
    signals_block = (
        "\n".join(f"- {s}" for s in signals) if signals else "*(no signals detected)*"
    )
    rationale_block = f"\n\n**Rationale:** {rationale}" if rationale else ""

    summary = (
        "## Email Screening\n\n"
        f"The screening agent reviewed the email and attached document.\n\n"
        f"| Signal | Strength |\n"
        f"|---|---|\n"
        f"| Attachment | {assessment.get('attachment_signal', 'absent')} |\n"
        f"| Body | {assessment.get('body_signal', 'absent')} |\n"
        f"| Subject | {assessment.get('subject_signal', 'absent')} |\n\n"
        f"**Detected signals:**\n\n"
        f"{signals_block}"
        f"{rationale_block}"
    )

    logger.info(
        "screen_email_signal route=%s attachment=%s body=%s subject=%s",
        route,
        assessment.get("attachment_signal", "absent"),
        assessment.get("body_signal", "absent"),
        assessment.get("subject_signal", "absent"),
    )
    with enrich_node_span("screen_email_signal") as span:
        span.set("decision.route", route)
        span.set("signal.attachment", assessment.get("attachment_signal", "absent"))
        span.set("signal.body", assessment.get("body_signal", "absent"))
        span.set("signal.subject", assessment.get("subject_signal", "absent"))

    messages = build_screening_surface(assessment) if a2ui_enabled() else None
    content = _content_with_a2ui(summary, messages) if messages else _content_msg(summary)
    # Persist the computed route so downstream consumers (the summary node and the
    # SPA) read the authoritative decision instead of re-deriving the policy.
    return Event(
        content=content,
        route=route,  # ty: ignore[unknown-argument]
        state={"screening_assessment": assessment, "screening_route": route},  # ty: ignore[unknown-argument]
    )


# ---------------------------------------------------------------------------
# 3b. discard_non_invoice  (terminal; email is not an invoice)
# ---------------------------------------------------------------------------


@node(name="discard_non_invoice")
def discard_non_invoice(ctx: Context, node_input: Any):  # type: ignore[return]
    """Inform the user that this email was screened out as a non-invoice."""
    assessment = ctx.state.get("screening_assessment") or {}
    rationale = assessment.get("assessment_rationale", "")
    reason_note = f"\n\n**Screening note:** {rationale}" if rationale else ""

    msg = (
        "## Email Screened — Not an Invoice\n\n"
        "The screening agent reviewed this email and determined it does not contain "
        "a vendor invoice. No AP processing has been performed.\n\n"
        "If this is a genuine invoice, please ensure the email includes a PDF or image "
        "attachment with clear invoice signals (invoice number, amount due, vendor name) and "
        "resubmit it to the accounts payable inbox."
        f"{reason_note}\n\n"
        f"{_brand_footer()}"
    )
    logger.info("discard_non_invoice: email discarded as non-invoice")
    with enrich_node_span("discard_non_invoice") as span:
        span.set("pipeline.status", "discarded")
    yield Event(
        content=_content_msg(msg),
        state={"pipeline_status": "discarded"},  # ty: ignore[unknown-argument]
    )
