"""Tests for the AP AgentMail label lifecycle: intake -> AP-InProgress, then a
final AP-Processed (handled invoice) or AP-NoAction (screened-out non-invoice),
applied by pipeline_complete. Regression for noise emails being marked
AP-Processed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app import agentmail_tools
from app import nodes as n


def _ctx(state: dict) -> Any:
    return SimpleNamespace(state=dict(state), resume_inputs={})


def _drain(node, ctx) -> list:
    return list(node._func(ctx, None))


def _recorders(monkeypatch):
    calls: dict[str, list] = {"processed": [], "no_action": []}
    monkeypatch.setattr(n.agentmail_tools, "mark_processed", lambda m: calls["processed"].append(m))
    monkeypatch.setattr(n.agentmail_tools, "mark_no_action", lambda m: calls["no_action"].append(m))
    return calls


def test_pipeline_complete_marks_no_action_for_discarded(monkeypatch):
    calls = _recorders(monkeypatch)
    _drain(n.pipeline_complete, _ctx({"invoice_message_id": "m1", "pipeline_status": "discarded"}))
    assert calls["no_action"] == ["m1"]
    assert calls["processed"] == []


def test_pipeline_complete_marks_processed_for_posted(monkeypatch):
    calls = _recorders(monkeypatch)
    _drain(n.pipeline_complete, _ctx({"invoice_message_id": "m1", "pipeline_status": "posted_erp"}))
    assert calls["processed"] == ["m1"]
    assert calls["no_action"] == []


def test_pipeline_complete_no_label_without_message_id(monkeypatch):
    calls = _recorders(monkeypatch)
    _drain(n.pipeline_complete, _ctx({"pipeline_status": "posted_erp"}))
    assert calls["processed"] == []
    assert calls["no_action"] == []


def test_mark_no_action_payload_swaps_labels(monkeypatch):
    updates: list[dict] = []

    class _Messages:
        def update(self, inbox_id, message_id, **kwargs):
            updates.append({"inbox_id": inbox_id, "message_id": message_id, **kwargs})

    fake = SimpleNamespace(inboxes=SimpleNamespace(messages=_Messages()))
    monkeypatch.setattr(agentmail_tools, "_client", fake)
    monkeypatch.setattr(agentmail_tools, "AGENTMAIL_INBOX_ID", "ap@agentmail.to")

    assert agentmail_tools.mark_no_action("m1") is True
    assert updates == [
        {
            "inbox_id": "ap@agentmail.to",
            "message_id": "m1",
            "add_labels": [agentmail_tools.NO_ACTION_LABEL],
            "remove_labels": [agentmail_tools.IN_PROGRESS_LABEL],
        }
    ]


def test_label_failure_is_best_effort(monkeypatch):
    class _Messages:
        def update(self, *a, **k):
            raise RuntimeError("boom")

    fake = SimpleNamespace(inboxes=SimpleNamespace(messages=_Messages()))
    monkeypatch.setattr(agentmail_tools, "_client", fake)
    monkeypatch.setattr(agentmail_tools, "AGENTMAIL_INBOX_ID", "ap@agentmail.to")

    assert agentmail_tools.label_in_progress("m1") is False
