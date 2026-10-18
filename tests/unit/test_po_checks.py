"""PO lookup before validation, the invoice-vs-PO checks, route_posting, the
AP-Error retry label, and the graph wiring that ties them together."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import agentmail_tools, config
from app import nodes as n
from app.nodes.approval import _format_approval_message
from app.nodes.validation import check_po_match, vendor_similarity
from app.policy import AP_RULES_DOC, ApRules, get_policy, reset_policy_cache
from app.schemas import InvoiceRecord


def _fv(value, grounding="grounded"):
    return {"value": value, "source_grounding": grounding}


def _record(*, vendor="BluePeak Plastics", total="3250.00", currency="USD", po="PO-2026-1003"):
    return {
        "vendor": _fv(vendor),
        "invoice_no": _fv("INV-2026-0301"),
        "po_ref": _fv(po),
        "total": _fv(total),
        "currency": _fv(currency),
        "line_items": [],
    }


def _po(**over):
    po = {
        "po_number": "PO-2026-1003",
        "po_type": "direct",
        "route": "direct",
        "matched": True,
        "vendor_name": "BluePeak Plastics",
        "po_amount": 3250.0,
        "currency": "USD",
        "po_status": "open",
        "target_system": "SAP",
    }
    po.update(over)
    return po


def _rules(backend, **fields):
    backend.set(config.POLICIES_COLLECTION, AP_RULES_DOC, fields)
    reset_policy_cache()


def _flags(rec, po, policy=None):
    return check_po_match(InvoiceRecord(**rec), po, policy)


# ---------------------------------------------------------------------------
# Policy fields
# ---------------------------------------------------------------------------


def test_po_policy_defaults():
    p = get_policy()
    assert p.po_vendor_min_similarity == 0.85
    assert p.po_amount_tolerance_pct == 0.05
    assert p.blocked_po_statuses == ("closed", "cancelled", "fully_invoiced")
    assert {"po_vendor_min_similarity", "po_amount_tolerance_pct", "blocked_po_statuses"} <= set(p.audit())


def test_po_policy_validation_and_normalisation():
    assert ApRules(po_amount_tolerance_pct=None).po_amount_tolerance_pct is None
    assert ApRules(blocked_po_statuses=[" Closed ", "CLOSED", "on_hold", ""]).blocked_po_statuses == [
        "closed",
        "on_hold",
    ]
    for bad in ({"po_vendor_min_similarity": 1.5}, {"po_amount_tolerance_pct": -0.1}):
        with pytest.raises(ValueError):
            ApRules(**bad)


# ---------------------------------------------------------------------------
# Vendor similarity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("BluePeak Plastics", "bluepeak plastics"),
        ("Meridian Steel & Alloys", "Meridian Steel and Alloys Inc."),
        ("Cascade Components Inc", "Cascade Components, Inc"),
        ("Helix Cloud Services LLC", "Helix Cloud Services"),
    ],
)
def test_vendor_names_that_should_match(a, b):
    assert vendor_similarity(a, b) >= 0.85


@pytest.mark.parametrize(
    "a,b",
    [
        ("Globex Trading Co", "Greenfield Catering"),
        ("Acme Corp", "BluePeak Plastics"),
    ],
)
def test_vendor_names_that_should_not_match(a, b):
    assert vendor_similarity(a, b) < 0.85


# ---------------------------------------------------------------------------
# check_po_match
# ---------------------------------------------------------------------------


def test_clean_invoice_matching_its_po_has_no_flags():
    assert _flags(_record(), _po()) == []


def test_no_po_match_means_no_po_flags():
    assert _flags(_record(vendor="Totally Different"), None) == []
    assert _flags(_record(vendor="Totally Different"), _po(matched=False)) == []


def test_vendor_mismatch_is_flagged():
    flags = _flags(_record(vendor="Globex Trading Co"), _po())
    assert len(flags) == 1
    assert flags[0].startswith("PO vendor check:")
    assert "Globex Trading Co" in flags[0] and "BluePeak Plastics" in flags[0]


def test_vendor_check_can_be_disabled(firestore_backend):
    _rules(firestore_backend, po_vendor_min_similarity=0)
    assert _flags(_record(vendor="Globex Trading Co"), _po()) == []


def test_invoice_over_po_amount_is_flagged():
    flags = _flags(_record(total="4000.00"), _po())  # 23% over a 3,250 PO
    assert len(flags) == 1 and flags[0].startswith("PO amount check:")
    assert "4,000.00" in flags[0] and "3,250.00" in flags[0] and "5%" in flags[0]


def test_invoice_within_tolerance_or_below_po_is_fine():
    assert _flags(_record(total="3400.00"), _po()) == []  # +4.6%, inside 5%
    assert _flags(_record(total="1000.00"), _po()) == []  # partial billing


def test_amount_tolerance_is_a_policy(firestore_backend):
    rec = _record(total="3400.00")
    _rules(firestore_backend, po_amount_tolerance_pct=0)
    assert any("PO amount check" in f for f in _flags(rec, _po()))
    _rules(firestore_backend, po_amount_tolerance_pct=None)
    assert _flags(_record(total="9999.00"), _po()) == []  # check disabled


def test_amount_check_converts_currencies():
    # EUR 3,000 ~ USD 3,240 (1.08) vs a USD 3,250 PO: fine.
    assert _flags(_record(total="3000.00", currency="EUR"), _po()) == []
    # EUR 4,000 ~ USD 4,320 vs USD 3,250: over.
    assert any("PO amount check" in f for f in _flags(_record(total="4000.00", currency="EUR"), _po()))


@pytest.mark.parametrize("status", ["closed", "cancelled", "fully_invoiced", "CLOSED"])
def test_blocked_po_status_is_flagged(status):
    flags = _flags(_record(), _po(po_status=status))
    assert len(flags) == 1 and flags[0].startswith("PO status check:")


@pytest.mark.parametrize("status", ["open", "partially_invoiced", "approved", None])
def test_open_po_statuses_are_fine(status):
    assert _flags(_record(), _po(po_status=status)) == []


def test_blocked_statuses_are_a_policy(firestore_backend):
    _rules(firestore_backend, blocked_po_statuses=["on_hold"])
    assert _flags(_record(), _po(po_status="closed")) == []
    assert len(_flags(_record(), _po(po_status="on_hold"))) == 1


def test_missing_invoice_fields_skip_the_checks():
    rec = _record(vendor=None, total=None)
    assert _flags(rec, _po(po_status="open")) == []


# ---------------------------------------------------------------------------
# Rule engine + validation card
# ---------------------------------------------------------------------------


def test_po_flags_send_the_invoice_to_human_review():
    route, flags = n._check_rules(InvoiceRecord(**_record(vendor="Globex Trading Co")), None, _po())
    assert route == "human_review"
    assert any(f.startswith("PO vendor check:") for f in flags)


def test_same_invoice_without_a_po_match_still_proceeds():
    route, flags = n._check_rules(InvoiceRecord(**_record(vendor="Globex Trading Co")), None, None)
    assert (route, flags) == ("proceed", [])


def test_clean_matched_invoice_proceeds():
    assert n._check_rules(InvoiceRecord(**_record()), None, _po()) == ("proceed", [])


def _row(summary, label):
    return next(line for line in summary.splitlines() if label in line)


def test_validation_card_marks_po_checks_pass_flag_and_na():
    rec = InvoiceRecord(**_record(vendor="Globex Trading Co"))
    route, flags = n._check_rules(rec, None, _po())
    summary = n._format_validation_summary(rec, route, flags, None, _po())
    assert _row(summary, "Invoice vendor matches the PO vendor").rstrip().endswith("| Flag |")
    assert _row(summary, "PO is open for invoicing").rstrip().endswith("| Pass |")
    assert _row(summary, "Invoice total within the PO amount").rstrip().endswith("| Pass |")

    # unmatched PO: those rows are n/a rather than a misleading Pass
    summary = n._format_validation_summary(rec, "proceed", [], None, _po(matched=False))
    assert "n/a (no PO match)" in _row(summary, "PO is open for invoicing")
    assert "n/a (no PO match)" in _row(summary, "Invoice vendor matches the PO vendor")


def test_validate_invoice_uses_the_po_match_from_state():
    ctx = SimpleNamespace(
        state={"invoice_record": _record(vendor="Globex Trading Co"), "po_match": _po()}, resume_inputs={}
    )
    ev = n.validate_invoice._func(ctx, _record(vendor="Globex Trading Co"))
    assert ev.actions.route == "human_review"
    assert any("PO vendor check" in f for f in ev.actions.state_delta["validation_flags"])


# ---------------------------------------------------------------------------
# classify_spend / route_posting
# ---------------------------------------------------------------------------


def test_classify_forwards_the_invoice_record_for_validation():
    rec = _record()
    ev = n.classify_spend._func(SimpleNamespace(state={"invoice_record": rec}), rec)
    assert ev.output == rec  # validate_invoice reads its input as the invoice record
    assert ev.actions.state_delta["po_match"]["matched"] is True


@pytest.mark.parametrize("route", ["direct", "indirect"])
def test_route_posting_follows_the_po_match(route):
    ev = n.route_posting._func(SimpleNamespace(state={"po_match": {"route": route}}), None)
    assert ev.actions.route == "erp"


def test_route_posting_falls_back_to_the_policy_route(firestore_backend):
    ev = n.route_posting._func(SimpleNamespace(state={}), None)
    assert ev.actions.route == "erp"
    _rules(firestore_backend, unmatched_po_route="indirect")
    ev = n.route_posting._func(SimpleNamespace(state={}), None)
    assert ev.actions.route == "erp"


# ---------------------------------------------------------------------------
# Approval message shows the PO
# ---------------------------------------------------------------------------


def test_approval_message_shows_the_matched_po():
    rec = _record()
    msg = _format_approval_message(rec, ["flag"], None, None, _po())
    assert "| PO match | Direct -> SAP · BluePeak Plastics · USD 3,250.00 · open |" in msg


def test_approval_message_says_when_the_po_was_not_found():
    msg = _format_approval_message(_record(), ["flag"], None, None, _po(matched=False, route="indirect"))
    assert "No match in the PO ledger - defaults to Indirect spend" in msg


def test_approval_message_without_a_po_lookup_has_no_po_row():
    assert "| PO match |" not in _format_approval_message(_record(), ["flag"])


# ---------------------------------------------------------------------------
# AP-Error: failed runs are labelled for retry, not "processed"
# ---------------------------------------------------------------------------


def _recorders(monkeypatch):
    calls = {"processed": [], "no_action": [], "error": []}
    monkeypatch.setattr(n.agentmail_tools, "mark_processed", lambda m: calls["processed"].append(m))
    monkeypatch.setattr(n.agentmail_tools, "mark_no_action", lambda m: calls["no_action"].append(m))
    monkeypatch.setattr(n.agentmail_tools, "mark_error", lambda m: calls["error"].append(m))
    return calls


def _complete(state):
    ctx = SimpleNamespace(state=dict(state), resume_inputs={})
    return list(n.pipeline_complete._func(ctx, None))


def test_failed_run_is_labelled_error_not_processed(monkeypatch):
    calls = _recorders(monkeypatch)
    events = _complete(
        {"invoice_message_id": "m1", "pipeline_error": "Firestore outage", "pipeline_error_node": "classify_spend"}
    )
    assert calls["error"] == ["m1"]
    assert calls["processed"] == [] and calls["no_action"] == []
    assert events[0].actions.state_delta["pipeline_status"] == "error"
    assert events[1].output == {"status": "error"}


def test_failed_run_summary_names_the_step_and_the_error(monkeypatch):
    _recorders(monkeypatch)
    text = _complete(
        {"invoice_message_id": "m1", "pipeline_error": "Firestore outage", "pipeline_error_node": "classify_spend"}
    )[0].content.parts[0].text
    assert "Processing failed" in text
    assert "classify_spend" in text and "Firestore outage" in text
    assert "AP-Error" in text
    assert "Vendor notification" not in text  # no misleading "email queued" table


@pytest.mark.parametrize("status", ["posted_erp", "escalated", "rejected"])
def test_completed_runs_are_still_processed_even_with_an_error_recorded(monkeypatch, status):
    calls = _recorders(monkeypatch)
    _complete({"invoice_message_id": "m1", "pipeline_status": status, "pipeline_error": "stale"})
    assert calls["processed"] == ["m1"] and calls["error"] == []


def test_discarded_stays_no_action(monkeypatch):
    calls = _recorders(monkeypatch)
    _complete({"invoice_message_id": "m1", "pipeline_status": "discarded"})
    assert calls["no_action"] == ["m1"] and calls["error"] == []


def test_mark_error_swaps_in_progress_for_error(monkeypatch):
    updates = []

    class _Messages:
        def update(self, inbox_id, message_id, **kwargs):
            updates.append(kwargs)

    monkeypatch.setattr(
        agentmail_tools, "_client", SimpleNamespace(inboxes=SimpleNamespace(messages=_Messages()))
    )
    monkeypatch.setattr(agentmail_tools, "AGENTMAIL_INBOX_ID", "ap@agentmail.to")
    assert agentmail_tools.mark_error("m1") is True
    assert updates == [
        {"add_labels": [agentmail_tools.ERROR_LABEL], "remove_labels": [agentmail_tools.IN_PROGRESS_LABEL]}
    ]


def test_an_errored_message_is_unhandled_so_it_gets_retried():
    assert agentmail_tools.is_handled([agentmail_tools.ERROR_LABEL]) is False
    # a later successful retry keeps the old AP-Error label but is handled
    assert agentmail_tools.is_handled([agentmail_tools.ERROR_LABEL, agentmail_tools.PROCESSED_LABEL]) is True
    # ...and while the retry is running it is in progress, so it isn't picked up twice
    assert agentmail_tools.is_handled([agentmail_tools.ERROR_LABEL, agentmail_tools.IN_PROGRESS_LABEL]) is True


def test_forced_fetch_failure_hands_the_message_back_as_error(monkeypatch):
    marked = []

    def boom(_id):
        raise RuntimeError("agentmail down")

    monkeypatch.setattr(agentmail_tools, "fetch_invoice", boom)
    monkeypatch.setattr(agentmail_tools, "mark_error", lambda m: marked.append(m))
    ctx = SimpleNamespace(invocation_id="i1", state={})
    events = list(n.fetch_invoice_email._func(ctx, "MSG_ID=abc123"))
    assert marked == ["abc123"]
    assert "NO_INVOICE_FOUND" in events[-1].actions.state_delta["invoice_email_raw"]


# ---------------------------------------------------------------------------
# Memory: the new flags get stable kinds (and do not leak into "over_limit")
# ---------------------------------------------------------------------------


def test_memory_classifies_the_po_flags():
    from app import memory

    assert memory.flag_kinds(['PO vendor check: invoice vendor "A" does not match vendor "B" on PO P']) == [
        "po_vendor_mismatch"
    ]
    assert memory.flag_kinds(["PO amount check: invoice total USD 5.00 is above PO P amount USD 1.00 (tolerance 5%)"]) == [
        "over_po_amount"
    ]
    assert memory.flag_kinds(['PO status check: PO P is "closed" and not open for invoicing']) == ["po_not_open"]


# ---------------------------------------------------------------------------
# Graph wiring: the PO lookup runs BEFORE validation
# ---------------------------------------------------------------------------


def _edges():
    from app.agent import root_agent

    return {
        (e.from_node.name, e.to_node.name, getattr(e, "route", None)) for e in root_agent.graph.edges
    }


def test_graph_looks_up_the_po_before_validation():
    edges = _edges()
    assert ("emit_extraction_card", "classify_spend", None) in edges
    assert ("classify_spend", "validate_invoice", "classified") in edges
    # a failed lookup ends the run (-> AP-Error) instead of validating without the PO
    assert ("classify_spend", "pipeline_complete", "default") in edges
    assert not any(src == "emit_extraction_card" and dst == "validate_invoice" for src, dst, _ in edges)


def test_graph_posts_via_route_posting_after_validation_or_approval():
    edges = _edges()
    assert ("validate_invoice", "route_posting", "proceed") in edges
    assert ("human_approval_gate", "route_posting", "approve") in edges
    assert ("route_posting", "post_to_erp", "erp") in edges
    assert not any(src == "classify_spend" and dst == "post_to_erp" for src, dst, _ in edges)
