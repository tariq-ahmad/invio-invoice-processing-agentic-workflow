"""Run-outcome records and the aggregate behind GET /stats (app/run_stats.py)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Response

from app import config
from app.cost_calculator import compute_cost
from app.firestore_store import FailingBackend, InMemoryBackend, set_backend
from app.policy import get_policy
from app.run_stats import (
    MAX_RUNS_READ,
    build_outcome_record,
    get_run_stats,
    record_run_outcome,
    summarize_outcomes,
)

NOW = "2026-10-15T10:00:00+00:00"


def _state(**over):
    base = {
        "invoice_record": {
            "vendor": {"value": "Acme Corp"},
            "invoice_no": {"value": "INV-1042"},
            "total": {"value": "1,200.00"},
            "currency": {"value": "USD"},
        },
        "validation_route": "proceed",
        "extraction_latency_ms": 1800,
        "invoice_message_id": "msg-1",
    }
    base.update(over)
    return base


def _build(state, status="posted_erp", run_id="run-1"):
    return build_outcome_record(state, status=status, run_id=run_id, recorded_at=NOW)


# ---------------------------------------------------------------------------
# What a run records
# ---------------------------------------------------------------------------


def test_runs_that_never_extracted_an_invoice_are_not_recorded():
    assert _build({"pipeline_status": "discarded"}, status="discarded") is None
    assert _build({"invoice_record": {}}, status="error") is None


def test_a_security_quarantine_is_recorded_even_without_an_extracted_invoice():
    rec = _build({"invoice_message_id": "msg-9"}, status="quarantined", run_id="q-1")
    assert rec["status"] == "quarantined" and rec["run_id"] == "q-1"
    assert not rec["posted"] and not rec["touchless"]
    assert rec["vendor"] is None and rec["amount_usd"] is None
    assert rec["message_id"] == "msg-9"


def test_a_quarantined_invoice_adds_no_value_to_the_total():
    rec = _build(_state(), status="quarantined")
    assert rec["amount_usd"] is None and not rec["posted"]


def test_a_straight_through_post_is_touchless():
    rec = _build(_state())
    assert rec["posted"] and not rec["exception"] and rec["touchless"]
    assert rec["amount"] == 1200.0 and rec["amount_usd"] == 1200.0
    assert rec["vendor"] == "Acme Corp" and rec["invoice_no"] == "INV-1042"
    assert rec["decision_ms"] == 1800
    assert rec["message_id"] == "msg-1"


def test_a_human_approved_post_counts_as_sent_to_review_not_auto_posted():
    rec = _build(_state(validation_route="human_review"))
    assert rec["posted"] and rec["exception"] and rec["human_review"]
    assert not rec["touchless"]


@pytest.mark.parametrize(
    ("status", "extra"),
    [
        ("escalated", {"validation_route": "duplicate_po"}),
        ("rejected", {"validation_route": "human_review", "notification_result": {"ok": 1}}),
        ("error", {}),
    ],
)
def test_escalations_rejections_and_failures_are_exceptions(status, extra):
    rec = _build(_state(**extra), status=status)
    assert rec["exception"] and not rec["touchless"] and not rec["posted"]


def test_non_usd_totals_are_converted_with_the_policy_fx_table():
    state = _state()
    state["invoice_record"]["currency"] = {"value": "EUR"}
    state["invoice_record"]["total"] = {"value": "EUR 1,000.00"}
    rec = _build(state)
    assert rec["amount"] == 1000.0
    assert rec["amount_usd"] == round(get_policy().to_usd(1000.0, "EUR"), 2)


def test_cost_prices_each_call_at_the_model_it_used():
    rec = _build(
        _state(
            screening_model="gemini-3.5-flash-lite",
            screening_input_tokens=2000,
            screening_output_tokens=100,
            extraction_model="gemini-3.8-flash",
            extraction_input_tokens=3000,
            extraction_output_tokens=500,
            extraction_thinking_tokens=200,
            extraction_cache_read_tokens=1000,
        )
    )
    expected = compute_cost("gemini-3.5-flash-lite", 2000, 100) + compute_cost(
        "gemini-3.8-flash", 3000, 500, 200, 1000
    )
    assert rec["cost_usd"] == pytest.approx(expected, abs=1e-6)


def test_extractions_authoritative_cost_wins_over_recomputing():
    rec = _build(
        _state(
            extraction_model="gemini-3.8-flash",
            extraction_input_tokens=9999,
            extraction_output_tokens=9999,
            extraction_cost_usd=0.0421,
        )
    )
    assert rec["cost_usd"] == pytest.approx(0.0421)


def test_briefings_are_priced_only_when_a_model_wrote_them():
    llm = {"source": "llm", "model": "gemini-3.5-flash-lite", "input_tokens": 1000, "output_tokens": 200}
    fallback = {"source": "fallback", "model": None, "input_tokens": 1000, "output_tokens": 200}
    assert _build(_state(review_summary=llm))["cost_usd"] == pytest.approx(
        compute_cost("gemini-3.5-flash-lite", 1000, 200), abs=1e-6
    )
    assert _build(_state(review_summary=fallback))["cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Writing is best-effort
# ---------------------------------------------------------------------------


def test_recording_writes_one_document_keyed_by_the_run(firestore_backend):
    record_run_outcome(_state(), status="posted_erp", run_id="inv-abc")
    doc = firestore_backend.get(config.RUN_OUTCOMES_COLLECTION, "inv-abc")
    assert doc is not None and doc.data["status"] == "posted_erp"
    assert doc.data["recorded_at"]


def test_recording_the_same_run_twice_is_harmless(firestore_backend):
    record_run_outcome(_state(), status="posted_erp", run_id="inv-abc")
    record_run_outcome(_state(), status="posted_erp", run_id="inv-abc")  # re-execution
    assert len(firestore_backend.list_recent(config.RUN_OUTCOMES_COLLECTION, "recorded_at")) == 1


def test_a_firestore_outage_never_fails_the_invoice():
    set_backend(FailingBackend())
    record_run_outcome(_state(), status="posted_erp", run_id="inv-abc")  # must not raise


def test_a_missing_or_unusable_run_id_still_records(firestore_backend):
    record_run_outcome(_state(), status="posted_erp", run_id=None)
    record_run_outcome(_state(), status="posted_erp", run_id="bad/id")
    assert len(firestore_backend.list_recent(config.RUN_OUTCOMES_COLLECTION, "recorded_at")) == 2


def test_runs_without_an_invoice_write_nothing(firestore_backend):
    record_run_outcome({"pipeline_status": "discarded"}, status="discarded", run_id="x")
    assert firestore_backend.list_recent(config.RUN_OUTCOMES_COLLECTION, "recorded_at") == []


# ---------------------------------------------------------------------------
# The aggregate
# ---------------------------------------------------------------------------


def test_summary_totals_and_averages_inputs():
    docs = [
        {"status": "posted_erp", "touchless": True, "exception": False, "amount_usd": 100.0,
         "decision_ms": 2000, "cost_usd": 0.01, "recorded_at": "2026-10-01T00:00:00+00:00"},
        {"status": "posted_erp", "touchless": False, "exception": True, "amount_usd": 50.5,
         "decision_ms": 4000, "cost_usd": 0.03, "recorded_at": "2026-10-03T00:00:00+00:00"},
        {"status": "rejected", "touchless": False, "exception": True, "amount_usd": None,
         "decision_ms": 0, "cost_usd": 0, "recorded_at": "2026-10-02T00:00:00+00:00"},
    ]
    s = summarize_outcomes(docs)
    assert s["invoices"] == 3
    assert s["touchless_count"] == 1 and s["exception_count"] == 2
    assert s["amount_total_usd"] == 150.5
    assert (s["decision_ms_total"], s["decision_ms_count"]) == (6000, 2)  # zero-time runs excluded
    assert (s["cost_total_usd"], s["cost_count"]) == (pytest.approx(0.04), 2)
    assert s["by_status"] == {"posted_erp": 2, "rejected": 1}
    assert s["first_recorded_at"].startswith("2026-10-01") and s["last_recorded_at"].startswith("2026-10-03")


def test_summary_of_nothing_is_zeros_not_errors():
    s = summarize_outcomes([])
    assert s["invoices"] == 0 and s["amount_total_usd"] == 0 and s["first_recorded_at"] is None


def test_get_run_stats_reads_firestore_and_filters_by_window(firestore_backend):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    for i, days_ago in enumerate((0, 2, 10, 40)):
        at = (now - timedelta(days=days_ago)).isoformat(timespec="seconds")
        firestore_backend.set(
            config.RUN_OUTCOMES_COLLECTION, f"r{i}",
            {"status": "posted_erp", "touchless": True, "amount_usd": 10.0, "recorded_at": at},
        )
    assert get_run_stats(0, now=now)["invoices"] == 4
    assert get_run_stats(7, now=now)["invoices"] == 2
    assert get_run_stats(30, now=now)["invoices"] == 3
    out = get_run_stats(1, now=now)
    assert out["invoices"] == 1 and out["window_days"] == 1 and out["source"] == "firestore"
    assert get_run_stats(0, now=now)["window_days"] is None


def test_get_run_stats_flags_when_older_runs_were_cut_off(monkeypatch, firestore_backend):
    monkeypatch.setattr("app.run_stats.MAX_RUNS_READ", 3)
    for i in range(5):
        firestore_backend.set(
            config.RUN_OUTCOMES_COLLECTION, f"r{i}",
            {"status": "posted_erp", "recorded_at": f"2026-10-0{i + 1}T00:00:00+00:00"},
        )
    out = get_run_stats(0)
    assert out["invoices"] == 3 and out["truncated"] is True
    assert MAX_RUNS_READ == 2000  # the real bound


def test_get_run_stats_surfaces_an_outage_instead_of_reporting_zero():
    from app.firestore_store import ConfigStoreError

    set_backend(FailingBackend())
    with pytest.raises(ConfigStoreError):
        get_run_stats(0)


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_stats_endpoint_returns_totals_and_disables_caching(firestore_backend):
    from app.fast_api_app import get_stats

    firestore_backend.set(
        config.RUN_OUTCOMES_COLLECTION, "r1",
        {"status": "posted_erp", "touchless": True, "amount_usd": 99.0,
         "recorded_at": "2026-10-15T00:00:00+00:00"},
    )
    response = Response()
    out = get_stats(response, days=0)
    assert out["invoices"] == 1 and out["amount_total_usd"] == 99.0
    assert response.headers["cache-control"] == "no-store"


def test_stats_endpoint_is_503_on_an_outage_and_422_on_a_bad_window():
    from app.fast_api_app import get_stats

    with pytest.raises(HTTPException) as bad:
        get_stats(Response(), days=-1)
    assert bad.value.status_code == 422

    set_backend(FailingBackend())
    with pytest.raises(HTTPException) as down:
        get_stats(Response(), days=0)
    assert down.value.status_code == 503


def test_in_memory_list_recent_orders_newest_first_and_limits():
    backend = InMemoryBackend()
    for i in range(4):
        backend.set("c", f"d{i}", {"recorded_at": f"2026-10-0{i + 1}"})
    assert [d.id for d in backend.list_recent("c", "recorded_at", 2)] == ["d3", "d2"]
    assert backend.list_recent("missing", "recorded_at") == []
