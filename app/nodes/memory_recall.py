"""Advisory Agent Memory recall before the human-approval gate."""

import logging
import time
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

from app import memory
from app.a2ui_views import a2ui_enabled, build_memory_surface
from app.app_utils.tracing import enrich_node_span
from app.nodes._helpers import _content_msg, _content_with_a2ui

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 5b. recall_precedents  (advisory Agent Memory recall before human review)
#     Sits between validate_invoice's human_review route and
#     human_approval_gate. Never blocks or alters the route — a memory miss
#     or failure (see app/memory.py) just means no card and no state delta.
# ---------------------------------------------------------------------------


def _format_memory_summary(summary: memory.PrecedentSummary) -> str:
    return f"## Agent Memory\n\n{summary.narrative}"


@node(name="recall_precedents")
async def recall_precedents(ctx: Context, node_input: Any) -> Event | None:
    """Advisory recall of prior approve/reject precedent for this vendor.

    Returns None (no event, no state change) when memory is disabled, the
    vendor is unknown, or there is no matching precedent yet.
    """
    record = ctx.state.get("invoice_record") or {}
    flags = ctx.state.get("validation_flags") or []
    vendor = (record.get("vendor") or {}).get("value") or ""
    kinds = memory.flag_kinds(flags)

    t0 = time.perf_counter()
    summary = await memory.recall_precedents(vendor, kinds) if vendor else None
    recall_ms = (time.perf_counter() - t0) * 1000.0

    with enrich_node_span("recall_precedents") as span:
        span.set("memory.enabled", memory.ENABLE_MEMORY)
        span.set("memory.recall_ms", round(recall_ms, 1))
        if summary:
            span.set("memory.total_count", summary.total_count)
            span.set("memory.recommendation", summary.recommendation or "")
    logger.info(
        "recall_precedents: recall_ms=%.1f vendor=%s hit=%s",
        recall_ms, vendor, bool(summary),
    )

    if not summary:
        # No matching precedent. When memory actually ran for this invoice, still
        # emit a zero-count summary so the approval card can always show an Agent
        # Memory section (its empty state) rather than silently omitting it. A
        # disabled / vendorless run emits nothing so the card stays clean.
        if not (memory.ENABLE_MEMORY and vendor):
            return None
        summary = memory.PrecedentSummary(
            vendor=vendor,
            flag_kinds=kinds,
            narrative="No prior decisions on file for this vendor yet.",
        )

    # Only the real-precedent case still builds the (now unused) A2UI card payload.
    messages = build_memory_surface(summary.model_dump()) if (a2ui_enabled() and summary.total_count) else None
    summary_md = _format_memory_summary(summary)
    content = _content_with_a2ui(summary_md, messages) if messages else _content_msg(summary_md)
    return Event(
        content=content,
        state={"decision_precedents": summary.model_dump()},  # ty: ignore[unknown-argument]
    )
