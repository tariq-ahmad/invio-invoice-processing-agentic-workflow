"""Human-in-the-loop approval gate."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import node

from app import memory
from app.a2ui_views import a2ui_enabled, build_approval_surface
from app.app_utils.tracing import enrich_node_span
from app.nodes._helpers import _HR, _content_with_a2ui, _normalize_resume
from app.policy import get_policy
from app.review_summary import generate_memory_summary, generate_review_summary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 6. human_approval_gate  (HITL; rerun_on_resume=True)
#    Routes -> "approve" | "reject"
# ---------------------------------------------------------------------------


_APPROVE_WORDS = frozenset(["approve", "approved", "yes", "y", "ok", "okay", "accept", "proceed", "confirm"])


async def _noop() -> None:
    """Placeholder for a briefing that is already in state (keeps gather positional)."""
    return None


def _po_row(po_match: dict | None) -> str:
    """Table row describing the PO the invoice matched (empty when no lookup ran)."""
    if not po_match:
        return ""
    if not po_match.get("matched"):
        route = (po_match.get("route") or "direct").title()
        return f"| PO match | No match in the PO ledger - defaults to {route} spend |\n"
    bits = [
        f"{(po_match.get('po_type') or po_match.get('route') or '').title()} -> {po_match.get('target_system') or ''}".strip(),
        po_match.get("vendor_name"),
        f"{po_match.get('currency') or ''} {po_match['po_amount']:,.2f}".strip()
        if po_match.get("po_amount") is not None
        else None,
        po_match.get("po_status"),
    ]
    return f"| PO match | {' · '.join(b for b in bits if b)} |\n"


# Structured reason codes a reviewer picks from (mirrored in
# frontend/src/a2ui/ApprovalReview.tsx's REASONS — keep in sync). A bare
# approve/reject carries no rationale, so the audit record requires one; codes
# outside these sets are recorded as UNSPECIFIED rather than blocking the run.
REASON_CODES: dict[str, frozenset[str]] = {
    "approve": frozenset({
        "VERIFIED_AGAINST_SOURCE",
        "WITHIN_DELEGATED_AUTHORITY",
        "PO_CONFIRMED_WITH_REQUESTER",
        "KNOWN_RECURRING_CHARGE",
        "OTHER",
    }),
    "reject": frozenset({
        "AMOUNT_INCORRECT",
        "PO_MISSING_OR_INVALID",
        "SUSPECTED_DUPLICATE",
        "VENDOR_NOT_RECOGNISED",
        "DOCUMENT_UNREADABLE",
        "OTHER",
    }),
}
# Optional quick-pick rationale options (multi-select; mirrored in the UI's
# RATIONALE_OPTIONS). Unlike the reason code these are never required, and
# unknown values are dropped rather than recorded.
RATIONALE_OPTIONS: dict[str, frozenset[str]] = {
    "approve": frozenset({
        "DOC_MATCHES_EXTRACTION",
        "PO_AND_VENDOR_VERIFIED",
        "GOODS_SERVICES_RECEIVED",
        "WITHIN_BUDGET",
        "PRE_APPROVED_BY_MANAGER",
    }),
    "reject": frozenset({
        "DOC_DOES_NOT_MATCH",
        "NO_VALID_PO",
        "GOODS_SERVICES_NOT_RECEIVED",
        "OVER_BUDGET",
        "VENDOR_MUST_RESUBMIT",
    }),
}
_NOTE_MAX_CHARS = 1000


def _build_audit_record(ctx: Context, route: str, raw_decision: Any) -> dict[str, Any]:
    """Structured audit record for a human decision (approval/rejection/override).

    Combines what the reviewer submitted (reason code, note, attestation) with
    what the system knew at decision time (flags, primary trigger), so the
    decision is reconstructable without the UI. ``override`` marks an approval
    that proceeds despite a data-integrity finding (anything other than the
    routine over-auto-approval-limit trigger).
    """
    payload = raw_decision if isinstance(raw_decision, dict) else {}
    code = str(payload.get("reason_code") or "").strip().upper()
    if code not in REASON_CODES.get(route, frozenset()):
        if code:
            logger.warning("human_approval_gate: unknown reason_code %r for %s", code, route)
        code = "UNSPECIFIED"

    raw_opts = payload.get("rationale_options")
    allowed_opts = RATIONALE_OPTIONS.get(route, frozenset())
    rationale_options = sorted(
        {o for o in (raw_opts if isinstance(raw_opts, list) else []) if isinstance(o, str) and o in allowed_opts}
    )

    rubric = ctx.state.get("validation_rubric") or []
    flagged = [r.get("id") for r in rubric if isinstance(r, dict) and r.get("status") == "flag"]
    try:
        server_user = ctx.session.user_id
    except Exception:  # bare/test contexts have no session
        server_user = None

    return {
        "decision": route,
        "reason_code": code,
        "rationale_options": rationale_options,
        "note": str(payload.get("note") or "").strip()[:_NOTE_MAX_CHARS],
        "primary_trigger": ctx.state.get("validation_primary_trigger"),
        "flagged_rules": flagged,
        "validation_flags": list(ctx.state.get("validation_flags") or []),
        "override": route == "approve" and any(i != "within_limit" for i in flagged),
        "attested_source_review": bool(payload.get("attested_source_review")),
        "source_document_opened": bool(payload.get("source_document_opened")),
        "reviewer": server_user or str(payload.get("reviewer") or "") or None,
        "decided_at": datetime.now(UTC).isoformat(),
    }


def _format_approval_message(
    record: dict,
    flags: list[str],
    precedent: dict | None = None,
    required_fields: list[str] | None = None,
    po_match: dict | None = None,
) -> str:
    inv = (record.get("invoice_no") or {}).get("value", "N/A")
    vendor = (record.get("vendor") or {}).get("value", "N/A")
    total = (record.get("total") or {}).get("value", "N/A")
    currency = (record.get("currency") or {}).get("value", "USD")
    po_ref = (record.get("po_ref") or {}).get("value", "N/A")
    # "Grounded" is counted over the policy's required fields (Firestore
    # policies/ap_rules), not a hard-coded four.
    required = list(required_fields or get_policy().required_fields)
    grounded = sum(
        1 for key in required if (record.get(key) or {}).get("source_grounding") == "grounded"
    )

    flags_block = "\n".join(f"- {f}" for f in flags) if flags else "No specific flags raised. Manual review has been requested."

    po_row = _po_row(po_match)

    memory_block = ""
    if precedent and precedent.get("total_count") and precedent.get("narrative"):
        memory_block = f"\n\n**Agent Memory:** {precedent['narrative']}"

    return (
        "## Human Approval Required\n\n"
        "This invoice requires your review and approval before it can be posted. "
        "One or more accounts payable policy checks have raised a concern."
        f"{memory_block}\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Invoice No | `{inv}` |\n"
        f"| Vendor | {vendor} |\n"
        f"| Amount | **{currency} {total}** |\n"
        f"| PO Reference | `{po_ref}` |\n"
        f"{po_row}"
        f"| Required fields grounded in the document | {grounded}/{len(required)} |\n\n"
        "### Reason for Review\n\n"
        f"{flags_block}\n\n"
        f"{_HR}\n\n"
        "**Your decision:**\n\n"
        "- **Approve and post** — reply `approve` to post the invoice to the matched system.\n"
        "- **Reject and notify vendor** — reply `reject` to draft a rejection notice for your review."
    )


@node(name="human_approval_gate", rerun_on_resume=True)
async def human_approval_gate(ctx: Context, node_input: Any):
    """HITL pause: display invoice + flags; route on human decision."""
    if "approval_decision" not in ctx.resume_inputs:
        record = ctx.state.get("invoice_record", {})
        flags = ctx.state.get("validation_flags", [])
        precedent = ctx.state.get("decision_precedents")
        # Decision-time policy stamped by validate_invoice (falls back to the live one).
        required = (ctx.state.get("policy_snapshot") or {}).get("required_fields")
        msg = _format_approval_message(
            record, flags, precedent, required, ctx.state.get("po_match")
        )
        messages = build_approval_surface(record, flags, precedent) if a2ui_enabled() else None
        # Plain-language briefing for the reviewer (LLM, with a deterministic
        # fallback — never blocks the gate). Reused if the node re-enters.
        threshold = (ctx.state.get("policy_snapshot") or {}).get("hitl_threshold_usd")
        summary = ctx.state.get("review_summary")
        # The Agent Memory note is written the same way, only when there is a real
        # precedent (the empty state needs no model). Both run together so the card
        # waits for the slower of the two, not their sum.
        memory_summary = ctx.state.get("memory_summary")
        want_memory = bool(precedent and precedent.get("total_count") and not memory_summary)
        new_summary, new_memory = await asyncio.gather(
            generate_review_summary(
                record,
                flags,
                ctx.state.get("validation_rubric") or [],
                ctx.state.get("po_match"),
                precedent,
                threshold,
            )
            if not summary
            else _noop(),
            generate_memory_summary(precedent, record, flags, threshold) if want_memory else _noop(),
        )
        summary = summary or new_summary
        memory_summary = memory_summary or new_memory
        with enrich_node_span("human_approval_gate") as span:
            span.set("decision.flag_count", len(flags))
            span.set("hitl.summary_source", summary["source"])
            if memory_summary:
                span.set("hitl.memory_summary_source", memory_summary["source"])
            span.event("hitl.prompt_shown", {"interrupt_id": "approval_decision"})
        gate_state: dict[str, Any] = {"review_summary": summary}
        if memory_summary:
            gate_state["memory_summary"] = memory_summary
        yield Event(
            content=_content_with_a2ui(msg, messages),
            state=gate_state,  # ty: ignore[unknown-argument]
        )
        yield RequestInput(
            interrupt_id="approval_decision",
            message="Approve or reject this invoice?",
        )
        return

    # Resume may be a bare word (string) or a structured A2UI action payload
    # (dict with a "decision" key carrying "approve"/"reject").
    raw_decision = _normalize_resume(ctx.resume_inputs.get("approval_decision", ""))
    if isinstance(raw_decision, dict):
        decision = str(raw_decision.get("decision") or raw_decision.get("result") or "").lower().strip()
    else:
        decision = str(raw_decision).lower().strip()
    first_word = decision.split()[0] if decision.split() else ""
    route = "approve" if first_word in _APPROVE_WORDS else "reject"
    logger.info("human_approval_gate route=%s (user said: %r)", route, decision)

    audit = _build_audit_record(ctx, route, raw_decision)
    logger.info("human_approval_gate audit=%s", audit)

    with enrich_node_span("human_approval_gate") as span:
        span.set("decision.route", route)
        span.set("hitl.decision", route)
        span.set("hitl.reason_code", audit["reason_code"])
        span.set("hitl.override", audit["override"])
        span.event(
            "hitl.resolved",
            {
                "route": route,
                "reason_code": audit["reason_code"],
                "primary_trigger": audit["primary_trigger"] or "",
                "override": audit["override"],
            },
        )

    # Persist the decision to memory (advisory only; a failure here can never
    # affect the run — see app/memory.py). Amount/currency are the invoice's
    # own total, not PII, and let a future precedent state what was actually
    # approved/rejected before.
    invoice_record = ctx.state.get("invoice_record") or {}
    vendor = (invoice_record.get("vendor") or {}).get("value") or ""
    if vendor:
        kinds = memory.flag_kinds(ctx.state.get("validation_flags") or [])
        amount = (invoice_record.get("total") or {}).get("value")
        currency = (invoice_record.get("currency") or {}).get("value")
        await memory.remember_decision(vendor, kinds, route, amount=amount, currency=currency)

    yield Event(
        output={"decision": decision},
        route=route,  # ty: ignore[unknown-argument]
        state={"approval_decision": decision, "approval_audit": audit},  # ty: ignore[unknown-argument]
    )
