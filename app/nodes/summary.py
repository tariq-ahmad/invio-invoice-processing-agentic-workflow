"""Terminal node: final chat summary, cost accounting, and AgentMail labeling."""

import logging
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

from app import agentmail_tools
from app.app_utils.tracing import enrich_node_span
from app.cost_calculator import STATIC_ESTIMATES, compute_all_model_costs
from app.models import MODEL_SCREENING
from app.nodes._helpers import _HR, _brand_footer, _content_msg, _now_display
from app.nodes._state import _MASKED_TEXT
from app.run_stats import record_run_outcome

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 11. pipeline_complete  (terminal; emits content for web UI)
# ---------------------------------------------------------------------------


_OUTCOME_HEADLINES = {
    "posted_erp": "Posted to ERP",
    "escalated": "Escalated to ServiceNow",
    "rejected": "Rejected. Vendor has been notified.",
    "discarded": "Screened out — not an invoice. No action taken.",
    "quarantined": "Quarantined by Model Armor. Nothing was processed.",
    "error": "Processing failed. Nothing was posted.",
}

# Terminal outcomes that mean the pipeline finished its job. Anything else that
# arrives with a recorded pipeline_error is a failed run.
_COMPLETED_STATUSES = frozenset(
    {"posted_erp", "escalated", "rejected", "discarded", "quarantined"}
)


def _run_failed(state: Any) -> bool:
    """True when a node errored and the run never reached a terminal outcome."""
    return bool(state.get("pipeline_error")) and state.get("pipeline_status") not in _COMPLETED_STATUSES


def _build_summary(ctx: Context) -> str:
    failed = _run_failed(ctx.state)
    status = "error" if failed else ctx.state.get("pipeline_status", "unknown")

    record_raw = ctx.state.get("invoice_record", {})
    inv_no = (record_raw.get("invoice_no") or {}).get("value", "—")
    vendor = (record_raw.get("vendor") or {}).get("value", "—")
    total = (record_raw.get("total") or {}).get("value", "—")
    currency = (record_raw.get("currency") or {}).get("value", "")
    amount = f"{currency} {total}".strip() if total != "—" else "—"

    po_match = ctx.state.get("po_match", {})
    headline = _OUTCOME_HEADLINES.get(status, "Processing complete.")

    lines = [
        "## Processing Complete\n",
        f"**{headline}**\n",
    ]
    if status not in ("discarded", "quarantined"):
        lines += [
            "| Field | Value |",
            "|---|---|",
            f"| Invoice # | `{inv_no}` |",
            f"| Vendor | {vendor} |",
            f"| Amount | **{amount}** |",
        ]
    if po_match.get("po_number"):
        route = (po_match.get("po_type") or po_match.get("route") or "").title()
        cat = po_match.get("category")
        lines.append(
            f"| PO / Spend | `{po_match['po_number']}` · {route}"
            + (f" · {cat}" if cat else "")
            + " |"
        )
    lines.append("")

    if status == "posted_erp":
        posting = ctx.state.get("posting_result", {})
        lines += [
            "### Posting confirmation",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Document | `{posting.get('document_id', 'N/A')}` |",
            f"| Target system | ERP |",
            f"| Posted at | {posting.get('posted_at', 'N/A')} |",
            f"| Reference | {posting.get('reference', 'N/A')} |",
        ]
    elif status == "escalated":
        esc = ctx.state.get("escalation_result", {})
        lines += [
            "### Escalation",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Ticket | `{esc.get('ticket_id', 'N/A')}` |",
            f"| System | {esc.get('system', 'ServiceNow')} |",
            f"| Priority | {esc.get('priority', 'N/A')} |",
            f"| Assigned to | {esc.get('assignment_group', 'N/A')} |",
            f"| Reason | {esc.get('reason', '')} |",
        ]
    elif status == "error":
        lines += [
            "### What went wrong",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Failed step | `{ctx.state.get('pipeline_error_node', 'unknown')}` |",
            f"| Error | {ctx.state.get('pipeline_error')} |",
            "",
            "The email was labelled **AP-Error** and will be picked up again on the next "
            "catch-up (or when intake is resumed). Nothing was posted.",
        ]
    elif status == "quarantined":
        quarantine = ctx.state.get("armor_quarantine") or {}
        filters = ", ".join(quarantine.get("filters") or []) or "n/a"
        if quarantine.get("stage") == "extraction":
            flagged = (
                "Model Armor flagged text the extraction model read out of this invoice, so it "
                "was not validated, approved or posted."
            )
        else:
            flagged = "Model Armor flagged this email, so its content was never passed to an AI agent."
        lines += [
            "### Security screening",
            "",
            f"{flagged} It was labelled **AP-Quarantined** and left in the inbox for manual review.",
            "",
            f"**Filters matched:** {filters}",
        ]
    elif status == "discarded":
        assessment = ctx.state.get("screening_assessment", {})
        reasoning = assessment.get("assessment_rationale", "")
        lines += [
            "### Screening verdict",
            "",
            "The screening agent determined this email is not a vendor invoice; "
            "no extraction, validation, or vendor notification was performed.",
        ]
        if reasoning:
            lines += ["", f"**Agent reasoning:** {reasoning}"]
    else:
        notif = ctx.state.get("notification_result", {})
        lines += [
            "### Vendor notification",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| Email delivered | {'Yes' if notif.get('email_sent') else 'No (queued)'} |",
            f"| Recipient | {notif.get('recipient', 'N/A')} |",
            f"| Subject | {notif.get('subject', 'N/A')} |",
        ]

    flags = ctx.state.get("validation_flags", [])
    if flags:
        lines += ["", "### Why this invoice needed review", ""]
        lines += [f"- {f}" for f in flags]

    lines += [
        "",
        _HR,
        f"*Completed {_now_display()}*  ",
        _brand_footer(),
    ]
    return "\n".join(lines)


def _build_cost_summary(ctx: Context) -> dict:
    """Build per-agent token counts and cross-model cost projections for the UI."""
    agents_data: dict = {}
    total_in = 0
    total_out = 0
    total_think = 0

    # Screening — real token counts from the _screening_after callback
    screening_in = ctx.state.get("screening_input_tokens")
    screening_out = ctx.state.get("screening_output_tokens")
    screening_think = int(ctx.state.get("screening_thinking_tokens") or 0)
    estimated_screening = screening_in is None
    if estimated_screening:
        screening_in, screening_out = STATIC_ESTIMATES["screening"]
        screening_think = 0  # no static estimate for thinking depth
    agents_data["screening"] = {
        "label": "Screening",
        "input_tokens": screening_in,
        "output_tokens": screening_out,
        "thinking_tokens": screening_think,
        "costs": compute_all_model_costs(screening_in, screening_out, screening_think),
        "estimated": estimated_screening,
    }
    total_in += screening_in
    total_out += screening_out
    total_think += screening_think

    # Extraction — real token counts when available
    ext_in = ctx.state.get("extraction_input_tokens")
    ext_out = ctx.state.get("extraction_output_tokens")
    ext_think = int(ctx.state.get("extraction_thinking_tokens") or 0)
    estimated_ext = ext_in is None
    if estimated_ext:
        ext_in, ext_out = STATIC_ESTIMATES["extraction"]
        ext_think = 0
    agents_data["extraction"] = {
        "label": "Invoice Extraction",
        "input_tokens": ext_in,
        "output_tokens": ext_out,
        "thinking_tokens": ext_think,
        "costs": compute_all_model_costs(ext_in, ext_out, ext_think),
        "estimated": estimated_ext,
    }
    total_in += ext_in
    total_out += ext_out
    total_think += ext_think

    # Draft email — only on rejection paths; use real tokens when available
    draft_in = ctx.state.get("draft_email_input_tokens")
    draft_out = ctx.state.get("draft_email_output_tokens")
    draft_think = int(ctx.state.get("draft_email_thinking_tokens") or 0)
    if draft_in is not None or ctx.state.get("rejection_email_draft"):
        estimated_draft = draft_in is None
        if estimated_draft:
            draft_in, draft_out = STATIC_ESTIMATES["draft_email"]
            draft_think = 0
        agents_data["draft_email"] = {
            "label": "Draft Rejection Email",
            "input_tokens": draft_in,
            "output_tokens": draft_out,
            "thinking_tokens": draft_think,
            "costs": compute_all_model_costs(draft_in, draft_out, draft_think),
            "estimated": estimated_draft,
        }
        total_in += draft_in
        total_out += draft_out
        total_think += draft_think

    # The two approval-card briefings (app/review_summary.py): counted only when a
    # model actually wrote them, with the tokens that call reported.
    for key, label in (("review_summary", "Reviewer Briefing"), ("memory_summary", "Agent Memory Note")):
        call = ctx.state.get(key) or {}
        if call.get("source") != "llm":
            continue
        c_in = int(call.get("input_tokens") or 0)
        c_out = int(call.get("output_tokens") or 0)
        c_think = int(call.get("thinking_tokens") or 0)
        agents_data[key] = {
            "label": label,
            "input_tokens": c_in,
            "output_tokens": c_out,
            "thinking_tokens": c_think,
            "costs": compute_all_model_costs(c_in, c_out, c_think),
            "estimated": False,
        }
        total_in += c_in
        total_out += c_out
        total_think += c_think

    return {
        "agents": agents_data,
        "totals": {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "thinking_tokens": total_think,
            "costs": compute_all_model_costs(total_in, total_out, total_think),
        },
        "current_model": ctx.state.get("screening_model")
            or ctx.state.get("extraction_model", MODEL_SCREENING),
    }


@node(name="pipeline_complete")
def pipeline_complete(ctx: Context, node_input: Any):  # type: ignore[return]
    """Emit the final chat summary and close the pipeline turn."""
    summary = _build_summary(ctx)
    cost_summary = _build_cost_summary(ctx)
    failed = _run_failed(ctx.state)
    status = "error" if failed else ctx.state.get("pipeline_status")
    logger.info("pipeline_complete pipeline_status=%s", status)

    # Stamp the final AgentMail label, replacing the interim AP-InProgress: a
    # screened-out non-invoice becomes AP-NoAction, a failed run becomes AP-Error
    # (so it is retried rather than silently treated as done), anything the
    # pipeline acted on becomes AP-Processed. Best-effort — a labeling failure
    # never breaks the turn.
    # Drop this run's PII-masked text (it is keyed by the unmasked originals).
    _MASKED_TEXT.pop(getattr(ctx, "invocation_id", None), None)
    msg_id = ctx.state.get("invoice_message_id")
    if msg_id:
        if status == "discarded":
            agentmail_tools.mark_no_action(msg_id)
        elif status == "quarantined":
            agentmail_tools.mark_quarantined(msg_id)
        elif failed:
            agentmail_tools.mark_error(msg_id)
        else:
            agentmail_tools.mark_processed(msg_id)
    with enrich_node_span("pipeline_complete") as span:
        span.set("pipeline.status", status or "unknown")
        if failed:
            span.set("pipeline.error_node", ctx.state.get("pipeline_error_node", "unknown"))
    # One outcome document per finished run feeds the UI's stats (GET /stats).
    # Best-effort: never raises, so a Firestore hiccup can't fail the invoice.
    record_run_outcome(ctx.state, status=status, run_id=getattr(ctx, "invocation_id", None))
    # Emit cost_summary as an explicit state delta (the frontend only ingests
    # stateDelta, not implicit ctx.state mutations). A failed run also surfaces
    # pipeline_status="error" so the UI shows the failure outcome card.
    delta: dict = {"cost_summary": cost_summary}
    if failed:
        delta["pipeline_status"] = "error"
    yield Event(
        content=_content_msg(summary),
        state=delta,  # ty: ignore[unknown-argument]
    )
    yield Event(output={"status": status})
