"""Approval-card briefing: LLM path, deterministic fallback, and gate wiring."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import review_summary as rs

RECORD = {
    "vendor": {"value": "Meridian Steel & Alloys"},
    "invoice_no": {"value": "INV-2026-0303"},
    "po_ref": {"value": "PO-2026-1010"},
    "total": {"value": "12500.00"},
    "currency": {"value": "USD"},
    "line_items": [{"name": "Steel plate", "amount": "12500.00"}],
}
FLAGS = ["Invoice total USD 12,500.00 exceeds the AP auto-approval limit of $4,000.00. Human approval is required."]


def test_fallback_summary_is_short_bolds_key_facts_and_has_two_paragraphs():
    text = rs.fallback_summary(RECORD, FLAGS)
    paras = text.split("\n\n")
    assert len(paras) == 2
    assert "**Meridian Steel & Alloys**" in paras[0] and "**USD 12,500.00**" in paras[0]
    assert "auto-approval limit" in paras[0]
    assert len(text.split()) < 70


def _what(flags):
    return rs.fallback_summary(RECORD, flags).split("\n\n")[0]


def test_fallback_lists_up_to_two_findings_and_counts_the_rest():
    f1, f2, f3, f4 = "First finding. Detail.", "Second finding.", "Third finding.", "Fourth finding."
    one = _what([f1])
    assert "First finding" in one and "more finding" not in one
    # Two findings are both named, not collapsed into "+1 more".
    two = _what([f1, f2])
    assert "First finding; Second finding" in two and "more finding" not in two
    assert "Detail" not in two  # still only the first sentence of each finding
    # Beyond two, the remainder is counted with correct pluralisation.
    three = _what([f1, f2, f3])
    assert "First finding; Second finding" in three and "Third" not in three
    assert "(+1 more finding.)" in three
    assert "(+2 more findings.)" in _what([f1, f2, f3, f4])


def test_disabled_returns_fallback_without_calling_the_model():
    out = asyncio.run(rs.generate_review_summary(RECORD, FLAGS))
    assert out["source"] == "fallback" and out["model"] is None and out["text"]


WHAT = "**Meridian Steel & Alloys** billed **USD 12,500.00** on invoice **INV-2026-0303** against PO **PO-2026-1010**."
CHECK = "Confirm the total and PO number against the original document."


class _Models:
    """Stand-in for client.aio.models; ``reply`` is the raw text the model returns."""

    def __init__(self, reply=None, fail=False):
        self.reply, self.fail, self.prompt, self.config = reply, fail, None, None

    async def generate_content(self, *, model, contents, config):
        if self.fail:
            raise RuntimeError("429")
        self.prompt, self.config = contents, config
        return SimpleNamespace(text=self.reply)


def _run(monkeypatch, models, **kw):
    monkeypatch.setattr(rs, "ENABLED", True)
    import google.genai as genai

    monkeypatch.setattr(genai, "Client", lambda **_: SimpleNamespace(aio=SimpleNamespace(models=models)))
    return asyncio.run(rs.generate_review_summary(RECORD, FLAGS, **kw))


def test_llm_path_returns_two_clean_paragraphs(monkeypatch):
    models = _Models(json.dumps({"what": WHAT, "check": CHECK}))
    out = _run(monkeypatch, models, threshold_usd=4000)
    assert out["source"] == "llm" and out["model"]
    assert out["text"] == f"{WHAT}\n\n{CHECK}"
    assert "Meridian Steel" in models.prompt and "USD 4,000" in models.prompt
    # Structured output is what keeps drafting notes out of the card.
    assert models.config.response_mime_type == "application/json"
    assert models.config.response_schema is rs._Briefing


@pytest.mark.parametrize(
    "reply",
    [
        # The model's drafting leaked into the answer (the bug seen in production).
        json.dumps({"what": "(Wait, let's include invoice) Let's adjust bolds: 1. **Northwind**", "check": CHECK}),
        json.dumps({"what": WHAT, "check": "Verify the total.\nAlso the PO."}),  # line break inside a field
        json.dumps({"what": "**unbalanced bold " + WHAT, "check": CHECK}),
        json.dumps({"what": "x" * 500, "check": CHECK}),  # far beyond 2-3 lines
        json.dumps({"what": WHAT}),  # missing field
        "not json at all",
        "",
    ],
)
def test_malformed_or_leaky_model_output_falls_back(monkeypatch, reply):
    out = _run(monkeypatch, _Models(reply))
    assert out["source"] == "fallback" and "Meridian Steel" in out["text"]


def test_model_failure_falls_back(monkeypatch):
    out = _run(monkeypatch, _Models(fail=True))
    assert out["source"] == "fallback"


# ---------------------------------------------------------------------------
# Agent Memory note (LLM rewrite of the precedent tally, template as fallback)
# ---------------------------------------------------------------------------

PRECEDENT = {
    "vendor": "Meridian Steel & Alloys",
    "flag_kinds": ["over_limit"],
    "total_count": 3,
    "approve_count": 3,
    "reject_count": 0,
    "recommendation": "approve",
    "approved_amounts": ["USD 11200.00", "USD 9800.00", "USD 10100.00"],
    "rejected_amounts": [],
    "narrative": "TEMPLATE SENTENCE from app/memory.py.",
}
NOTE = (
    "**Meridian Steel & Alloys** invoices over the limit were approved **3 of 3** times before "
    "(**USD 9,800 to USD 11,200**), and Agent Memory advises **approve**. This is advisory only."
)


def _run_memory(monkeypatch, models, precedent=PRECEDENT):
    monkeypatch.setattr(rs, "ENABLED", True)
    import google.genai as genai

    monkeypatch.setattr(genai, "Client", lambda **_: SimpleNamespace(aio=SimpleNamespace(models=models)))
    return asyncio.run(rs.generate_memory_summary(precedent, RECORD, FLAGS, 4000))


def test_memory_note_uses_the_model_and_reports_token_usage(monkeypatch):
    usage = SimpleNamespace(
        prompt_token_count=410, candidates_token_count=60, thoughts_token_count=25, cached_content_token_count=0
    )

    class _M(_Models):
        async def generate_content(self, **kw):
            out = await super().generate_content(**kw)
            out.usage_metadata = usage
            return out

    models = _M(json.dumps({"text": NOTE}))
    out = _run_memory(monkeypatch, models)
    assert out["source"] == "llm" and out["text"] == NOTE and out["model"]
    assert (out["input_tokens"], out["output_tokens"], out["thinking_tokens"]) == (410, 60, 25)
    assert models.config.response_schema is rs._MemoryNote
    # The model is given the tally, the amounts and the rule-based recommendation.
    assert "3 approved, 0 rejected" in models.prompt and "USD 9800.00" in models.prompt
    assert "recommendation: approve" in models.prompt


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps({"text": "Earlier decisions were mostly approvals; nothing else to add."}),  # drops the recommendation
        json.dumps({"text": "Wait, let's rewrite: **Meridian** approved **approve**."}),  # drafting leak
        json.dumps({"text": "x" * 500}),
        "not json",
    ],
)
def test_bad_memory_note_falls_back_to_the_template_sentence(monkeypatch, reply):
    out = _run_memory(monkeypatch, _Models(reply))
    assert out["source"] == "fallback" and out["text"] == PRECEDENT["narrative"]


def test_memory_note_without_a_recommendation_needs_none(monkeypatch):
    mixed = {**PRECEDENT, "approve_count": 1, "reject_count": 1, "total_count": 2, "recommendation": None}
    text = "Reviewers were split on **Meridian Steel & Alloys**: **1 approved**, **1 rejected**."
    out = _run_memory(monkeypatch, _Models(json.dumps({"text": text})), mixed)
    assert out["source"] == "llm" and out["text"] == text


def test_memory_note_model_failure_and_disabled_fall_back(monkeypatch):
    assert _run_memory(monkeypatch, _Models(fail=True))["source"] == "fallback"
    monkeypatch.setattr(rs, "ENABLED", False)
    out = asyncio.run(rs.generate_memory_summary(PRECEDENT, RECORD, FLAGS))
    assert out["source"] == "fallback" and out["text"] == PRECEDENT["narrative"]


def test_review_summary_also_reports_token_usage(monkeypatch):
    class _M(_Models):
        async def generate_content(self, **kw):
            out = await super().generate_content(**kw)
            out.usage_metadata = SimpleNamespace(
                prompt_token_count=300, candidates_token_count=80, thoughts_token_count=0, cached_content_token_count=12
            )
            return out

    out = _run(monkeypatch, _M(json.dumps({"what": WHAT, "check": CHECK})))
    assert (out["input_tokens"], out["output_tokens"], out["cache_read_tokens"]) == (300, 80, 12)
