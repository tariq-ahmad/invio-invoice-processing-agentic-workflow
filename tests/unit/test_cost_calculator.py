"""Tests for thinking-token cost accounting: Gemini bills thinking tokens at
the same per-token rate as output tokens, but the API reports them separately
(thoughts_token_count vs candidates_token_count) — compute_cost/
compute_all_model_costs must fold them in, and pipeline_complete's
_build_cost_summary must surface them per-agent for the UI.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.cost_calculator import compute_all_model_costs, compute_cost
from app.nodes import summary as summary_mod

_MODEL = "gemini-3.8-flash"


def test_compute_cost_bills_thinking_tokens_at_output_rate():
    with_thinking = compute_cost(_MODEL, 1_000, 500, thinking_tokens=300)
    folded_into_output = compute_cost(_MODEL, 1_000, 800, thinking_tokens=0)
    assert with_thinking == folded_into_output


def test_compute_cost_defaults_thinking_tokens_to_zero():
    assert compute_cost(_MODEL, 1_000, 500) == compute_cost(_MODEL, 1_000, 500, thinking_tokens=0)


def test_compute_all_model_costs_includes_thinking_for_every_model():
    without = compute_all_model_costs(1_000, 500)
    with_thinking = compute_all_model_costs(1_000, 500, thinking_tokens=1_000)
    for model, cost in with_thinking.items():
        assert cost > without[model]


def _ctx(state: dict) -> Any:
    return SimpleNamespace(state=dict(state), resume_inputs={})


def test_build_cost_summary_surfaces_real_thinking_tokens():
    ctx = _ctx(
        {
            "screening_input_tokens": 100,
            "screening_output_tokens": 20,
            "screening_thinking_tokens": 40,
            "extraction_input_tokens": 2_000,
            "extraction_output_tokens": 300,
            "extraction_thinking_tokens": 900,
        }
    )
    result = summary_mod._build_cost_summary(ctx)

    assert result["agents"]["screening"]["thinking_tokens"] == 40
    assert result["agents"]["extraction"]["thinking_tokens"] == 900
    assert result["totals"]["thinking_tokens"] == 940

    # Cost for the active model must be strictly higher than a naive
    # (input, output)-only calc that ignores the 900 extraction thinking tokens.
    naive = compute_cost(_MODEL, 2_000, 300)
    with_thinking = result["agents"]["extraction"]["costs"][_MODEL]
    assert with_thinking > naive


def test_build_cost_summary_estimated_fallback_has_zero_thinking_tokens():
    # No real token counts in state at all -> STATIC_ESTIMATES fallback path;
    # we have no static estimate for thinking depth, so it must default to 0
    # rather than silently reusing a stale/real number from state.
    result = summary_mod._build_cost_summary(_ctx({}))

    assert result["agents"]["screening"]["estimated"] is True
    assert result["agents"]["screening"]["thinking_tokens"] == 0
    assert result["agents"]["extraction"]["thinking_tokens"] == 0


def test_build_cost_summary_counts_card_briefings_only_when_a_model_wrote_them():
    llm = {"source": "llm", "input_tokens": 400, "output_tokens": 70, "thinking_tokens": 30}
    fallback = {"source": "fallback", "input_tokens": 999}
    result = summary_mod._build_cost_summary(_ctx({"review_summary": llm, "memory_summary": fallback}))

    assert result["agents"]["review_summary"]["input_tokens"] == 400
    assert result["agents"]["review_summary"]["estimated"] is False
    assert "memory_summary" not in result["agents"]  # template text costs nothing

    base = summary_mod._build_cost_summary(_ctx({}))["totals"]
    assert result["totals"]["input_tokens"] == base["input_tokens"] + 400
    assert result["totals"]["thinking_tokens"] == base["thinking_tokens"] + 30
