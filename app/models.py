"""Centralized Gemini model configuration.

Override any model at runtime via environment variable — no code change needed:
  GEMINI_MODEL_SCREENING=gemini-3.5-flash-lite
  GEMINI_MODEL_EXTRACTION=gemini-3.8-flash
  GEMINI_MODEL_DRAFT_EMAIL=gemini-3.5-flash-lite

For Cloud Run: pass via --update-env-vars or update deployment/deploy.sh.
For local dev:  uv run --env-file .env agents-cli playground
"""

import os

MODEL_SCREENING:   str = os.getenv("GEMINI_MODEL_SCREENING",   "gemini-3.5-flash-lite")
MODEL_EXTRACTION:  str = os.getenv("GEMINI_MODEL_EXTRACTION",  "gemini-3.8-flash")
MODEL_DRAFT_EMAIL: str = os.getenv("GEMINI_MODEL_DRAFT_EMAIL", "gemini-3.5-flash-lite")

# Extra tiers route_extraction_strategy (app/nodes/extraction.py) can switch
# extraction_agent into for a given invoice's size/complexity — cheapest for a
# small native-text single-pager, priciest for a scanned/very-large one.
# Applied by default (see EXTRACTION_DYNAMIC_MODEL_TIERING in app/config.py).
MODEL_EXTRACTION_LITE: str = os.getenv("GEMINI_MODEL_EXTRACTION_LITE", "gemini-3.5-flash-lite")
MODEL_EXTRACTION_PRO:  str = os.getenv("GEMINI_MODEL_EXTRACTION_PRO",  "gemini-3.1-pro-preview")

ALLOWED_MODELS: frozenset[str] = frozenset({
    MODEL_SCREENING, MODEL_EXTRACTION, MODEL_DRAFT_EMAIL,
    MODEL_EXTRACTION_LITE, MODEL_EXTRACTION_PRO,
})

# Maps route_extraction_strategy's size/complexity tier name to the Gemini
# model it selects. Values must be members of ALLOWED_MODELS.
EXTRACTION_MODEL_TIERS: dict[str, str] = {
    "lite": MODEL_EXTRACTION_LITE,
    "flash": MODEL_EXTRACTION,
    "pro": MODEL_EXTRACTION_PRO,
}

# Thinking depth applied per extraction tier (route_extraction_strategy).
# Independent of EXTRACTION_DYNAMIC_MODEL_TIERING: that flag gates the
# unvalidated MODEL swap; thinking depth is a planner knob on whichever model
# is already running, safe to apply immediately. Values must be members of
# google.genai.types.ThinkingLevel (MINIMAL/LOW/MEDIUM/HIGH).
EXTRACTION_THINKING_TIERS: dict[str, str] = {
    "lite": "MEDIUM",
    "flash": "MEDIUM",
    "pro": "HIGH",
}

# extraction_agent's max_output_tokens ceiling (app/agent.py). Centralized here
# so app/nodes/extraction.py's large-invoice truncation check compares against
# the SAME ceiling actually configured on the agent, instead of a copy-pasted
# literal that can silently drift out of sync. Set to the models' actual
# ceiling (gemini-3.1-pro-preview / gemini-3.5-flash both support 65536) —
# thinking tokens share this same budget with the final response, and a
# HIGH-thinking pass on a large document can otherwise truncate the
# structured JSON mid-string before it finishes (see EXTRACTION_THINKING_TIERS).
EXTRACTION_MAX_OUTPUT_TOKENS = 65536  # gemini-3.8-flash / gemini-3.1-pro-preview both support 65536

# Pricing: USD per 1M tokens, standard (synchronous) tier, ≤200k prompt.
# Source: https://ai.google.dev/gemini-api/docs/pricing (2026-09-21). Introductory
# rate for gemini-3.8-flash; rises to $1.50/$7.50 on 2027-01-01 — revisit then.
PRICING: dict[str, dict[str, float]] = {
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00},
    "gemini-3.8-flash":       {"input": 0.75, "output":  3.75},
    "gemini-3.5-flash-lite":  {"input": 0.30, "output":  2.50},
}

MODEL_LABELS: dict[str, str] = {
    "gemini-3.1-pro-preview": "3.1 Pro",
    "gemini-3.8-flash":       "3.8 Flash",
    "gemini-3.5-flash-lite":  "3.5 Flash-Lite",
}
