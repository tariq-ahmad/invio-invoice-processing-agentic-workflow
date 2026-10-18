# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Gemini API cost calculator — standard tier, per-invoice cost projection.

Pricing source: https://ai.google.dev/gemini-api/docs/pricing (as of 2026-07-13)
All rates are USD per 1 M tokens, standard (synchronous) tier.
"""

from __future__ import annotations

from app.models import MODEL_LABELS
from app.models import PRICING as _PRICING

# ---------------------------------------------------------------------------
# Re-export for callers that import these directly from cost_calculator
# ---------------------------------------------------------------------------
MODEL_LABELS = MODEL_LABELS
MODELS: tuple[str, ...] = tuple(_PRICING.keys())

# Fallback estimates used when real token counts are unavailable.
# Values are (input_tokens, output_tokens) for a typical single-page invoice.
STATIC_ESTIMATES: dict[str, tuple[int, int]] = {
    "screening": (2_000, 120),    # email summary + PDF screening classification output
    "extraction": (2_500, 450),   # PDF multimodal + extraction JSON output
    "draft_email": (1_200, 500),  # invoice brief input + email body output
}


# ---------------------------------------------------------------------------
# Cost functions
# ---------------------------------------------------------------------------

def compute_cost(
    model: str, input_tokens: int, output_tokens: int,
    thinking_tokens: int = 0, cache_read_tokens: int = 0
) -> float:
    """Return USD cost for one call to `model` with the given token counts.

    Gemini bills thinking tokens at the SAME per-token rate as output tokens
    (no separate thinking price) — but the API reports them in a distinct
    usage_metadata field (thoughts_token_count vs candidates_token_count), so
    they're summed with output_tokens here rather than needing their own rate.

    cache_read_tokens are tokens served from Gemini implicit context cache,
    billed at 10% of the standard input rate (90% discount applied here).
    """
    p = _PRICING.get(model)
    if p is None:
        return 0.0
    base = (input_tokens * p["input"] + (output_tokens + thinking_tokens) * p["output"]) / 1_000_000
    savings = (cache_read_tokens * p["input"] * 0.90) / 1_000_000
    return base - savings


def compute_all_model_costs(
    input_tokens: int, output_tokens: int, thinking_tokens: int = 0
) -> dict[str, float]:
    """Return cost dict for all models keyed by model name."""
    return {m: compute_cost(m, input_tokens, output_tokens, thinking_tokens) for m in MODELS}
