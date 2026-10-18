"""Downstream ERP posting: invoices are recorded in Firestore
(``posted_invoices``, see app/posting_store.py); the ServiceNow escalation is
still a stub."""

import logging
import uuid
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

from app.app_utils.tracing import enrich_node_span
from app.nodes._helpers import _now_iso
from app.posting_store import record_posting
from app.schemas import EscalationResult, PostingResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 8. post_to_erp  (ERP posting for both direct and indirect spend, recorded in Firestore)
# ---------------------------------------------------------------------------


@node(name="post_to_erp")
def post_to_erp(ctx: Context, node_input: Any) -> Event:
    """Post an invoice to the ERP system (recorded in Firestore ``posted_invoices``).

    Handles both direct-spend and indirect-spend invoices. The ERP system is not
    actually connected: the posting is stored as a document and the step only
    reports "posted" once that write has succeeded. A failed write raises into
    the ``except`` below (route "error")."""
    try:
        record_raw = ctx.state.get("invoice_record", {})
        inv_no = (record_raw.get("invoice_no") or {}).get("value", "UNKNOWN")
        vendor = (record_raw.get("vendor") or {}).get("value") or "vendor"
        po_match = ctx.state.get("po_match") or {}
        spend_type = (po_match.get("route") or "direct").title()

        doc_id = f"ERP-{uuid.uuid4().hex[:10].upper()}"
        posted_at = _now_iso()
        store_path, latency_ms = record_posting(
            ctx.state, system="ERP", document_id=doc_id, posted_at=posted_at
        )
        result = PostingResult(
            system="ERP",
            document_id=doc_id,
            status="posted",
            message=(
                f"{spend_type}-spend invoice {inv_no} ({vendor}) posted to ERP."
            ),
            posted_at=posted_at,
            reference=f"{vendor} · {inv_no}",
            store_path=store_path,
            latency_ms=latency_ms,
        )
        logger.info("post_to_erp document_id=%s latency_ms=%d", doc_id, latency_ms)
        with enrich_node_span("post_to_erp") as span:
            span.set("posting.system", result.system)
            span.set("posting.document_id", result.document_id)
            span.set("posting.status", result.status)
            span.set("posting.latency_ms", latency_ms)
            span.set("posting.spend_type", spend_type)
            span.event("posting.completed", {"system": result.system})
        return Event(
            output=result.model_dump(),
            route="done",  # ty: ignore[unknown-argument]
            state={
                "posting_result": result.model_dump(),
                "posting_latency_ms": latency_ms,
                "pipeline_status": "posted_erp",
            },  # ty: ignore[unknown-argument]
        )
    except Exception as exc:
        logger.error("post_to_erp failed: %s", exc)
        with enrich_node_span("post_to_erp") as span:
            span.set("posting.status", "error")
            span.error(exc)
        return Event(
            route="error",  # ty: ignore[unknown-argument]
            state={"pipeline_error": str(exc), "pipeline_error_node": "post_to_erp"},  # ty: ignore[unknown-argument]
        )


# ---------------------------------------------------------------------------
# 9. raise_duplicate_alert  (stubbed ServiceNow ticket for confirmed duplicates)
# ---------------------------------------------------------------------------


@node(name="raise_duplicate_alert")
def raise_duplicate_alert(ctx: Context, node_input: Any) -> Event:
    """Stub: raise a ServiceNow duplicate-invoice ticket."""
    try:
        record_raw = ctx.state.get("invoice_record", {})
        inv_no = (record_raw.get("invoice_no") or {}).get("value", "UNKNOWN")
        flags = ctx.state.get("validation_flags", [])

        ticket_id = f"INC{uuid.uuid4().int % 10_000_000:07d}"
        result = EscalationResult(
            ticket_id=ticket_id,
            system="ServiceNow",
            status="created",
            reason="; ".join(flags) or "Suspected duplicate invoice detected",
            invoice_no=inv_no,
            priority="2 - High",
            assignment_group="Accounts Payable Operations",
            created_at=_now_iso(),
        )
        logger.info("raise_duplicate_alert ticket_id=%s", ticket_id)
        with enrich_node_span("raise_duplicate_alert") as span:
            span.set("posting.system", result.system)
            span.set("posting.document_id", result.ticket_id)
            span.set("posting.status", result.status)
            span.event("posting.completed", {"system": result.system})
        return Event(
            output=result.model_dump(),
            route="done",  # ty: ignore[unknown-argument]
            state={  # ty: ignore[unknown-argument]
                "escalation_result": result.model_dump(),
                "pipeline_status": "escalated",
            },
        )
    except Exception as exc:
        logger.error("raise_duplicate_alert failed: %s", exc)
        with enrich_node_span("raise_duplicate_alert") as span:
            span.set("posting.status", "error")
            span.error(exc)
        return Event(
            route="error",  # ty: ignore[unknown-argument]
            state={
                "pipeline_error": str(exc),
                "pipeline_error_node": "raise_duplicate_alert",
            },  # ty: ignore[unknown-argument]
        )
