"""AP business-rule validation and routing."""

import difflib
import logging
import re
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node

from app.app_utils.tracing import enrich_node_span
from app.config import BUYER_ORG
from app.duplicate_check import find_posted_by_invoice, find_posted_by_po
from app.nodes._helpers import _HR, _content_msg, _safe_float
from app.policy import Policy, get_policy
from app.schemas import InvoiceRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 5. validate_invoice  (router -> "proceed" | "human_review" | "duplicate_po" | "quarantined")
# ---------------------------------------------------------------------------

_ROUTE_LABELS = {
    "proceed": "Proceed to posting",
    "human_review": "Escalate to human review",
    "duplicate_po": "Escalate — duplicate detected",
}

_DECISION_BANNERS = {
    "proceed": "**Cleared for straight-through processing.** Proceeding to posting.",
    "human_review": "**On hold for human review.** Your approval is required before this invoice can be posted.",
    "duplicate_po": "**Blocked. A duplicate has been detected.** Escalating to ServiceNow; no posting will occur.",
}

# The rule book, in the order it is evaluated. Each entry maps a check to the
# substrings its flags contain, so the display can mark every rule pass/flag
# without re-implementing the numeric logic in _check_rules(). The auto-approval
# label carries a {threshold} placeholder filled from the live policy at render
# time (the limit lives in Firestore, so it can't be baked in at import).
# The third element marks checks that compare against the matched PO: they show
# "n/a" instead of a misleading Pass when the PO was not found in the ledger.
_CHECK_CATALOG: list[tuple[str, tuple[str, ...], bool]] = [
    ("No duplicate invoice or PO on file", ("duplicate", "already invoiced", "already been"), False),
    ("All required fields present", ("required field missing",), False),
    ("Total amount is valid", ("invalid total",), False),
    (
        "Within auto-approval limit (≤ ${threshold})",
        ("auto-approval limit", "hitl threshold"),
        False,
    ),
    ("PO is open for invoicing", ("po status check",), True),
    ("Invoice vendor matches the PO vendor", ("po vendor check",), True),
    ("Invoice total within the PO amount", ("po amount check",), True),
    ("Line items fully captured", ("line items incomplete",), False),
    ("Line items reconcile to total", ("do not reconcile",), False),
    ("Fields well-formed", ("not a valid",), False),
    ("All required fields grounded in the document", ("not grounded",), False),
]


# ---------------------------------------------------------------------------
# Review rubric — the structured "why" behind a human-review escalation.
#
# Each rule pairs the policy criterion with the facts actually observed on this
# invoice and the outcome, so a reviewer sees the chain that produced the flag
# (facts -> criterion -> result), not just the flag text. Rules are listed in
# severity order: the first failing rule is the single *primary trigger* the
# approval UI leads with; the rest are secondary findings. Pure presentation
# data — routing is still decided only by _check_rules().
# ---------------------------------------------------------------------------

_RULE_ORDER: list[tuple[str, str, tuple[str, ...]]] = [
    ("duplicate", "No duplicate invoice or PO on file", ("duplicate", "already invoiced", "already been")),
    ("required_fields", "All required fields present", ("required field missing",)),
    ("valid_total", "Total amount is valid", ("invalid total",)),
    ("within_limit", "Within auto-approval limit", ("auto-approval limit", "hitl threshold")),
    ("po_status", "PO is open for invoicing", ("po status check",)),
    ("po_vendor", "Invoice vendor matches the PO vendor", ("po vendor check",)),
    ("po_amount", "Invoice total within the PO amount", ("po amount check",)),
    ("line_items_complete", "Line items fully captured", ("line items incomplete",)),
    ("reconciles", "Line items reconcile to total", ("do not reconcile",)),
    ("well_formed", "Fields well-formed", ("not a valid",)),
    ("grounded", "All required fields grounded in the document", ("not grounded",)),
]

# Rules that compare against the matched PO; listed only when the PO was found.
_PO_RULES = frozenset({"po_status", "po_vendor", "po_amount"})


# Failure-phrased headline per rule (the rule label above reads as a passing check).
_RULE_HEADLINES = {
    "duplicate": "Duplicate invoice or PO detected",
    "required_fields": "Required field missing",
    "valid_total": "Invalid total amount",
    "within_limit": "Over the auto-approval limit",
    "po_status": "PO not open for invoicing",
    "po_vendor": "Vendor differs from the PO vendor",
    "po_amount": "Invoice exceeds the PO amount",
    "line_items_complete": "Line items incomplete",
    "reconciles": "Line items don't add up to the total",
    "well_formed": "Malformed field value",
    "grounded": "Field not grounded in the document",
}


def _rule_facts(
    rule_id: str, record: InvoiceRecord, policy: Policy, po_match: dict | None = None
) -> tuple[str, str]:
    """(criterion, observed) text for a rule, from the record's own values."""
    currency = (record.currency.value or "USD").strip() or "USD"
    total = _safe_float(record.total.value)
    if rule_id == "duplicate":
        return (
            "Invoice number and PO reference are not already booked",
            f"Invoice {record.invoice_no.value or '—'} · PO {record.po_ref.value or '—'}",
        )
    if rule_id == "required_fields":
        missing = [f for f in policy.required_fields if not getattr(getattr(record, f, None), "value", None)]
        return (
            f"Present: {', '.join(policy.required_fields)}",
            f"Missing: {', '.join(missing)}" if missing else "All present",
        )
    if rule_id == "valid_total":
        return ("Total is a number greater than 0", record.total.value or "—")
    if rule_id == "within_limit":
        if total is None:
            observed = "—"
        elif currency.upper() == "USD":
            observed = f"USD {total:,.2f}"
        else:
            observed = f"{currency} {total:,.2f} (≈ ${policy.to_usd(total, currency):,.2f})"
        return (f"Total ≤ ${policy.hitl_threshold_usd:,.0f} (USD-equivalent)", observed)
    if rule_id in _PO_RULES:
        po = po_match or {}
        if rule_id == "po_status":
            return (
                f"PO status is not one of: {', '.join(policy.blocked_po_statuses) or '—'}",
                f"PO {po.get('po_number') or '?'} · {po.get('po_status') or 'unknown'}",
            )
        if rule_id == "po_vendor":
            return (
                f"Vendor names at least {policy.po_vendor_min_similarity:.0%} similar",
                f"Invoice \"{record.vendor.value or '—'}\" vs PO \"{po.get('vendor_name') or '—'}\"",
            )
        tol = policy.po_amount_tolerance_pct
        po_amount = po.get("po_amount")
        return (
            "Invoice total does not exceed the PO amount"
            + (f" (+{tol:.0%} tolerance)" if tol else ""),
            f"Invoice {currency} {total:,.2f} vs PO {po.get('currency') or 'USD'} {float(po_amount):,.2f}"
            if total is not None and po_amount is not None
            else "—",
        )
    if rule_id == "line_items_complete":
        n = len(record.line_items)
        return (
            "Every line item was captured",
            f"{n} captured" + ("" if getattr(record, "line_items_complete", True) else " — truncated"),
        )
    if rule_id == "reconciles":
        line_sum = sum(
            amt for li in record.line_items if (amt := _safe_float(li.amount)) is not None
        )
        return (
            f"Line-item sum within ±{policy.reconcile_tolerance_pct:.0%} of the total",
            f"Σ lines {line_sum:,.2f} vs total {total:,.2f}" if record.line_items and total is not None
            else "No line items to reconcile",
        )
    if rule_id == "well_formed":
        return (
            "Currency is an ISO 4217 code; invoice number is a valid identifier",
            f"Currency {record.currency.value or '—'} · Invoice {record.invoice_no.value or '—'}",
        )
    # grounded
    states = ", ".join(
        f"{f}: {getattr(record, f).source_grounding}" for f in policy.required_fields if getattr(record, f, None)
    )
    return ("Required fields read directly from the document", states or "—")


def build_rubric(
    record: InvoiceRecord,
    flags: list[str],
    policy: Policy | None = None,
    po_match: dict | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Build the review rubric and the id of the single primary trigger.

    Returns ``(rules, primary_trigger_id)``. Each rule is ``{id, label,
    headline, criterion, observed, status ("pass" | "flag"), finding, rank}``; ``rank`` is
    1-based among the flagged rules only (1 = primary trigger) and ``None`` for
    passes. A flag matching no known rule is surfaced as its own trailing
    ``other`` rule so nothing is hidden.
    """
    policy = policy or get_policy()
    po_matched = bool(po_match and po_match.get("matched"))
    remaining = list(flags)
    rules: list[dict[str, Any]] = []
    for rule_id, label, needles in _RULE_ORDER:
        if rule_id in _PO_RULES and not po_matched:
            continue
        hits = [f for f in remaining if any(n in f.lower() for n in needles)]
        remaining = [f for f in remaining if f not in hits]
        criterion, observed = _rule_facts(rule_id, record, policy, po_match)
        rules.append({
            "id": rule_id,
            "label": label,
            "headline": _RULE_HEADLINES[rule_id],
            "criterion": criterion,
            "observed": observed,
            "status": "flag" if hits else "pass",
            "finding": " ".join(hits) if hits else None,
            "rank": None,
        })
    for i, f in enumerate(remaining):
        rules.append({
            "id": f"other_{i}",
            "label": "Other policy finding",
            "headline": "Other policy finding",
            "criterion": "Policy check",
            "observed": f,
            "status": "flag",
            "finding": f,
            "rank": None,
        })
    flagged = [r for r in rules if r["status"] == "flag"]
    for rank, r in enumerate(flagged, start=1):
        r["rank"] = rank
    return rules, (flagged[0]["id"] if flagged else None)


def _format_validation_summary(
    record: InvoiceRecord,
    route: str,
    flags: list[str],
    policy: Policy | None = None,
    po_match: dict | None = None,
) -> str:
    policy = policy or get_policy()
    threshold = f"{policy.hitl_threshold_usd:,.0f}"
    po_matched = bool(po_match and po_match.get("matched"))
    inv_no = record.invoice_no.value or "—"
    vendor = record.vendor.value or "—"
    po_ref = record.po_ref.value or "—"
    total = record.total.value or "—"
    currency = record.currency.value or ""
    amount = f"{currency} {total}".strip() if total != "—" else "—"

    lower_flags = [f.lower() for f in flags]
    check_lines = []
    for label, needles, needs_po in _CHECK_CATALOG:
        hit = any(any(n in lf for n in needles) for lf in lower_flags)
        result = "n/a (no PO match)" if needs_po and not po_matched else ("Flag" if hit else "Pass")
        check_lines.append(f"| {label.format(threshold=threshold)} | {result} |")
    checks_block = "\n".join(check_lines)

    findings_block = (
        "\n".join(f"- {f}" for f in flags)
        if flags
        else "All checks passed. No exceptions noted."
    )

    return (
        "## Business-Rule Validation\n\n"
        f"Invoice **{inv_no}** from **{vendor}** has been reviewed against the {BUYER_ORG} accounts payable policy book.\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        f"| Invoice No | `{inv_no}` |\n"
        f"| Vendor | {vendor} |\n"
        f"| PO Reference | `{po_ref}` |\n"
        f"| Amount | **{amount}** |\n\n"
        "### Policy Checks\n\n"
        "| Check | Result |\n"
        "|---|---|\n"
        f"{checks_block}\n\n"
        "### Exceptions\n\n"
        f"{findings_block}\n\n"
        f"{_HR}\n\n"
        f"{_DECISION_BANNERS.get(route, _ROUTE_LABELS.get(route, route))}"
    )


_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def reconcile_totals(record: InvoiceRecord, policy: Policy | None = None) -> str | None:
    """Sum line_items and compare against the header total.

    The strongest deterministic verification signal: it corroborates the
    extracted total from independently-transcribed rows, without asking the
    model to self-grade its own certainty. Returns a flag message when the sum
    doesn't reconcile within the policy's reconcile_tolerance_pct of the total, else None.
    Invoices with no line items (e.g. a single lump-sum bill) are not flagged —
    there's nothing to reconcile against.
    """
    if not record.line_items:
        return None
    total_val = _safe_float(record.total.value)
    if total_val is None:
        return None
    line_sum = sum(
        amt for li in record.line_items if (amt := _safe_float(li.amount)) is not None
    )
    policy = policy or get_policy()
    tolerance = max(abs(total_val) * policy.reconcile_tolerance_pct, 0.01)
    if abs(line_sum - total_val) > tolerance:
        return (
            f"Line items ({line_sum:,.2f}) do not reconcile with the invoice total "
            f"({total_val:,.2f})"
        )
    return None


def check_field_formats(record: InvoiceRecord, policy: Policy | None = None) -> list[str]:
    """Deterministic format/plausibility checks independent of model self-grading."""
    policy = policy or get_policy()
    flags: list[str] = []
    currency = (record.currency.value or "").strip().upper()
    if currency and not _CURRENCY_RE.match(currency):
        flags.append(f"Currency '{record.currency.value}' is not a valid ISO 4217 code")
    invoice_no = (record.invoice_no.value or "").strip()
    if invoice_no and len(invoice_no) < policy.min_invoice_no_length:
        flags.append(f"Invoice number '{invoice_no}' is not a valid identifier")
    return flags


def assess_grounding(record: InvoiceRecord, policy: Policy | None = None) -> list[str]:
    """Flag any required field the model could not ground in the document.

    Deterministic policy over the model's evidence — a field rated "inferred"
    or "not_found" routes to human review, regardless of any self-reported
    certainty (there is none to report; the schema no longer carries one).
    """
    policy = policy or get_policy()
    flags: list[str] = []
    for field_name in policy.required_fields:
        field = getattr(record, field_name, None)
        if field is not None and field.source_grounding != "grounded":
            flags.append(f"Required field '{field_name}' is not grounded in the document")
    return flags


# Legal-form and filler words dropped before comparing vendor names, so
# "Meridian Steel & Alloys" and "Meridian Steel and Alloys Inc." compare equal.
_VENDOR_NOISE = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co",
    "company", "gmbh", "plc", "pvt", "lp", "llp", "sa", "ag", "bv", "nv", "pty",
    "and", "the", "of",
})


def _normalize_vendor(name: str) -> str:
    tokens = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("&", " and ")).split()
    return " ".join(t for t in tokens if t not in _VENDOR_NOISE)


def vendor_similarity(a: str, b: str) -> float:
    """0-1 similarity of two vendor names, ignoring case, punctuation and legal suffixes."""
    na, nb = _normalize_vendor(a), _normalize_vendor(b)
    if not na or not nb:  # a name made only of noise words: fall back to a raw compare
        return 1.0 if a.strip().lower() == b.strip().lower() else 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def check_po_match(
    record: InvoiceRecord, po_match: dict | None, policy: Policy | None = None
) -> list[str]:
    """Compare the invoice against the PO it references (status, vendor, amount).

    Runs only when the PO was found in the ledger; an unmatched PO produces no
    flags here (there is nothing to compare against). An invoice BELOW the PO
    amount is a normal partial billing and is never flagged.
    """
    if not po_match or not po_match.get("matched"):
        return []
    policy = policy or get_policy()
    flags: list[str] = []
    po_no = po_match.get("po_number") or "?"

    status = (po_match.get("po_status") or "").strip().lower()
    if status and status in policy.blocked_po_statuses:
        flags.append(f'PO status check: PO {po_no} is "{status}" and not open for invoicing')

    inv_vendor = (record.vendor.value or "").strip()
    po_vendor = (po_match.get("vendor_name") or "").strip()
    if policy.po_vendor_min_similarity > 0 and inv_vendor and po_vendor:
        if vendor_similarity(inv_vendor, po_vendor) < policy.po_vendor_min_similarity:
            flags.append(
                f'PO vendor check: invoice vendor "{inv_vendor}" does not match '
                f'vendor "{po_vendor}" on PO {po_no}'
            )

    tolerance = policy.po_amount_tolerance_pct
    inv_total = _safe_float(record.total.value)
    po_amount = po_match.get("po_amount")
    if tolerance is not None and inv_total is not None and po_amount is not None:
        inv_cur = (record.currency.value or "USD").strip().upper()
        po_cur = (po_match.get("currency") or "USD").strip().upper()
        inv_usd = policy.to_usd(inv_total, inv_cur)
        po_usd = policy.to_usd(float(po_amount), po_cur)
        if inv_usd > po_usd * (1 + tolerance) + 0.01:
            flags.append(
                f"PO amount check: invoice total {inv_cur} {inv_total:,.2f} is above "
                f"PO {po_no} amount {po_cur} {float(po_amount):,.2f} "
                f"(tolerance {tolerance * 100:g}%)"
            )
    return flags


def _same_vendor(invoice_vendor: str, posted_vendor: str, policy: Policy) -> bool:
    """True when two vendor names plausibly refer to the same supplier.

    A name missing on either side can't be compared, so it counts as a match: the
    invoice number / PO + amount already agree, and escalating a possible duplicate
    is the safe outcome."""
    if not invoice_vendor.strip() or not posted_vendor.strip():
        return True
    threshold = policy.po_vendor_min_similarity or 0.85
    return vendor_similarity(invoice_vendor, posted_vendor) >= threshold


def check_duplicates(record: InvoiceRecord, policy: Policy | None = None) -> str | None:
    """Flag an invoice that has already been posted, per Firestore ``posted_invoices``.

    Two checks, first hit wins:

    * the same invoice number from the same vendor was already posted, or
    * the same vendor already posted the same amount (and currency) against the
      same PO under a different invoice number (a re-keyed resubmission).

    A further invoice against a PO for a different amount is a normal partial
    billing and is not flagged here. Raises ``ConfigStoreError`` if Firestore
    can't be read; ``validate_invoice`` turns that into its error route rather
    than letting a possible duplicate through.
    """
    policy = policy or get_policy()
    vendor = (record.vendor.value or "").strip()

    inv_no = (record.invoice_no.value or "").strip()
    for doc in find_posted_by_invoice(inv_no):
        if _same_vendor(vendor, (doc.data.get("invoice") or {}).get("vendor") or "", policy):
            return f"Duplicate invoice number detected: {inv_no} (already posted as {doc.id})"

    po_ref = (record.po_ref.value or "").strip()
    total = _safe_float(record.total.value)
    if po_ref and total is not None:
        currency = (record.currency.value or "USD").strip().upper()
        for doc in find_posted_by_po(po_ref):
            posted = doc.data.get("invoice") or {}
            posted_total = posted.get("total_amount")
            if (
                posted_total is not None
                and abs(float(posted_total) - total) <= 0.01
                and (posted.get("currency") or "USD").strip().upper() == currency
                and _same_vendor(vendor, posted.get("vendor") or "", policy)
            ):
                return (
                    f"PO number already invoiced (duplicate): {po_ref} "
                    f"(same vendor and amount already posted as {doc.id})"
                )
    return None


def _check_rules(
    record: InvoiceRecord, policy: Policy | None = None, po_match: dict | None = None
) -> tuple[str, list[str]]:
    """Run AP business rules. Returns (route, list_of_flag_messages).

    ``policy`` is the Firestore-backed AP policy snapshot (``app.policy``); when
    omitted the current cached policy is used. validate_invoice passes one
    snapshot so a single decision never straddles a policy edit. ``po_match`` is
    the PO lookup result from ``classify_spend`` (which now runs before
    validation); the PO checks run only when it matched.
    """
    policy = policy or get_policy()
    flags: list[str] = []

    # Invoice currency (for converting amounts to USD before threshold checks).
    _cur_field = getattr(record, "currency", None)
    currency = (_cur_field.value if _cur_field and _cur_field.value else "USD")

    # (a) Duplicate check against the Firestore `posted_invoices` ledger
    # (invoice number, then PO + amount). A hit short-circuits to the duplicate route.
    duplicate_flag = check_duplicates(record, policy)
    if duplicate_flag:
        return "duplicate_po", [duplicate_flag]

    # (a-iii) Invoice total threshold — amounts above the policy's HITL limit
    # (USD-equivalent) require approval
    invoice_total = _safe_float(record.total.value)
    if invoice_total is not None and policy.to_usd(invoice_total, currency) > policy.hitl_threshold_usd:
        # The USD conversion is only informative for a non-USD invoice.
        usd_note = (
            ""
            if currency.strip().upper() == "USD"
            else f" (approx. ${policy.to_usd(invoice_total, currency):,.2f})"
        )
        flags.append(
            f"Invoice total {currency} {invoice_total:,.2f}{usd_note} "
            f"exceeds the AP auto-approval limit of ${policy.hitl_threshold_usd:,.2f}. Human approval is required."
        )

    # (a-iv) PO checks: status / vendor / amount against the matched PO
    flags.extend(check_po_match(record, po_match, policy))

    # (b) Required-field check
    for field_name in policy.required_fields:
        field = getattr(record, field_name, None)
        if field is None or not field.value:
            flags.append(f"Required field missing: {field_name}")

    # (c) Total-amount sanity check
    total_val = _safe_float(record.total.value)
    if total_val is not None and total_val <= 0:
        flags.append(f"Invalid total amount: {record.total.value}")

    # (d) Deterministic verification — arithmetic reconciliation, field format,
    # and source-grounding checks, in place of any self-graded confidence score.
    if not getattr(record, "line_items_complete", True):
        # Line-item extraction was truncated before every row was captured, so
        # line_items is partial. Flag it explicitly (distinct from a genuine
        # reconcile mismatch) and skip the reconcile check, whose result would be
        # a misleading "don't reconcile" on knowingly-incomplete data.
        flags.append(
            f"Line items incomplete — extraction was truncated ({len(record.line_items)} "
            f"captured); the invoice total cannot be reconciled reliably."
        )
    else:
        reconcile_flag = reconcile_totals(record, policy)
        if reconcile_flag:
            flags.append(reconcile_flag)
    flags.extend(check_field_formats(record, policy))
    flags.extend(assess_grounding(record, policy))

    route = "human_review" if flags else "proceed"
    return route, flags


def _armor_quarantine(verdict: dict | None) -> dict | None:
    """Quarantine decision for a blocked Model Armor scan of the extracted content.

    Same shape as the intake quarantine (``fetch_invoice_email``), so
    ``quarantine_invoice`` / ``pipeline_complete`` handle both: a filter match is
    a ``threat`` (AP-Quarantined, never retried), a scan that could not run is a
    ``scan_error`` (AP-Error, retried on the next catch-up).
    """
    if not verdict or not verdict.get("blocked"):
        return None
    filters = list(verdict.get("filters") or [])
    if filters:
        reason = f"Model Armor flagged: {', '.join(filters)}"
    else:
        reason = f"Model Armor could not scan the content: {verdict.get('error') or 'unknown error'}"
    return {
        "reason": reason,
        "kind": "threat" if filters else "scan_error",
        "stage": "extraction",
        "filters": filters,
    }


@node(name="validate_invoice")
def validate_invoice(ctx: Context, node_input: Any) -> Event:
    """Apply AP business rules and route accordingly."""
    try:
        # Fall back to the stored record (never overwrite good extraction with an
        # empty dict) if this node's input isn't a populated record.
        raw = (
            node_input
            if isinstance(node_input, dict) and node_input
            else (ctx.state.get("invoice_record") or {})
        )
        try:
            record = InvoiceRecord(**raw)
        except Exception as exc:
            logger.warning("Could not parse InvoiceRecord: %s", exc)
            record = InvoiceRecord()

        # Model Armor flagged (or could not scan) the content the model extracted:
        # the values can't be trusted, so stop here -- before any rule, approval or
        # posting -- and take precedence over every other route (duplicate included).
        quarantine = _armor_quarantine(ctx.state.get("armor_extraction"))
        if quarantine:
            logger.warning("validate_invoice: quarantined by Model Armor: %s", quarantine["reason"])
            with enrich_node_span("validate_invoice") as span:
                span.set("decision.route", "quarantined")
                span.set("quarantine.kind", quarantine["kind"])
                span.set("armor.filters", ",".join(quarantine["filters"]))
            return Event(
                route="quarantined",  # ty: ignore[unknown-argument]
                state={"invoice_record": raw, "armor_quarantine": quarantine},  # ty: ignore[unknown-argument]
            )

        # One policy snapshot for the whole decision (rules, rendering, audit).
        policy = get_policy()
        po_match = ctx.state.get("po_match") or None
        route, flags = _check_rules(record, policy, po_match)
        rubric, primary_trigger = build_rubric(record, flags, policy, po_match)
        logger.info(
            "validate_invoice route=%s flags=%s policy=%s", route, flags, policy.version
        )

        with enrich_node_span("validate_invoice") as span:
            span.set("decision.route", route)
            span.set("decision.flag_count", len(flags))
            span.set("policy.version", policy.version)
            span.set("invoice.no", record.invoice_no.value)
            span.set_verbose("invoice.vendor", record.vendor.value)
            span.set_verbose("invoice.total", record.total.value)
            span.set_verbose("invoice.currency", record.currency.value)
            span.set_verbose("invoice.po_ref", record.po_ref.value)
            for flag in flags:
                span.event("validation.flag", {"message": flag})

        return Event(
            content=_content_msg(_format_validation_summary(record, route, flags, policy, po_match)),
            route=route,  # ty: ignore[unknown-argument]
            state={  # ty: ignore[unknown-argument]
                "invoice_record": raw,
                "validation_flags": flags,
                "validation_route": route,
                # Which policy values this decision was made under (audit trail).
                "policy_snapshot": policy.audit(),
                "validation_rubric": rubric,
                "validation_primary_trigger": primary_trigger,
            },
        )
    except Exception as exc:
        logger.error("validate_invoice failed: %s", exc)
        with enrich_node_span("validate_invoice") as span:
            span.set("decision.route", "error")
            span.error(exc)
        return Event(
            route="error",  # ty: ignore[unknown-argument]
            state={
                "pipeline_error": str(exc),
                "pipeline_error_node": "validate_invoice",
            },  # ty: ignore[unknown-argument]
        )
