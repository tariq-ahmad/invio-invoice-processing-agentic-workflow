"""Per-run outcome records and the aggregate behind the UI's stats drawer.

When a run finishes, ``pipeline_complete`` writes one small document to the
Firestore ``run_outcomes`` collection (document id = the run's invocation id).
``GET /stats`` reads the newest of them and totals them, so the numbers are shared
by everyone, survive restarts and browsers, and never depend on one tab.

What counts is deliberately the same as the UI's old browser-local tally, so the
drawer reads the same: only runs that extracted an invoice, "auto-posted" means
posted with no human or exception step, and "sent to review" means a human review,
a duplicate escalation, a rejection, or a failure.

Documents hold the invoice header (vendor, number, total) and run metrics — never
the PDF, line items, sender address or email text. Recording is best-effort: a
Firestore outage is logged and never fails or delays an invoice.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import RUN_OUTCOMES_COLLECTION
from app.cost_calculator import compute_cost
from app.firestore_store import ConfigStoreError, get_backend, valid_doc_id
from app.nodes._helpers import _safe_float
from app.policy import get_policy

logger = logging.getLogger(__name__)

# Newest runs read per /stats call. Bounds the read; `truncated` tells the UI when
# older runs exist beyond it.
MAX_RUNS_READ = 2000

_POSTED = frozenset({"posted_erp", "posted_sap", "posted_coupa"})  # posted_sap/coupa for backward compat
# Routes that mean a person or an exception queue touched the invoice.
_EXCEPTION_ROUTES = frozenset({"human_review", "duplicate_po"})


def _field(record: dict, key: str) -> str | None:
    return (record.get(key) or {}).get("value") or None


def _run_cost_usd(state: Any) -> float:
    """Actual model spend for the run: each call at the model it really used.

    Mirrors the UI's per-run cost (frontend/src/lib/runCost.ts): extraction prefers
    its authoritative server-computed total; every other call is priced from its
    tokens, with cache reads discounted.
    """
    total = 0.0
    for step in ("screening", "extraction", "draft_email"):
        model = state.get(f"{step}_model")
        if not model:
            continue
        if step == "extraction" and state.get("extraction_cost_usd") is not None:
            total += float(state.get("extraction_cost_usd") or 0)
            continue
        total += compute_cost(
            model,
            int(state.get(f"{step}_input_tokens") or 0),
            int(state.get(f"{step}_output_tokens") or 0),
            int(state.get(f"{step}_thinking_tokens") or 0),
            int(state.get(f"{step}_cache_read_tokens") or 0),
        )
    # The approval-card briefings count only when a model wrote them.
    for key in ("review_summary", "memory_summary"):
        call = state.get(key) or {}
        if call.get("source") != "llm" or not call.get("model"):
            continue
        total += compute_cost(
            call["model"],
            int(call.get("input_tokens") or 0),
            int(call.get("output_tokens") or 0),
            int(call.get("thinking_tokens") or 0),
            int(call.get("cache_read_tokens") or 0),
        )
    return round(total, 6)


def build_outcome_record(
    state: Any, *, status: str | None, run_id: str, recorded_at: str
) -> dict[str, Any] | None:
    """The Firestore document for one finished run, or ``None`` when the run never
    extracted an invoice (a screened-out email, ...). A security quarantine is the
    exception: an intake-time one never reaches extraction, but it is still an
    invoice the pipeline stopped, so it is recorded (header fields empty)."""
    record = state.get("invoice_record")
    if not record:
        if status != "quarantined":
            return None
        record = {}

    route = state.get("validation_route")
    exception = bool(
        status == "error"
        or route in _EXCEPTION_ROUTES
        or state.get("escalation_result")
        or state.get("notification_result")
    )
    posted = status in _POSTED
    total = _field(record, "total")
    currency = _field(record, "currency")
    amount = _safe_float(total)
    amount_usd = None
    # A quarantined invoice's extracted values can't be trusted, so it adds no value.
    if amount is not None and status != "quarantined":
        amount_usd = round(get_policy().to_usd(amount, currency), 2)

    decision_ms = int(state.get("extraction_latency_ms") or 0) + int(
        state.get("draft_email_latency_ms") or 0
    )
    return {
        "run_id": run_id,
        "recorded_at": recorded_at,
        "status": status or "unknown",
        "posted": posted,
        "exception": exception,
        "human_review": route == "human_review",
        "touchless": posted and not exception,
        "vendor": _field(record, "vendor"),
        "invoice_no": _field(record, "invoice_no"),
        "amount": amount,
        "currency": currency,
        "amount_usd": amount_usd,
        "decision_ms": decision_ms,
        "cost_usd": _run_cost_usd(state),
        "policy_version": (state.get("policy_snapshot") or {}).get("version"),
        "message_id": state.get("invoice_message_id"),
    }


def record_run_outcome(state: Any, *, status: str | None, run_id: str | None) -> None:
    """Write the run's outcome to Firestore. Best-effort: never raises."""
    try:
        doc_id = run_id if run_id and valid_doc_id(run_id) else f"run-{uuid.uuid4().hex}"
        data = build_outcome_record(
            state,
            status=status,
            run_id=doc_id,
            recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if data is None:
            return
        get_backend().create(RUN_OUTCOMES_COLLECTION, doc_id, data)
        logger.info("run_stats: recorded %s/%s (%s)", RUN_OUTCOMES_COLLECTION, doc_id, status)
    except ConfigStoreError as exc:
        # Includes "already exists" (a run that re-executes this node) — harmless.
        logger.warning("run_stats: not recorded: %s", exc)
    except Exception:  # noqa: BLE001 - stats must never break an invoice
        logger.exception("run_stats: unexpected failure recording a run outcome")


def summarize_outcomes(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over run-outcome documents (the shape GET /stats returns)."""
    out: dict[str, Any] = {
        "invoices": 0,
        "amount_total_usd": 0.0,
        "touchless_count": 0,
        "exception_count": 0,
        "decision_ms_total": 0,
        "decision_ms_count": 0,
        "cost_total_usd": 0.0,
        "cost_count": 0,
        "by_status": {},
        "first_recorded_at": None,
        "last_recorded_at": None,
    }
    for d in docs:
        out["invoices"] += 1
        out["amount_total_usd"] += float(d.get("amount_usd") or 0)
        out["touchless_count"] += 1 if d.get("touchless") else 0
        out["exception_count"] += 1 if d.get("exception") else 0
        ms = int(d.get("decision_ms") or 0)
        if ms > 0:
            out["decision_ms_total"] += ms
            out["decision_ms_count"] += 1
        cost = float(d.get("cost_usd") or 0)
        if cost > 0:
            out["cost_total_usd"] += cost
            out["cost_count"] += 1
        status = d.get("status") or "unknown"
        out["by_status"][status] = out["by_status"].get(status, 0) + 1
        at = d.get("recorded_at")
        if at:
            if out["first_recorded_at"] is None or at < out["first_recorded_at"]:
                out["first_recorded_at"] = at
            if out["last_recorded_at"] is None or at > out["last_recorded_at"]:
                out["last_recorded_at"] = at
    out["amount_total_usd"] = round(out["amount_total_usd"], 2)
    out["cost_total_usd"] = round(out["cost_total_usd"], 6)
    return out


def get_run_stats(days: int = 0, *, now: datetime | None = None) -> dict[str, Any]:
    """Aggregate the newest runs; ``days`` > 0 keeps only the last N days.

    Raises ``ConfigStoreError`` when Firestore can't be read (an outage is never
    reported as "no runs yet").
    """
    docs = get_backend().list_recent(RUN_OUTCOMES_COLLECTION, "recorded_at", MAX_RUNS_READ)
    rows = [d.data for d in docs]
    truncated = len(docs) >= MAX_RUNS_READ
    if days > 0:
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        rows = [r for r in rows if (r.get("recorded_at") or "") >= cutoff]
        # If the newest MAX_RUNS_READ all fall inside the window, older in-window
        # runs may exist beyond them; otherwise the window is fully covered.
        truncated = truncated and len(rows) >= MAX_RUNS_READ
    return {
        **summarize_outcomes(rows),
        "window_days": days or None,
        "truncated": truncated,
        "source": "firestore",
    }
