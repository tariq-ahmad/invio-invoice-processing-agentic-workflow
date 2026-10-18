"""Agent Memory Bank — advisory approve/reject precedent per vendor + flag kind.

Wraps a single ``BaseMemoryService`` instance: ``VertexAiMemoryBankService`` when
``MEMORY_AGENT_ENGINE_ID`` is configured (see ``docs/spec-agent-memory-demo.md``
for the standalone Agent Engine used purely as the memory backend), else
``InMemoryMemoryService`` for local dev. This module owns its service directly
rather than going through the ADK Runner's configured memory service, so no
change to ``get_fast_api_app(...)`` is required — the recall/remember calls
below are the entire integration surface, which keeps the rollback story to a
single file plus one node.

Every public call is wrapped so a memory failure (quota, network, disabled
service) can never break a pipeline run — memory is advisory only, and the
human approval gate is never bypassed.

Fact encoding (schema v2):
    vendor=<name>|flags=<kind,kind>|decision=<approve|reject>|amount=<value>|currency=<code>|ts=<iso>
Pipe-delimited (not space-delimited) because vendor names contain spaces.
``amount``/``currency`` are the invoice's own total — a business figure, not
personal data, so it's fine to keep alongside the rest. Both are optional on
decode so facts written before this field existed still parse (with amount
treated as unknown for those entries). No PII either way — no email content,
no invoice numbers, no reviewer identity.

The recall/recommendation logic here is entirely deterministic (plain Python
tally + a fixed threshold rule) — no LLM call reads or writes memory. Only the
narrative sentence built by ``_narrate`` is new prose; the underlying decision
is always traceable to an exact prior-fact count, which is the point for an
auditable AP workflow.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from google.adk.memory import BaseMemoryService, InMemoryMemoryService
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from app.config import (
    ENABLE_MEMORY,
    MEMORY_AGENT_ENGINE_ID,
    MEMORY_LOCATION,
    MEMORY_PRECEDENT_MIN,
)

logger = logging.getLogger(__name__)

# Fixed scope: precedent is shared across approvers and accumulates across runs
# (not per-session), so a repeat vendor+flag pattern is recognized regardless
# of who is reviewing.
_APP_NAME = "app"
_AP_TEAM_USER = "ap-team"


class PrecedentSummary(BaseModel):
    """Advisory recall result shown on the Agent Memory card and echoed on the
    approval gate. ``recommendation`` is set only once decisions are unanimous
    and reach ``MEMORY_PRECEDENT_MIN`` — otherwise the human decides with no
    steer either way. ``narrative`` is the ready-to-render sentence; every
    consumer (backend markdown, A2UI JSON, the actual React cards) renders it
    verbatim rather than re-deriving its own phrasing."""

    vendor: str
    flag_kinds: list[str]
    total_count: int = 0
    approve_count: int = 0
    reject_count: int = 0
    recommendation: str | None = None  # "approve" | "reject" | None
    approved_amounts: list[str] = Field(default_factory=list)  # e.g. ["USD 4750.00"]
    rejected_amounts: list[str] = Field(default_factory=list)
    narrative: str = ""


# Classifies raw validation-flag strings (app/nodes.py._check_rules messages)
# into a small set of stable kinds so precedent keys on "duplicate" or
# "over_limit" rather than a full sentence that never repeats verbatim.
_FLAG_KIND_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("duplicate", ("duplicate", "already invoiced", "already been")),
    ("missing_field", ("required field missing",)),
    ("invalid_total", ("invalid total",)),
    ("over_limit", ("auto-approval limit", "hitl threshold", "exceeds")),
    ("total_mismatch", ("do not reconcile",)),
    ("invalid_format", ("not a valid",)),
    ("ungrounded_field", ("not grounded",)),
    ("po_vendor_mismatch", ("po vendor check",)),
    ("over_po_amount", ("po amount check",)),
    ("po_not_open", ("po status check",)),
]

# Plain-English phrasing for each kind, so the narrative never surfaces an
# internal token like "over_limit" to the reader.
_KIND_PHRASES: dict[str, str] = {
    "duplicate": "a duplicate invoice or PO flag",
    "missing_field": "a missing required field",
    "invalid_total": "an invalid total amount",
    "over_limit": "an amount above the auto-approval limit",
    "total_mismatch": "line items not reconciling with the total",
    "invalid_format": "a malformed field",
    "ungrounded_field": "a field that could not be grounded in the document",
    "po_vendor_mismatch": "a vendor that differs from the purchase order",
    "over_po_amount": "an amount above the purchase order value",
    "po_not_open": "a purchase order that is not open for invoicing",
    "other": "a similar flag",
}


def flag_kinds(flags: list[str]) -> list[str]:
    """Classify raw validation-flag strings into stable kinds for memory keys."""
    kinds: list[str] = []
    for flag in flags:
        lf = flag.lower()
        for kind, needles in _FLAG_KIND_RULES:
            if kind not in kinds and any(n in lf for n in needles):
                kinds.append(kind)
    return kinds or ["other"]


def _kind_phrase(kinds: list[str]) -> str:
    """Turn coarse flag kinds into a plain-English phrase, e.g. 'an amount
    above the auto-approval limit', deduped and joined for readability."""
    seen: list[str] = []
    for k in kinds:
        phrase = _KIND_PHRASES.get(k, "a similar flag")
        if phrase not in seen:
            seen.append(phrase)
    if not seen:
        return "a similar flag"
    if len(seen) == 1:
        return seen[0]
    return ", ".join(seen[:-1]) + f", and {seen[-1]}"


def _amounts_clause(amounts: list[str]) -> str:
    if not amounts:
        return ""
    if len(amounts) == 1:
        return f" — most recently at {amounts[0]}"
    return f": {', '.join(amounts[:-1])}, and {amounts[-1]}"


def _narrate(
    vendor: str,
    kinds: list[str],
    total: int,
    approve: int,
    reject: int,
    recommendation: str | None,
    approved_amounts: list[str],
    rejected_amounts: list[str],
) -> str:
    """Build the precedent sentence shown on the Agent Memory card and echoed
    on the approval gate. A Python template, not an LLM — deterministic and
    repeatable, and every recommendation traces to this exact tally."""
    kind_phrase = _kind_phrase(kinds)

    if reject == 0:
        history = (
            f"{vendor}'s invoices with {kind_phrase} have been approved every "
            f"time before ({approve} of {total}){_amounts_clause(approved_amounts)}."
        )
    elif approve == 0:
        history = (
            f"{vendor}'s invoices with {kind_phrase} have been rejected every "
            f"time before ({reject} of {total}){_amounts_clause(rejected_amounts)}."
        )
    else:
        approve_part = f"{approve} approved"
        if approved_amounts:
            approve_part += f" ({', '.join(approved_amounts)})"
        reject_part = f"{reject} rejected"
        if rejected_amounts:
            reject_part += f" ({', '.join(rejected_amounts)})"
        history = (
            f"Reviewers have been split on {vendor}'s invoices with {kind_phrase} "
            f"before — {approve_part}, {reject_part}."
        )

    if recommendation:
        history += (
            f" Based on that consistent history, the agent recommends "
            f"**{recommendation}** — the final decision is yours."
        )
    return history


_service: BaseMemoryService | None = None


def _get_service() -> BaseMemoryService:
    """Lazily build the memory service singleton (Vertex when configured, else
    in-memory). Constructed lazily so importing this module never requires
    Vertex credentials when the feature is off or unconfigured."""
    global _service
    if _service is not None:
        return _service
    if MEMORY_AGENT_ENGINE_ID:
        import os

        from google.adk.memory import VertexAiMemoryBankService

        _service = VertexAiMemoryBankService(
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=MEMORY_LOCATION,
            agent_engine_id=MEMORY_AGENT_ENGINE_ID,
        )
    else:
        _service = InMemoryMemoryService()
    return _service


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_amount(amount: str | None, currency: str | None) -> str | None:
    """'4750.00', 'USD' -> 'USD 4750.00'. None when the amount is unknown."""
    if not amount:
        return None
    return f"{currency} {amount}".strip() if currency else amount


def _encode_fact(
    vendor: str,
    kinds: list[str],
    decision: str,
    when_iso: str,
    amount: str | None = None,
    currency: str | None = None,
) -> str:
    return (
        f"vendor={vendor}|flags={','.join(kinds)}|decision={decision}"
        f"|amount={amount or ''}|currency={currency or ''}|ts={when_iso}"
    )


def _decode_fact(
    text: str,
) -> tuple[str, list[str], str, str | None, str | None] | None:
    """Parse a fact string back into (vendor, kinds, decision, amount, currency).

    ``amount``/``currency`` are None when absent or empty — tolerates facts
    written before schema v2 added them. None if the text is malformed.
    """
    try:
        fields = dict(kv.split("=", 1) for kv in text.strip().split("|") if "=" in kv)
        vendor = fields["vendor"]
        decision = fields["decision"]
        kinds = fields["flags"].split(",") if fields.get("flags") else []
        amount = fields.get("amount") or None
        currency = fields.get("currency") or None
        return vendor, kinds, decision, amount, currency
    except Exception:
        return None


async def remember_decision(
    vendor: str,
    kinds: list[str],
    decision: str,
    amount: str | None = None,
    currency: str | None = None,
) -> None:
    """Persist an approve/reject fact for this vendor + flag kinds, including
    the invoice amount when known so a future precedent can state what was
    actually approved or rejected before, not just how many times.

    No-op when memory is disabled or vendor is unknown. Never raises.
    """
    if not ENABLE_MEMORY or not vendor:
        return
    fact = _encode_fact(vendor, kinds, decision, _now_iso(), amount, currency)
    content = genai_types.Content(role="user", parts=[genai_types.Part.from_text(text=fact)])

    try:
        service = _get_service()
        if isinstance(service, InMemoryMemoryService):
            from google.adk.events.event import Event

            # Explicit id required: Event.id defaults to "" and the in-memory
            # service dedupes writes by id, so repeated blank ids would drop
            # every fact after the first.
            event = Event(id=uuid.uuid4().hex, author="user", content=content)
            await service.add_events_to_memory(
                app_name=_APP_NAME, user_id=_AP_TEAM_USER, events=[event]
            )
        else:
            from google.adk.memory.memory_entry import MemoryEntry

            entry = MemoryEntry(content=content, timestamp=_now_iso())
            await service.add_memory(
                app_name=_APP_NAME, user_id=_AP_TEAM_USER, memories=[entry]
            )
        logger.info(
            "remember_decision: vendor=%s kinds=%s decision=%s amount=%s",
            vendor, kinds, decision, _format_amount(amount, currency),
        )
    except Exception as exc:
        logger.warning("remember_decision: memory write failed (non-fatal): %s", exc)


async def recall_precedents(vendor: str, kinds: list[str]) -> PrecedentSummary | None:
    """Recall prior approve/reject decisions for this vendor with an overlapping
    flag kind. Returns None when memory is disabled, unavailable, or no prior
    decision matches — never raises.
    """
    if not ENABLE_MEMORY or not vendor:
        return None

    try:
        service = _get_service()
        response = await service.search_memory(
            app_name=_APP_NAME, user_id=_AP_TEAM_USER, query=vendor
        )
    except Exception as exc:
        logger.warning("recall_precedents: memory search failed (non-fatal): %s", exc)
        return None

    kind_set = set(kinds)
    approve = reject = 0
    approved_amounts: list[str] = []
    rejected_amounts: list[str] = []
    # Kinds actually confirmed present on a matched fact — NOT the query kind
    # set. The narrative/PrecedentSummary must only ever describe what memory
    # itself confirms, never the current invoice's own flags wholesale (a
    # multi-flag invoice can match single-issue historical facts that only
    # share one of its several kinds).
    matched_kinds: set[str] = set()
    for mem in response.memories or []:
        text = "".join(p.text or "" for p in (mem.content.parts or [])) if mem.content else ""
        decoded = _decode_fact(text)
        if not decoded:
            continue
        p_vendor, p_kinds, p_decision, p_amount, p_currency = decoded
        overlap = set(p_kinds) & kind_set
        if p_vendor != vendor or not overlap:
            continue
        matched_kinds |= overlap
        display_amount = _format_amount(p_amount, p_currency)
        if p_decision == "approve":
            approve += 1
            if display_amount:
                approved_amounts.append(display_amount)
        elif p_decision == "reject":
            reject += 1
            if display_amount:
                rejected_amounts.append(display_amount)

    total = approve + reject
    if total == 0:
        return None

    recommendation: str | None = None
    if approve >= MEMORY_PRECEDENT_MIN and reject == 0:
        recommendation = "approve"
    elif reject >= MEMORY_PRECEDENT_MIN and approve == 0:
        recommendation = "reject"

    # Preserve the invoice's own flag order for stable, readable phrasing
    # (not set iteration order); every element is guaranteed present since
    # `matched_kinds` only ever grows from a confirmed overlap above.
    observed_kinds = [k for k in kinds if k in matched_kinds]

    narrative = _narrate(
        vendor, observed_kinds, total, approve, reject, recommendation, approved_amounts, rejected_amounts
    )

    return PrecedentSummary(
        vendor=vendor,
        flag_kinds=observed_kinds,
        total_count=total,
        approve_count=approve,
        reject_count=reject,
        recommendation=recommendation,
        approved_amounts=approved_amounts,
        rejected_amounts=rejected_amounts,
        narrative=narrative,
    )
