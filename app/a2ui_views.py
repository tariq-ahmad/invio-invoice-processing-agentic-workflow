"""A2UI (Agent-to-UI) surface builders for the invoice-processing demo.

This module turns the pipeline's already-structured data (the extracted
``InvoiceRecord`` dict, validation flags, the drafted rejection email) into
**A2UI** declarative-UI messages that a custom React front end renders with
``@a2ui/react``. Surfaces are built deterministically in Python (NOT generated
by an LLM) so the client demo is repeatable and reliable.

Three surfaces are produced:
  * ``build_extraction_surface``  - read-only invoice card with grounding badges
  * ``build_approval_surface``    - human-approval gate (Approve / Reject buttons)
  * ``build_email_review_surface``- editable rejection email (Send / Revise)

Transport: each A2UI message is wrapped in a ``google.genai`` ``Part`` whose
``inline_data`` Blob carries the JSON bytes with MIME ``application/a2ui+json``.
Nodes emit these parts ALONGSIDE the existing markdown text part in the same
Event, so the ADK dev playground (which ignores the unknown MIME) keeps working
while our React app filters for the A2UI parts. Emission is gated by the
``ENABLE_A2UI`` env var (default on).

The A2UI message format targets the bundled ``basic`` catalog, spec **v0.9**
(``a2ui-agent-sdk`` 0.9.x). Message envelope, component names, and data-binding
shapes were verified against the catalog's JSON Schema; see
``tests/unit/test_a2ui_views.py`` for schema validation.
"""

from __future__ import annotations

import json
import os
from typing import Any

from google.genai import types as genai_types

# ---------------------------------------------------------------------------
# Constants (centralised so a spec-version bump is a one-line change)
# ---------------------------------------------------------------------------

# Current canonical A2UI MIME. Our backend <-> frontend channel uses this string
# on both ends; the frontend interpreter filters parts on exactly this value.
A2UI_MIME = "application/a2ui+json"

# Message envelope version + catalog id for the bundled "basic" catalog.
A2UI_VERSION = "v0.9"
A2UI_CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"

# Surface identifiers (stable; the frontend may key animations off these).
SURFACE_SCREENING = "invoice_screening"
SURFACE_EXTRACTION = "invoice_extraction"
SURFACE_APPROVAL = "approval_gate"
SURFACE_EMAIL = "email_review"
SURFACE_MEMORY = "agent_memory"

# Interrupt ids the HITL nodes use (mirrors app/nodes.py). The email review id
# is dynamic (per revision loop), so it is passed in to the builder.
INTERRUPT_APPROVAL = "approval_decision"



# ---------------------------------------------------------------------------
# Enablement + genai Part wrapping
# ---------------------------------------------------------------------------


def a2ui_enabled() -> bool:
    """Whether to emit A2UI parts. Controlled by env ``ENABLE_A2UI`` (default on)."""
    return os.getenv("ENABLE_A2UI", "true").strip().lower() in ("1", "true", "yes", "on")


def a2ui_part(message: dict[str, Any]) -> genai_types.Part:
    """Wrap a single A2UI message dict as a genai inline-data Part."""
    return genai_types.Part(
        inline_data=genai_types.Blob(
            data=json.dumps(message).encode("utf-8"),
            mime_type=A2UI_MIME,
        )
    )


def a2ui_parts(messages: list[dict[str, Any]]) -> list[genai_types.Part]:
    """Wrap a list of A2UI messages (one surface = several messages) as Parts."""
    return [a2ui_part(m) for m in messages]


# ---------------------------------------------------------------------------
# Message builders (server -> client)
# ---------------------------------------------------------------------------


def _create_surface(surface_id: str, *, send_data_model: bool = False) -> dict[str, Any]:
    create: dict[str, Any] = {"surfaceId": surface_id, "catalogId": A2UI_CATALOG_ID}
    if send_data_model:
        create["sendDataModel"] = True
    return {"version": A2UI_VERSION, "createSurface": create}


def _update_components(surface_id: str, components: list[dict]) -> dict[str, Any]:
    return {
        "version": A2UI_VERSION,
        "updateComponents": {"surfaceId": surface_id, "components": components},
    }


def _update_data_model(surface_id: str, value: dict, path: str = "/") -> dict[str, Any]:
    return {
        "version": A2UI_VERSION,
        "updateDataModel": {"surfaceId": surface_id, "path": path, "value": value},
    }


# ---------------------------------------------------------------------------
# Component helpers (flat list; every component needs a unique id)
# ---------------------------------------------------------------------------


def _text(cid: str, text: Any, variant: str | None = None) -> dict[str, Any]:
    c: dict[str, Any] = {"id": cid, "component": "Text", "text": str(text)}
    if variant:
        c["variant"] = variant
    return c


def _card(cid: str, child: str) -> dict[str, Any]:
    return {"id": cid, "component": "Card", "child": child}


def _column(cid: str, children: list[str], **kw) -> dict[str, Any]:
    return {"id": cid, "component": "Column", "children": children, **kw}


def _row(cid: str, children: list[str], **kw) -> dict[str, Any]:
    return {"id": cid, "component": "Row", "children": children, **kw}


def _divider(cid: str) -> dict[str, Any]:
    return {"id": cid, "component": "Divider"}


def _button(
    cid: str,
    child: str,
    action_name: str,
    context: dict | None = None,
    variant: str = "default",
) -> dict[str, Any]:
    event: dict[str, Any] = {"name": action_name}
    if context:
        event["context"] = context
    return {
        "id": cid,
        "component": "Button",
        "child": child,
        "variant": variant,
        "action": {"event": event},
    }


def _text_field(
    cid: str, label: str, value: Any = None, variant: str = "shortText"
) -> dict[str, Any]:
    c: dict[str, Any] = {
        "id": cid,
        "component": "TextField",
        "label": label,
        "variant": variant,
    }
    if value is not None:
        c["value"] = value
    return c


def _bind(path: str) -> dict[str, str]:
    """A data-model binding (JSON Pointer)."""
    return {"path": path}


def _fv(record: dict, key: str) -> tuple[str, str]:
    """Pull (value, source_grounding) for a FieldValue-shaped key from a record dict."""
    fv = record.get(key) or {}
    value = fv.get("value")
    return (
        str(value) if value not in (None, "") else "—",
        str(fv.get("source_grounding", "not_found")),
    )


# ---------------------------------------------------------------------------
# Surface 1 — invoice extraction card (read-only)
# ---------------------------------------------------------------------------


_GROUNDING_LABEL = {"grounded": "Grounded", "inferred": "Inferred", "not_found": "Not found"}


def build_extraction_surface(invoice_record: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the extracted-invoice card surface from an InvoiceRecord dict.

    Each field's source-grounding evidence (grounded / inferred / not_found) is
    rendered inline — observable evidence of how the value was obtained, not a
    self-graded confidence score.
    """
    rec = invoice_record or {}
    sid = SURFACE_EXTRACTION
    comps: list[dict[str, Any]] = [
        _text("title", "Invoice Extracted", "h3"),
        _text("subtitle", "Read from the PDF by Gemini — grounding shown per field", "caption"),
    ]

    currency, _ = _fv(rec, "currency")
    field_rows: list[str] = []
    grounded_count = 0
    fields = [
        ("vendor", "Vendor"),
        ("invoice_no", "Invoice No"),
        ("po_ref", "PO Reference"),
        ("total", "Total"),
    ]
    for key, label in fields:
        value, grounding = _fv(rec, key)
        if grounding == "grounded":
            grounded_count += 1
        if key == "total" and value != "—" and currency != "—":
            value = f"{currency} {value}"
        comps += [
            _text(f"{key}_l", label, "caption"),
            _text(f"{key}_v", value),
            _text(f"{key}_c", _GROUNDING_LABEL.get(grounding, grounding), "caption"),
        ]
        comps.append(_row(f"{key}_row", [f"{key}_l", f"{key}_v", f"{key}_c"], justify="spaceBetween"))
        field_rows.append(f"{key}_row")

    comps.append(_column("fields_col", field_rows))
    comps.append(_card("header_card", "fields_col"))

    root_children = ["title", "subtitle", "header_card"]

    # Line items, statically expanded (deterministic, avoids template binding).
    line_items = rec.get("line_items") or []
    if line_items:
        comps.append(_divider("div_li"))
        comps.append(_text("li_heading", "Line Items", "caption"))
        # Loud, explicit banner when line-item extraction truncated before every
        # row was captured — so a partial list never reads as a complete one.
        if rec.get("line_items_complete") is False:
            comps.append(_text(
                "li_incomplete",
                f"⚠ Line items incomplete — extraction was truncated ({len(line_items)} "
                "captured). The list below is partial and the total cannot be "
                "reconciled reliably.",
                "caption",
            ))
        li_rows: list[str] = []
        for i, li in enumerate(line_items):
            name = li.get("name") or "—"
            amt = li.get("amount") or ""
            grounding = _GROUNDING_LABEL.get(li.get("source_grounding", "not_found"), "Not found")
            comps += [
                _text(f"li{i}_d", name),
                _text(f"li{i}_a", amt),
                _text(f"li{i}_g", grounding, "caption"),
            ]
            comps.append(_row(f"li{i}_r", [f"li{i}_d", f"li{i}_a", f"li{i}_g"], justify="spaceBetween"))
            row_children = [f"li{i}_r"]
            attrs = [
                a for a in (li.get("attributes") or [])
                if isinstance(a, dict) and a.get("label") and a.get("value")
            ]
            if attrs:
                attrs_text = " · ".join(f"{a['label']}: {a['value']}" for a in attrs)
                comps.append(_text(f"li{i}_attrs", attrs_text, "caption"))
                row_children.append(f"li{i}_attrs")
            li_rows.extend(row_children)
        comps.append(_column("li_col", li_rows))
        root_children += ["div_li", "li_heading"]
        if rec.get("line_items_complete") is False:
            root_children.append("li_incomplete")
        root_children.append("li_col")

    comps.append(_divider("div_conf"))
    comps.append(_text("conf_footer", f"Fields grounded in the document: {grounded_count}/{len(fields)}", "caption"))
    root_children += ["div_conf", "conf_footer"]

    comps.append(_column("root", root_children))
    return [_create_surface(sid), _update_components(sid, comps)]


# ---------------------------------------------------------------------------
# Surface 1b — email screening card (read-only screening assessment)
# ---------------------------------------------------------------------------


def build_screening_surface(assessment: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the intake screening result card.

    Shows the per-signal evidence strengths (attachment / body / subject), the
    observed signals, and the assessment rationale — evidence only. The
    screening agent never decides; the deterministic "Screening Decision" step
    is the sole place the Invoice/Not-an-invoice verdict is displayed.
    """
    sid = SURFACE_SCREENING
    v = assessment or {}
    attachment_signal = str(v.get("attachment_signal", "absent"))
    body_signal = str(v.get("body_signal", "absent"))
    subject_signal = str(v.get("subject_signal", "absent"))
    signals: list[str] = v.get("observed_signals") or []
    rationale: str = v.get("assessment_rationale", "")

    comps: list[dict[str, Any]] = [
        _text("title", "Email Screening", "h3"),
        _text("subtitle", "Intake screening rated the attachment, body, and subject for invoice evidence", "caption"),
    ]

    comps += [
        _text("attach_l", "Attachment", "caption"),
        _text("attach_v", attachment_signal),
        _text("body_l", "Body", "caption"),
        _text("body_v", body_signal),
        _text("subject_l", "Subject", "caption"),
        _text("subject_v", subject_signal),
    ]
    comps.append(_row("attach_row", ["attach_l", "attach_v"], justify="spaceBetween"))
    comps.append(_row("body_row", ["body_l", "body_v"], justify="spaceBetween"))
    comps.append(_row("subject_row", ["subject_l", "subject_v"], justify="spaceBetween"))
    comps.append(_column("summary_col", ["attach_row", "body_row", "subject_row"]))
    comps.append(_card("summary_card", "summary_col"))

    root_children = ["title", "subtitle", "summary_card"]

    if signals:
        comps.append(_divider("div_signals"))
        comps.append(_text("signals_heading", "Detected Signals", "caption"))
        sig_ids: list[str] = []
        for i, sig in enumerate(signals):
            comps.append(_text(f"sig{i}", f"•  {sig}"))
            sig_ids.append(f"sig{i}")
        comps.append(_column("signals_col", sig_ids))
        root_children += ["div_signals", "signals_heading", "signals_col"]

    if rationale:
        comps.append(_divider("div_reason"))
        comps.append(_text("reason_heading", "Rationale", "caption"))
        comps.append(_text("reason_body", rationale))
        root_children += ["div_reason", "reason_heading", "reason_body"]

    comps.append(_column("root", root_children))
    return [_create_surface(sid), _update_components(sid, comps)]


# ---------------------------------------------------------------------------
# Surface 2 — human approval gate (Approve / Reject)
# ---------------------------------------------------------------------------


def build_approval_surface(
    invoice_record: dict[str, Any],
    flags: list[str] | None,
    precedent: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the human-approval gate surface.

    The Approve/Reject buttons carry an action whose ``context.interrupt_id`` is
    the HITL interrupt id (``approval_decision``) and ``context.decision`` is the
    resume value the node expects (``approve`` / ``reject``).

    ``precedent`` is the recall_precedents node's ``PrecedentSummary`` dict
    (``state["decision_precedents"]``), when the Agent Memory feature found a
    prior decision for this vendor + flag kind. Advisory only.
    """
    rec = invoice_record or {}
    sid = SURFACE_APPROVAL
    comps: list[dict[str, Any]] = [
        _text("title", "Human Review Required", "h3"),
        _text("subtitle", "Posting is paused pending your decision", "caption"),
    ]

    currency, _ = _fv(rec, "currency")
    summary_rows: list[str] = []
    for key, label in [
        ("invoice_no", "Invoice No"),
        ("vendor", "Vendor"),
        ("total", "Amount"),
        ("po_ref", "PO Reference"),
    ]:
        value, _ = _fv(rec, key)
        if key == "total" and value != "—" and currency != "—":
            value = f"{currency} {value}"
        comps += [_text(f"{key}_l", label, "caption"), _text(f"{key}_v", value)]
        comps.append(_row(f"{key}_r", [f"{key}_l", f"{key}_v"], justify="spaceBetween"))
        summary_rows.append(f"{key}_r")
    comps.append(_column("summary_col", summary_rows))
    comps.append(_card("summary_card", "summary_col"))

    comps.append(_divider("div_flags"))
    comps.append(_text("flags_heading", "Reasons for review", "caption"))
    flag_ids: list[str] = []
    for i, flag in enumerate(flags or ["Manual review requested."]):
        comps.append(_text(f"flag{i}", f"•  {flag}"))
        flag_ids.append(f"flag{i}")
    comps.append(_column("flags_col", flag_ids))

    root_children = ["title", "subtitle", "summary_card", "div_flags", "flags_heading", "flags_col"]

    if precedent and precedent.get("total_count") and precedent.get("narrative"):
        comps.append(_divider("div_memory"))
        comps.append(_text("memory_note", precedent["narrative"], "caption"))
        root_children += ["div_memory", "memory_note"]

    comps += [_text("approve_lbl", "Approve & post"), _text("reject_lbl", "Reject & notify vendor")]
    comps.append(
        _button(
            "approve_btn",
            "approve_lbl",
            "approve_invoice",
            {"interrupt_id": INTERRUPT_APPROVAL, "decision": "approve"},
            variant="primary",
        )
    )
    comps.append(
        _button(
            "reject_btn",
            "reject_lbl",
            "reject_invoice",
            {"interrupt_id": INTERRUPT_APPROVAL, "decision": "reject"},
            variant="default",
        )
    )
    comps.append(_row("actions", ["approve_btn", "reject_btn"], justify="start"))
    root_children.append("actions")

    comps.append(_column("root", root_children))
    return [_create_surface(sid), _update_components(sid, comps)]


# ---------------------------------------------------------------------------
# Surface 4 — Agent Memory precedent card (advisory; human still decides)
# ---------------------------------------------------------------------------


def build_memory_surface(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the Agent Memory precedent card from a PrecedentSummary dict.

    Read-only, advisory: shown before human_approval_gate on the human_review
    branch when recall_precedents finds a prior decision for this vendor with
    an overlapping flag kind (see app/memory.py).
    """
    sid = SURFACE_MEMORY
    s = summary or {}
    vendor = s.get("vendor") or "—"
    kinds: list[str] = s.get("flag_kinds") or []
    total = int(s.get("total_count", 0))
    approve = int(s.get("approve_count", 0))
    reject = int(s.get("reject_count", 0))

    comps: list[dict[str, Any]] = [
        _text("title", "Agent Memory", "h3"),
        _text("subtitle", f"Prior AP decisions for {vendor} with a similar flag", "caption"),
    ]

    comps += [
        _text("total_l", "Prior decisions", "caption"),
        _text("total_v", str(total)),
        _text("approve_l", "Approved", "caption"),
        _text("approve_v", str(approve)),
        _text("reject_l", "Rejected", "caption"),
        _text("reject_v", str(reject)),
    ]
    comps.append(_row("total_row", ["total_l", "total_v"], justify="spaceBetween"))
    comps.append(_row("approve_row", ["approve_l", "approve_v"], justify="spaceBetween"))
    comps.append(_row("reject_row", ["reject_l", "reject_v"], justify="spaceBetween"))
    comps.append(_column("summary_col", ["total_row", "approve_row", "reject_row"]))
    comps.append(_card("summary_card", "summary_col"))

    root_children = ["title", "subtitle", "summary_card"]

    if kinds:
        comps.append(_divider("div_kinds"))
        comps.append(_text("kinds_heading", "Flag kind(s)", "caption"))
        comps.append(_text("kinds_body", ", ".join(kinds)))
        root_children += ["div_kinds", "kinds_heading", "kinds_body"]

    comps.append(_divider("div_reco"))
    comps.append(_text("reco_body", s.get("narrative") or "No consistent precedent yet — your decision is required"))
    root_children += ["div_reco", "reco_body"]

    comps.append(_column("root", root_children))
    return [_create_surface(sid), _update_components(sid, comps)]


# ---------------------------------------------------------------------------
# Surface 3 — editable rejection email (Send / Revise)
# ---------------------------------------------------------------------------


def build_email_review_surface(
    draft: dict[str, Any], recipient: str, review_count: int, notice: str | None = None
) -> list[dict[str, Any]]:
    """Build the editable rejection-email surface.

    Subject/Body are two-way bound to the data model (``/email/subject`` and
    ``/email/body``). ``sendDataModel`` is set so the (edited) model travels with
    actions. The Send action also resolves the edited subject/body via
    ``{path}`` bindings in its context, and Revise carries the feedback text — so
    the backend receives the human's edits regardless of transport. ``notice``
    (e.g. a Model Armor warning) is shown as a line under the title.
    """
    # Unique surface id per review loop so a revise round doesn't collide with
    # an already-created surface in the client renderer.
    sid = f"{SURFACE_EMAIL}_{review_count}"
    interrupt_id = f"email_review_{review_count}"
    subject = (draft or {}).get("subject", "") if isinstance(draft, dict) else ""
    body = (draft or {}).get("body", "") if isinstance(draft, dict) else ""

    heading = "Review Rejection Email"
    if review_count > 0:
        heading = f"Review Rejection Email (revision {review_count})"

    comps: list[dict[str, Any]] = [
        _text("title", heading, "h3"),
        _text("to_l", "To", "caption"),
        _text("to_v", recipient or "vendor"),
        _row("to_r", ["to_l", "to_v"], justify="spaceBetween"),
        _text_field("subject_field", "Subject", _bind("/email/subject"), "shortText"),
        _text_field("body_field", "Body", _bind("/email/body"), "longText"),
        _text_field("feedback_field", "Revision instructions (optional)", _bind("/email/feedback"), "longText"),
        _text("send_lbl", "Send to vendor"),
        _text("revise_lbl", "Revise with instructions"),
        _button(
            "send_btn",
            "send_lbl",
            "send_email",
            {
                "interrupt_id": interrupt_id,
                "decision": "send",
                "subject": _bind("/email/subject"),
                "body": _bind("/email/body"),
            },
            variant="primary",
        ),
        _button(
            "revise_btn",
            "revise_lbl",
            "revise_email",
            {
                "interrupt_id": interrupt_id,
                "feedback": _bind("/email/feedback"),
                "subject": _bind("/email/subject"),
                "body": _bind("/email/body"),
            },
            variant="default",
        ),
        _row("actions", ["send_btn", "revise_btn"], justify="start"),
    ]
    root_children = ["title", "to_r", "subject_field", "body_field", "feedback_field", "actions"]
    if notice:
        comps.append(_text("notice", notice))
        root_children.insert(1, "notice")
    comps.append(_column("root", root_children))

    data_model = {
        "email": {"to": recipient or "", "subject": subject, "body": body, "feedback": ""}
    }
    return [
        _create_surface(sid, send_data_model=True),
        _update_components(sid, comps),
        _update_data_model(sid, data_model),
    ]
