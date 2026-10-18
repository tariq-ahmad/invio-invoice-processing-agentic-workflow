# ruff: noqa
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

"""Invoice-processing-agent - ADK 2.0 graph workflow.

Graph (START -> pipeline_complete):

    START
      └─► fetch_invoice_email
            └─► check_inbox
                  ├─[invoice_not_found]► inbox_empty (terminal)
                  ├─[quarantined]──────► quarantine_invoice (terminal; Model Armor flagged the email)
                  └─[invoice_found]────► screening_agent
                                              └─► screen_email_signal
                                                    ├─[not_invoice]──► discard_non_invoice (terminal)
                                                    └─[invoice]──────► route_extraction_strategy (size/tier router)
                                                                            └─► extraction_agent
                                                                                  └─► emit_extraction_card  (A2UI invoice card)
                                                                                        └─► classify_spend  (PO lookup in Firestore; BEFORE validation)
                                                                                              ├─[error]────► pipeline_complete (labelled AP-Error)
                                                                                              └─[classified]► validate_invoice  (rules + PO status/vendor/amount checks)
                                                                                                    ├─[proceed]─────► route_posting
                                                                                                    ├─[human_review]► recall_precedents (A2UI Agent Memory card) ─► human_approval_gate
                                                                                                    ├─[quarantined]─► quarantine_invoice (Model Armor flagged the extracted text) ─► pipeline_complete
                                                                                                    └─[duplicate_po]► raise_duplicate_alert ─► pipeline_complete
                                                                                                                          │
                                                                                                    human_approval_gate ──[approve]──► route_posting
                                                                                                                   └──[reject]──► prepare_rejection_brief ─► draft_rejection_email ─► review_email_draft
                                                                                                                                                                                          ├─[send]──► send_rejection_email ─► pipeline_complete
                                                                                                                                                                                          └─[revise]► prepare_rejection_brief (loop)
                                                                                                    route_posting ──[direct]──► post_to_erp ─► pipeline_complete
                                                                                                                  └─[indirect]► post_to_erp ─► pipeline_complete
"""

import logging
import os
import time

import google.auth
from google.adk.agents import LlmAgent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.planners import BuiltInPlanner
from google.adk.workflow import Workflow
from google.genai import types as genai_types

from app.nodes import (
    # Intake
    fetch_invoice_email,
    check_inbox,
    inbox_empty,
    quarantine_invoice,
    # Screening
    screen_email_signal,
    discard_non_invoice,
    # Extraction & validation
    route_extraction_strategy,
    emit_extraction_card,
    validate_invoice,
    # Human-in-the-loop / rejection path
    recall_precedents,
    human_approval_gate,
    prepare_rejection_brief,
    review_email_draft,
    send_rejection_email,
    raise_duplicate_alert,
    # Spend classification & posting
    classify_spend,
    route_posting,
    post_to_erp,
    # Terminal
    pipeline_complete,
)
from app.model_callbacks import (
    _apply_masked_text,
    _drop_model_turns,
    _inject_invoice_pdf,
    _strip_parts_by_mime,
)
from app.prompts import (
    DRAFT_EMAIL_INSTRUCTION,
    EXTRACTION_INSTRUCTION,
    INVOICE_SCREENING_INSTRUCTION,
)
from app.schemas import InvoiceRecord, InvoiceScreeningAssessment, RejectionEmail
from app.a2ui_views import A2UI_MIME

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GCP / Vertex AI bootstrap
# ---------------------------------------------------------------------------
_, _project_id = google.auth.default()
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", _project_id)
# Gemini calls target the `global` endpoint, NOT a single region. Gemini 2.x on
# Vertex uses Dynamic Shared Quota (capacity shared across all customers in a
# region), so pinning to one region (us-west1) throttles with 429
# RESOURCE_EXHAUSTED on regional congestion even at tiny volume. `global` pools
# capacity across regions and largely avoids that. (Cloud Run region is still
# us-west1 — set by the deploy --region flag, independent of this.)
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

# ---------------------------------------------------------------------------
# Model-latency instrumentation
# ---------------------------------------------------------------------------
# Measure how long each LLM agent's model call actually took and surface it
# (with the model name) to the UI via session state. The frontend reads
# state["<label>_model"] and state["<label>_latency_ms"] to show, e.g.,
# "gemini-3.5-flash-lite · 1.8s" on agent stages and the extraction card.
_model_t0: dict[str, float] = {}
# Per-invocation model name captured in `before`, read back in `after` — paired
# by the same f"{label}:{inv}" key as _model_t0 so the UI-facing state key
# always reflects THIS invocation's actual model, never a concurrent one's
# (see _model_timing's apply_dynamic_tiering branch for why this matters).
_model_name: dict[str, str] = {}
# Thought text accumulated from partial streaming chunks keyed by f"extraction:{inv}".
# The extraction after-callback reads from content.parts on the *final* response, but
# with ADK streaming the aggregator may emit thought parts only in partial chunks;
# accumulating here ensures we never miss them.
_thought_accumulator: dict[str, str] = {}

# ── Runtime-selectable models (per agent) ────────────────────────────────────
# The UI can re-point each LLM agent to a different Gemini tier at runtime via
# POST /config/model -> set_agent_model(). CURRENT_MODELS is the single source of
# truth the timing callbacks read, so telemetry always reflects the live model.
# Model names and env-var overrides live in app/models.py.
from app.models import (  # noqa: E402
    ALLOWED_MODELS,
    EXTRACTION_MAX_OUTPUT_TOKENS,
    MODEL_DRAFT_EMAIL,
    MODEL_EXTRACTION,
    MODEL_SCREENING,
)

CURRENT_MODELS: dict[str, str] = {
    "screening": MODEL_SCREENING,
    "extraction": MODEL_EXTRACTION,
    "draft_email": MODEL_DRAFT_EMAIL,
}

from app.config import EXTRACTION_DYNAMIC_MODEL_TIERING  # noqa: E402
from app.models import EXTRACTION_MODEL_TIERS  # noqa: E402

# Bounded backoff for Vertex 429/503. Gemini 3.x uses Dynamic Shared Quota, so a
# brief regional throttle can surface even at low volume; 3 attempts (2 retries)
# with exponential backoff lets a transient blip self-heal without hammering the
# API. Applied to every agent model, including runtime-switched ones.
_RETRY = genai_types.HttpRetryOptions(
    attempts=3, initial_delay=1, max_delay=8, exp_base=2, http_status_codes=[429, 503]
)


def _gemini(name: str) -> Gemini:
    """Wrap a model name in a Gemini model carrying the shared retry policy."""
    return Gemini(model=name, retry_options=_RETRY)


def _model_timing(
    label: str,
    strip_pdf: bool = False,
    drop_model_turns: bool = False,
    inject_pdf: bool = False,
    apply_dynamic_tiering: bool = False,
    mask_pii: bool = False,
):
    """Return (before, after) model callbacks that record real model latency.

    ``mask_pii`` (screening and extraction, the agents that read the email text)
    swaps the PII-masked email text recorded at intake into the request.

    ``callback_context``/``llm_request`` are per-invocation objects ADK hands
    to every call — never shared across concurrent requests. The model name
    written to state is captured from ``llm_request.model`` HERE (paired by
    invocation id via ``_model_name``), not read from the shared
    ``CURRENT_MODELS`` dict, so a concurrent invocation mutating that dict (or
    the agent's own ``.model``) can never leak into this invocation's badge.
    When ``strip_pdf`` is set (screening agent), the invoice PDF is removed
    from the request so the agent never reads the document contents. When
    ``inject_pdf`` is set (extraction agent), the invoice PDF stashed by the
    fetch node is re-injected into the request: extraction runs as a
    single-turn Workflow node (include_contents="none"), so the intake PDF
    turn is otherwise windowed out and the model would hallucinate instead of
    reading the document. When ``apply_dynamic_tiering`` is set (extraction
    agent only), this invocation's own recommended tier/thinking-level
    (written to state by route_extraction_strategy for THIS invocation) is
    applied directly to ``llm_request`` here — never by mutating the shared
    extraction_agent object, which would race with a concurrent invocation's
    own tier (see app/nodes/extraction.py's route_extraction_strategy).
    """
    _IMAGE_MIMES = {"image/jpeg", "image/png", "image/tiff", "image/webp"}
    strip_mimes = ({A2UI_MIME, "application/pdf"} | _IMAGE_MIMES) if strip_pdf else {A2UI_MIME}

    def before(callback_context, llm_request):  # noqa: ANN001
        _strip_parts_by_mime(llm_request, strip_mimes)
        if mask_pii:
            _apply_masked_text(callback_context, llm_request)
        if drop_model_turns:
            _drop_model_turns(llm_request)
        if inject_pdf:
            _inject_invoice_pdf(callback_context, llm_request)
        if apply_dynamic_tiering:
            # Model swap stays gated (unvalidated tier->model mapping); thinking
            # depth does not — it's a reasoning-depth knob on whichever model is
            # already running, not a model swap, so it's safe to always apply.
            tier = callback_context.state.get("extraction_recommended_model")
            if EXTRACTION_DYNAMIC_MODEL_TIERING and tier in EXTRACTION_MODEL_TIERS:
                llm_request.model = EXTRACTION_MODEL_TIERS[tier]
            # Always write a fresh ThinkingConfig rather than mutating the one
            # BuiltInPlanner stamped on the request. BuiltInPlanner assigns its
            # ThinkingConfig object BY REFERENCE, so mutation here would also
            # mutate the shared planner object. More importantly, this guarantees
            # include_thoughts=True reaches the Gemini API regardless of any
            # serialization path that might drop unset-but-assigned Pydantic fields.
            level = callback_context.state.get("extraction_thinking_level") or "MEDIUM"
            if getattr(llm_request, "config", None) is None:
                llm_request.config = genai_types.GenerateContentConfig()
            llm_request.config.thinking_config = genai_types.ThinkingConfig(
                thinking_level=genai_types.ThinkingLevel[level],
                include_thoughts=True,
            )
        inv = getattr(callback_context, "invocation_id", "default")
        _model_t0[f"{label}:{inv}"] = time.perf_counter()
        _model_name[f"{label}:{inv}"] = getattr(llm_request, "model", None) or CURRENT_MODELS.get(label, "")
        return None

    def after(callback_context, llm_response):  # noqa: ANN001
        # Accumulate thought text from every chunk — partial or final.  ADK's
        # StreamingResponseAggregator may emit thought parts only in partial
        # chunks and strip them from the reconstructed final response; by
        # collecting here before the partial guard we never miss them.
        _inv = getattr(callback_context, "invocation_id", "default")
        if label == "extraction":
            for _p in (getattr(getattr(llm_response, "content", None), "parts", None) or []):
                if getattr(_p, "thought", False) and getattr(_p, "text", None):
                    _acc_key = f"extraction:{_inv}"
                    _thought_accumulator[_acc_key] = _thought_accumulator.get(_acc_key, "") + _p.text

        # Partial streaming chunks arrive with usage_metadata=None; processing one
        # consumes the _model_t0 key so the real final response (which carries the
        # full token counts including thoughts_token_count) gets skipped entirely.
        if getattr(llm_response, "partial", False):
            return None
        inv = getattr(callback_context, "invocation_id", "default")
        # PDF cache is consumed by emit_extraction_card (line items phase),
        # not here, so do not pop it from _INVOICE_PDF after extraction.
        t0 = _model_t0.pop(f"{label}:{inv}", None)
        model = _model_name.pop(f"{label}:{inv}", CURRENT_MODELS.get(label, ""))
        if t0 is not None:
            ms = int((time.perf_counter() - t0) * 1000)
            try:
                callback_context.state[f"{label}_latency_ms"] = ms
                callback_context.state[f"{label}_model"] = model
                usage = getattr(llm_response, "usage_metadata", None)
                if usage is not None:
                    callback_context.state[f"{label}_input_tokens"] = getattr(usage, "prompt_token_count", 0) or 0
                    callback_context.state[f"{label}_output_tokens"] = getattr(usage, "candidates_token_count", 0) or 0
                    # Gemini 3.x models always think; thoughts_token_count is billed at
                    # the SAME per-token rate as candidates_token_count but is reported
                    # separately by the API — must be captured explicitly or real spend
                    # silently vanishes from the cost projection (see app/cost_calculator.py).
                    think_tok = getattr(usage, "thoughts_token_count", 0) or 0
                    callback_context.state[f"{label}_thinking_tokens"] = think_tok
                    cache_tok = getattr(usage, "cached_content_token_count", 0) or 0
                    callback_context.state[f"{label}_cache_read_tokens"] = cache_tok
                    if think_tok == 0 and label == "extraction":
                        # Historically observed on gemini-3.1-flash-lite (returned
                        # thoughts_token_count=None with response_schema); log so we can
                        # confirm whether the current model has the same limitation
                        # rather than a bug in our instrumentation.
                        logger.warning(
                            "extraction thinking_tokens=0 (model=%s prompt=%s candidates=%s thoughts=%s)",
                            model,
                            getattr(usage, "prompt_token_count", None),
                            getattr(usage, "candidates_token_count", None),
                            getattr(usage, "thoughts_token_count", None),
                        )
                if label == "extraction":
                    # Drain the streaming accumulator (thought text from partial chunks).
                    _acc_key = f"extraction:{inv}"
                    _accumulated = _thought_accumulator.pop(_acc_key, "")
                    # Also check final response content.parts (non-streaming path or
                    # when ADK aggregator preserves thought parts in the final chunk).
                    _final_parts = getattr(getattr(llm_response, "content", None), "parts", None) or []
                    _final_thought = "\n\n".join(
                        _p.text for _p in _final_parts
                        if getattr(_p, "thought", False) and getattr(_p, "text", None)
                    )
                    _thought_text = (
                        (_accumulated + ("\n\n" + _final_thought if _final_thought else "")).strip()
                        if _accumulated
                        else _final_thought
                    )
                    if _thought_text:
                        callback_context.state["extraction_thought_text"] = _thought_text
                    elif think_tok > 0:
                        logger.warning(
                            "extraction think_tok=%s but no thought text captured "
                            "(accumulated_len=%s, final_parts=%s, final_part_thoughts=%s)",
                            think_tok,
                            len(_accumulated),
                            len(_final_parts),
                            [getattr(_p, "thought", "?") for _p in _final_parts],
                        )
                # Record the stop reason so emit_extraction_card can tell whether the
                # structured output was truncated (large invoice) and only then fall
                # back to the free-form line-items pass.
                finish = getattr(llm_response, "finish_reason", None)
                if finish is None:
                    cands = getattr(llm_response, "candidates", None) or []
                    if cands:
                        finish = getattr(cands[0], "finish_reason", None)
                if finish is not None:
                    callback_context.state[f"{label}_finish_reason"] = str(finish)
            except Exception as exc:  # never let instrumentation break a run
                logger.warning("model timing state write failed (%s): %s", label, exc)
            logger.info("model latency [%s] %s = %d ms", label, model, ms)
        return None

    return before, after


_screening_before, _screening_after = _model_timing("screening", strip_pdf=True, mask_pii=True)
_extraction_before, _extraction_after = _model_timing(
    "extraction", drop_model_turns=True, inject_pdf=True, apply_dynamic_tiering=True, mask_pii=True
)
_draft_before, _draft_after = _model_timing("draft_email")

# ---------------------------------------------------------------------------
# LlmAgent nodes
# ---------------------------------------------------------------------------
# Intake is a deterministic AgentMail FunctionNode (app/nodes.fetch_invoice_email),
# not an LlmAgent. It fetches the invoice email + PDF and attaches the raw PDF
# to the conversation so the extraction model below reads it natively.

# ── screening_agent ──────────────────────────────────────────────────────────
# Screens the incoming email for invoice signals BEFORE extraction. It judges
# ONLY surface signals — the attachment's presence/filename, the email body, and
# the subject line — and never sees the PDF contents (the PDF part is stripped
# from its request via strip_pdf; reading the document is the extraction agent's
# job). Returns a typed InvoiceScreeningAssessment (per-signal evidence only —
# the model does not decide); the screen_email_signal router applies the
# deterministic proceed/discard policy. Non-invoices (newsletters, OOO,
# auto-replies, sent rejection emails that loop back) are discarded there.
# Instruction text lives in app/prompts.py (INVOICE_SCREENING_INSTRUCTION).
screening_agent = LlmAgent(
    name="screening_agent",
    model=_gemini(MODEL_SCREENING),
    instruction=INVOICE_SCREENING_INSTRUCTION,
    output_schema=InvoiceScreeningAssessment,
    output_key="screening_assessment",
    before_model_callback=_screening_before,
    after_model_callback=_screening_after,
)

# ── extraction_agent ─────────────────────────────────────────────────────────
# Reads the invoice PDF natively (multimodal) and returns a typed InvoiceRecord.
# Letting the model parse the document keeps extraction robust to varied vendor
# formats and future data drift. output_schema disables tools (none needed).
# This is a real LLM agent — it always calls the model (never mocked).
# Instruction text lives in app/prompts.py (EXTRACTION_INSTRUCTION).
extraction_agent = LlmAgent(
    name="extraction_agent",
    model=_gemini(MODEL_EXTRACTION),
    instruction=EXTRACTION_INSTRUCTION,
    output_schema=InvoiceRecord,
    output_key="invoice_record",
    generate_content_config=genai_types.GenerateContentConfig(
        max_output_tokens=EXTRACTION_MAX_OUTPUT_TOKENS,
        # PDFs saturate at MEDIUM (560 tok/page + native text): pinned explicitly
        # for predictable per-page cost rather than relying on the provider
        # default, which can change across model versions.
        media_resolution=genai_types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
    ),
    # Gemini 3.x models always think — thinking_budget=0 (the old setting here)
    # is a Gemini-2.5-era knob that cannot disable it and was a no-op on this
    # model family. thinking_level is the real depth control; ADK's documented
    # way to configure it is a planner, not generate_content_config (both write
    # the same field, but the planner is the one route_extraction_strategy
    # retunes per invoice via set_agent_thinking_level — see app/nodes/extraction.py).
    planner=BuiltInPlanner(
        thinking_config=genai_types.ThinkingConfig(
            thinking_level=genai_types.ThinkingLevel.MEDIUM,
            include_thoughts=True,
        )
    ),
    before_model_callback=_extraction_before,
    after_model_callback=_extraction_after,
)

# ── draft_rejection_email ────────────────────────────────────────────────────
# Writes a professional vendor rejection email from the validation flags.
# On revision rounds, incorporates human feedback from state['email_feedback'].
# Instruction text lives in app/prompts.py (DRAFT_EMAIL_INSTRUCTION).
draft_rejection_email = LlmAgent(
    name="draft_rejection_email",
    model=_gemini(MODEL_DRAFT_EMAIL),
    instruction=DRAFT_EMAIL_INSTRUCTION,
    output_schema=RejectionEmail,
    output_key="rejection_email_draft",
    before_model_callback=_draft_before,
    after_model_callback=_draft_after,
)

# ---------------------------------------------------------------------------
# Runtime model switching
# ---------------------------------------------------------------------------
# Maps the UI's agent keys to the live LlmAgent instances. Mutating
# `.model` is safe at runtime: ADK re-resolves canonical_model on every call and
# the FastAPI server shares these module-level instances (no per-request copy),
# so the next pipeline run uses the new model.
_AGENTS_BY_KEY: dict[str, LlmAgent] = {
    "screening": screening_agent,
    "extraction": extraction_agent,
    "draft_email": draft_rejection_email,
}


def set_agent_model(key: str, model: str) -> None:
    """Re-point an agent to a different Gemini tier. Raises ValueError if invalid."""
    if key not in _AGENTS_BY_KEY:
        raise ValueError(f"unknown agent key: {key!r}")
    if model not in ALLOWED_MODELS:
        raise ValueError(f"unsupported model: {model!r}")
    _AGENTS_BY_KEY[key].model = _gemini(model)
    CURRENT_MODELS[key] = model
    logger.info("set_agent_model: %s -> %s", key, model)


# ---------------------------------------------------------------------------
# Graph workflow
# ---------------------------------------------------------------------------

root_agent = Workflow(
    name="invoice_processing_workflow",
    edges=[
        # Entry — go straight to invoice intake
        ("START", fetch_invoice_email),
        # Gate on email presence, then screen for invoice signals before extraction
        (fetch_invoice_email, check_inbox),
        (
            check_inbox,
            {
                "invoice_found": screening_agent,
                "invoice_not_found": inbox_empty,
                "quarantined": quarantine_invoice,
            },
        ),
        (quarantine_invoice, pipeline_complete),
        # Screening: route on invoice vs non-invoice signal
        (screening_agent, screen_email_signal),
        (
            screen_email_signal,
            {
                "invoice": route_extraction_strategy,
                "not_invoice": discard_non_invoice,
                "default": pipeline_complete,
            },
        ),
        (discard_non_invoice, pipeline_complete),
        # Deterministically pick PDF transport (inline/files_api/multipass) and
        # model tier before extraction runs, then render the card and validate.
        (route_extraction_strategy, extraction_agent),
        (extraction_agent, emit_extraction_card),
        # PO lookup happens BEFORE validation so the PO checks and the human
        # approver both see the matched PO. A failed lookup ("error") ends the run
        # instead of validating without the PO.
        (emit_extraction_card, classify_spend),
        (classify_spend, {"classified": validate_invoice, "default": pipeline_complete}),
        # Business rule routing
        (
            validate_invoice,
            {
                "proceed": route_posting,
                "human_review": recall_precedents,
                "duplicate_po": raise_duplicate_alert,
                # Model Armor flagged the extracted text: stop, don't ask for approval.
                "quarantined": quarantine_invoice,
                "default": pipeline_complete,
            },
        ),
        # Advisory Agent Memory recall, then the approval gate.
        (recall_precedents, human_approval_gate),
        # HITL approval routing
        (
            human_approval_gate,
            {
                "approve": route_posting,
                "reject": prepare_rejection_brief,
                "default": pipeline_complete,
            },
        ),
        # Rejection email draft → human review loop
        (prepare_rejection_brief, draft_rejection_email),
        (draft_rejection_email, review_email_draft),
        (
            review_email_draft,
            {
                "send": send_rejection_email,
                "revise": prepare_rejection_brief,
                "default": pipeline_complete,
            },
        ),
        # Both direct and indirect spend go to ERP
        (
            route_posting,
            {
                "erp": post_to_erp,
                "default": pipeline_complete,
            },
        ),
        # All terminal paths converge at pipeline_complete
        (post_to_erp, pipeline_complete),
        (raise_duplicate_alert, pipeline_complete),
        (send_rejection_email, pipeline_complete),
    ],
)

# ---------------------------------------------------------------------------
# App  (name must match the agent directory - "app")
# ---------------------------------------------------------------------------

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
