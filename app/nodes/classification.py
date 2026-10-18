"""PO match + spend classification.

``classify_spend`` looks the PO up in Firestore (`purchase_orders`) right after
extraction — BEFORE validation and human approval — so the PO checks (status,
vendor, amount) and the approver both see the matched PO. ``route_posting``
runs after validation / approval and sends the invoice to the ERP (both direct
and indirect spend) from that earlier match.
"""

import logging
import time
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

from app.app_utils.tracing import enrich_node_span
from app.config import PURCHASE_ORDERS_COLLECTION
from app.po_store import lookup_po
from app.policy import get_policy
from app.schemas import POMatchResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 5a. classify_spend  (PO lookup; routes -> "classified" | "error")
# ---------------------------------------------------------------------------


@node(name="classify_spend")
def classify_spend(ctx: Context, node_input: Any) -> Event:
    """PO lookup; classify spend as direct (SAP CIMS) or indirect (Coupa AP).

    Runs before validation: it records the match in state (``po_match``) and
    forwards the invoice record unchanged so ``validate_invoice`` receives it as
    its input exactly as it did from the extraction card.

    A Firestore failure raises out of lookup_po and lands in the ``except`` below
    (route "error") — it is deliberately NOT treated as an unmatched PO.
    """
    try:
        record_raw = ctx.state.get("invoice_record", {})
        # An invoice with no PO is stored as {"value": None, ...}: the key exists, so a
        # .get("value", "") default would not apply and .strip() would raise.
        po_ref = ((record_raw.get("po_ref") or {}).get("value") or "").strip()

        # Time the ledger read (Firestore `purchase_orders`) — shown as the step's
        # latency in the UI, like the extraction step's model latency.
        t0 = time.perf_counter()
        match = lookup_po(po_ref)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        if match:
            result = POMatchResult(
                route=match["type"],
                po_number=match.get("po_number") or po_ref,
                po_type=match["type"],
                matched=True,
                vendor_name=match.get("vendor"),
                po_amount=match.get("amount"),
                currency=match.get("currency"),
                category=match.get("category"),
                cost_center=match.get("cost_center"),
                gl_account=match.get("gl_account"),
                requisitioner=match.get("requisitioner"),
                po_status=match.get("status"),
                target_system="ERP",
                store_path=f"{PURCHASE_ORDERS_COLLECTION}/{match.get('po_number')}",
                latency_ms=latency_ms,
            )
            logger.info(
                "classify_spend route=%s (PO %s matched in the PO ledger)",
                result.route,
                result.po_number,
            )
        else:
            # Unmatched PO -> default spend route per AP policy (policies/ap_rules
            # unmatched_po_route; "direct" / SAP CIMS unless changed).
            route = get_policy().unmatched_po_route
            result = POMatchResult(
                route=route,
                po_number=po_ref or None,
                matched=False,
                category="Unclassified (no PO match)",
                target_system="ERP",
                latency_ms=latency_ms,
            )
            logger.info(
                "classify_spend route=%s (PO %r not found in the PO ledger)", route, po_ref
            )

        with enrich_node_span("classify_spend") as span:
            span.set("decision.route", result.route)
            span.set("decision.po_matched", result.matched)
            span.set("po.target_system", result.target_system)
            span.set("classify.latency_ms", latency_ms)
            span.set("po.number", result.po_number)
            span.set_verbose("po.category", result.category)
            span.set_verbose("po.amount", result.po_amount)

        return Event(
            output=record_raw,  # forwarded for validate_invoice
            route="classified",  # ty: ignore[unknown-argument]
            state={  # ty: ignore[unknown-argument]
                "po_match": result.model_dump(),
                "classify_latency_ms": latency_ms,
            },
        )
    except Exception as exc:
        logger.error("classify_spend failed: %s", exc)
        with enrich_node_span("classify_spend") as span:
            span.set("decision.route", "error")
            span.error(exc)
        return Event(
            route="error",  # ty: ignore[unknown-argument]
            state={"pipeline_error": str(exc), "pipeline_error_node": "classify_spend"},  # ty: ignore[unknown-argument]
        )


# ---------------------------------------------------------------------------
# 7. route_posting  (router after validation / approval -> "direct" | "indirect")
# ---------------------------------------------------------------------------


@node(name="route_posting")
def route_posting(ctx: Context, node_input: Any) -> Event:
    """Send a cleared / approved invoice to the ERP (both direct and indirect spend).

    Reads the PO match recorded earlier by ``classify_spend``; with no match it
    falls back to the unmatched-PO route from the AP policy.
    """
    po = ctx.state.get("po_match") or {}
    spend_type = po.get("route") or get_policy().unmatched_po_route
    logger.info("route_posting spend_type=%s -> erp", spend_type)
    with enrich_node_span("route_posting") as span:
        span.set("decision.route", "erp")
        span.set("decision.spend_type", spend_type)
    return Event(route="erp")  # ty: ignore[unknown-argument]
