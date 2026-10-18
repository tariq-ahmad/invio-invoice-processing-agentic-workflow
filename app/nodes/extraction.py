"""Extraction card rendering + line-item fallback for the extracted invoice."""

import io
import logging
import re
import time
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.workflow import node
from google.genai import types as genai_types
from pypdf import PdfReader

from app import model_armor
from app.a2ui_views import a2ui_enabled, build_extraction_surface
from app.app_utils.tracing import enrich_node_span
from app.config import (
    DOMINANT_IMAGE_ASPECT_TOLERANCE,
    DOMINANT_IMAGE_MIN_PX,
    EXTRACTION_DYNAMIC_MODEL_TIERING,
    FILES_API_SIZE_THRESHOLD_BYTES,
    LINE_ITEM_CONTINUATION_ANCHOR_K,
    MAX_LINE_ITEM_CALLS,
    MULTIPASS_PAGE_THRESHOLD,
    SMALL_INVOICE_PAGE_THRESHOLD,
    SMALL_INVOICE_SIZE_THRESHOLD_BYTES,
)
from app.cost_calculator import compute_cost
from app.models import (
    EXTRACTION_MAX_OUTPUT_TOKENS,
    EXTRACTION_THINKING_TIERS,
    MODEL_EXTRACTION,
)
from app.nodes._helpers import (
    _content_msg,
    _content_with_a2ui,
    _lenient_json_objects,
    _safe_float,
)
from app.nodes._state import _INVOICE_PDF
from app.policy import get_policy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 4a. route_extraction_strategy  (deterministic size/complexity router)
#     Sits between screen_email_signal and extraction_agent. Decides HOW the
#     PDF is sent (inline / files_api / multipass) and WHICH Gemini tier reads
#     it (lite / flash / pro) — a 1-pager to a 100+ page bundle, no hardcoded
#     vendor template, no LLM round-trip: every signal here is computable
#     before the model ever runs.
# ---------------------------------------------------------------------------


def _page_has_dominant_image(page) -> bool:  # noqa: ANN001
    """True when the page's content is essentially one full-page photograph.

    A phone-scanning app (Adobe Scan, Google Drive scan, iOS Notes, CamScanner,
    etc.) embeds a real, searchable OCR text layer UNDER the photographed page
    specifically so the PDF is searchable/copyable — extract_text() finding
    that layer must not be mistaken for "clean born-digital document." Checked
    via the raw /XObject resource dict (Width/Height/Subtype) rather than
    decoding pixel data, so it's cheap even across every sampled page. A small
    logo/letterhead image is excluded by the minimum-size gate; an image whose
    aspect ratio doesn't resemble the page's is excluded too (unlikely to be a
    full-page scan of that same page).
    """
    resources = page.get("/Resources")
    xobjects = resources.get("/XObject") if resources else None
    if not xobjects:
        return False
    mediabox = page.mediabox
    page_w, page_h = float(mediabox.width), float(mediabox.height)
    if page_w <= 0 or page_h <= 0:
        return False
    page_ratio = page_w / page_h
    for xobj in xobjects.values():
        obj = xobj.get_object()
        if obj.get("/Subtype") != "/Image":
            continue
        width, height = obj.get("/Width"), obj.get("/Height")
        if not width or not height or min(width, height) < DOMINANT_IMAGE_MIN_PX:
            continue
        image_ratio = width / height
        if abs(image_ratio - page_ratio) / page_ratio <= DOMINANT_IMAGE_ASPECT_TOLERANCE:
            return True
    return False


def _pdf_page_count_and_native_text(att_bytes: bytes, att_mime: str) -> tuple[int, bool]:
    """Return (page_count, has_native_text) for a stashed attachment.

    Non-PDF attachments (a scanned invoice emailed as a JPEG/PNG) are treated
    as a single image page with no native text layer. A PDF that fails to
    parse fails safe the same way — both routes to the strongest tier rather
    than the cheapest, since we can't confirm it's a clean, easy document.
    """
    if att_mime != "application/pdf":
        return 1, False
    try:
        reader = PdfReader(io.BytesIO(att_bytes))
        pages = reader.pages
        page_count = len(pages)
        # Native-text vs. scanned/image is stable across a document, so
        # sampling the first few pages is enough — no need to walk all of them.
        sample = pages[: min(page_count, 3)]
        has_native_text = any(len((pg.extract_text() or "").strip()) > 20 for pg in sample)
        # A page with no text at all already correctly falls through to the
        # strongest tier below, so only spend this check on the ambiguous case:
        # text WAS found, but it may be an OCR layer under a photographed page.
        if has_native_text and any(_page_has_dominant_image(pg) for pg in sample):
            has_native_text = False
        return page_count, has_native_text
    except Exception as exc:
        logger.warning("route_extraction_strategy: could not read PDF (%s)", exc)
        return 1, False


def decide_extraction_strategy(byte_size: int, page_count: int) -> str:
    """How to send the PDF to Gemini: inline / files_api / multipass.

    multipass takes priority over files_api: a very long document needs a
    dedicated line-item pass regardless of whether its bytes also cross the
    inline-transport threshold.
    """
    if page_count > MULTIPASS_PAGE_THRESHOLD:
        return "multipass"
    if byte_size >= FILES_API_SIZE_THRESHOLD_BYTES:
        return "files_api"
    return "inline"


def decide_extraction_tier(
    byte_size: int, page_count: int, has_native_text: bool, strategy: str
) -> str:
    """Which Gemini tier extracts this invoice: lite / flash / pro.

    Driven by COMPLEXITY, not size alone — a small blurry/scanned single-pager
    must escalate to the strongest tier, not drop to the cheapest, because page
    count and byte size are weak proxies for how hard a document is to read.
    """
    if strategy == "multipass" or not has_native_text:
        return "pro"
    if page_count <= SMALL_INVOICE_PAGE_THRESHOLD and byte_size <= SMALL_INVOICE_SIZE_THRESHOLD_BYTES:
        return "lite"
    return "flash"


def decide_extraction_thinking_level(tier: str, strategy: str) -> str:
    """Thinking depth for the MAIN extraction_agent call — usually tier-driven,
    with one override: a "multipass" document already gets its own dedicated,
    unconstrained line-items pass (_extract_line_items_direct), so the main
    call only needs to nail the header fields, not reason deeply about every
    line item too. HIGH thinking on an 80+-page document can otherwise consume
    enough of the (finite, shared) output-token budget to truncate the actual
    JSON before it finishes — dropping to MEDIUM frees that headroom back for
    completeness on exactly the documents most likely to need it.
    """
    if strategy == "multipass":
        return "MEDIUM"
    return EXTRACTION_THINKING_TIERS[tier]


@node(name="route_extraction_strategy")
def route_extraction_strategy(ctx: Context, node_input: Any) -> Event:
    """Pick the PDF-transport strategy, model tier, and thinking depth before
    extraction_agent runs.

    Reads the PDF already stashed by fetch_invoice_email (peeked, not popped —
    the extraction before_model_callback and emit_extraction_card still need
    it). A pure state-writer: it only RECORDS the recommended strategy/tier/
    thinking-level for this invocation. extraction_agent's before_model_callback
    (app/agent.py's _extraction_before) reads these back from this SAME
    invocation's state and applies them directly to that call's llm_request —
    this node must never mutate the shared extraction_agent object itself,
    since that would race with a concurrent invocation's own tier decision
    (two invoices processed close together both mutate the same singleton).
    EXTRACTION_DYNAMIC_MODEL_TIERING gates whether the MODEL override is
    actually applied; the thinking-level override always applies.
    """
    stashed = _INVOICE_PDF.get(getattr(ctx, "invocation_id", None))
    if not stashed:
        return Event(
            state={  # ty: ignore[unknown-argument]
                "extraction_strategy": "inline",
                "extraction_recommended_model": "flash",
                "extraction_thinking_level": EXTRACTION_THINKING_TIERS["flash"],
                "extraction_dynamic_tiering_enabled": EXTRACTION_DYNAMIC_MODEL_TIERING,
            }
        )

    att_bytes, att_mime = stashed
    byte_size = len(att_bytes)
    page_count, has_native_text = _pdf_page_count_and_native_text(att_bytes, att_mime)

    strategy = decide_extraction_strategy(byte_size, page_count)
    tier = decide_extraction_tier(byte_size, page_count, has_native_text, strategy)
    thinking_level = decide_extraction_thinking_level(tier, strategy)

    logger.info(
        "route_extraction_strategy: pages=%d bytes=%d native_text=%s -> strategy=%s tier=%s thinking=%s",
        page_count, byte_size, has_native_text, strategy, tier, thinking_level,
    )

    return Event(
        state={  # ty: ignore[unknown-argument]
            "extraction_strategy": strategy,
            "extraction_recommended_model": tier,
            "extraction_page_count": page_count,
            "extraction_byte_size": byte_size,
            "extraction_has_native_text": has_native_text,
            "extraction_thinking_level": thinking_level,
            "extraction_dynamic_tiering_enabled": EXTRACTION_DYNAMIC_MODEL_TIERING,
        }
    )


def _base_line_items_prompt(scope: str) -> str:
    """The free-form line-items prompt (shared by the first call and, as the
    format/rules base, by every continuation call)."""
    return (
        "Extract the billable line items from this invoice.\n"
        + scope +
        "Return ONLY a raw JSON array — no markdown, no explanation.\n"
        "Each element is an object:\n"
        '  {"name": <display name>, "amount": <cost>, '
        '"attributes": [{"label": <verbatim label>, "value": <value>}, ...], '
        '"source_grounding": <"grounded"|"inferred"|"not_found">}\n'
        "\n"
        "  name   = the best short human-readable name for the line "
        "(the product/service name, or its circuit/item identifier).\n"
        "  amount = that line's cost / total, as printed.\n"
        "  attributes = any additional distinct data points THIS line "
        "prints beyond name/amount — decide per line, per invoice, what's "
        "actually there; do not assume a fixed set. Examples of the KIND "
        "of thing to look for: a circuit/asset ID, a site or location, a "
        "billing period, a unit rate, a serial number — but only capture "
        "what this specific document actually shows for this specific "
        "line. Use the label VERBATIM as printed (never your own "
        "paraphrase — the same invoice must produce the same label every "
        "time it's read), and never copy an invoice-level value (like the "
        "overall billing period) onto every line as if it were per-line "
        "data. OMIT the attributes key entirely when the line has nothing "
        "beyond name/amount.\n"
        "  source_grounding = how this line item was obtained: "
        "\"grounded\" = read directly, printed clearly and unambiguous; "
        "\"inferred\" = had to be interpreted (obscured, ambiguous, "
        "low-quality scan); judge each line in isolation — never let one "
        "line's grounding affect another's.\n"
        "\n"
        "ONLY genuine billable product/service line items count. Do "
        "NOT include summary or non-line rows, such as: payment or "
        "adjustment entries (e.g. a \"Details of Payment and "
        "Adjustments\" section, \"Payment Adjustment\", "
        "\"Payment-Thank You\"); charge-summary rollups (e.g. "
        "\"Current Charges Summary\", \"Summary of Account\", "
        "period \"Monthly Charges\" summaries, \"From <date> To "
        "<date>\" summary lines); subtotals; taxes; past-due or "
        "balance-forward; or grand totals. Those are summary "
        "sections, not line items — exclude them.\n"
        "\n"
        "Example — a telecom service block with per-line attributes:\n"
        '  {"name": "Fiber Leased Line - 500 Mbps", "amount": "$181.76", '
        '"attributes": [{"label": "Circuit ID", "value": "IS-10033"}, '
        '{"label": "Site", "value": "Raleigh, NC"}, '
        '{"label": "Billing Period", "value": "06/01/2026 - 06/30/2026"}], '
        '"source_grounding": "grounded"}\n'
        "Example — a standard table row with nothing beyond name/amount:\n"
        '  {"name": "AI Platform Integration Services", "amount": '
        '"$4,500.00", "source_grounding": "grounded"}\n'
        "\n"
        "Cover ALL genuine line items / service blocks from ALL "
        "pages. Each distinct product or service is its own item — "
        "never collapse them into a single summary row.\n"
        "This document may run many pages with hundreds of near-identical "
        "rows (e.g. one row per site/circuit). Do NOT conclude the list is "
        "finished just because recent rows look repetitive — verify you have "
        "covered every page before stopping.\n"
        "Return [] if no line items are present."
    )


def _norm_li_key(item: dict) -> str:
    """Normalized dedup key for a line item — name + amount + first attribute.

    Used ONLY for boundary dedup between a truncated call and its continuation
    (never for global dedup, which would wrongly collapse legitimately identical
    rows like two separate "Late Fee — $5.00" lines). The first attribute
    (e.g. a circuit/asset ID) disambiguates near-identical names.
    """
    name = str(item.get("name") or "").strip().lower()
    amount = re.sub(r"[^0-9.\-]", "", str(item.get("amount") or ""))
    attrs = item.get("attributes") or []
    first_attr = ""
    if isinstance(attrs, list) and attrs and isinstance(attrs[0], dict):
        first_attr = (
            f"{attrs[0].get('label', '')}={attrs[0].get('value', '')}".strip().lower()
        )
    return f"{name}|{amount}|{first_attr}"


def _continuation_suffix(tail: list[dict], captured: int) -> str:
    """Instruct the model to resume after the last `tail` items already captured.

    Anchors on the verbatim tail (not document row numbers — not all invoices
    number rows — and not a bare count the model can't self-verify)."""
    lines = []
    for it in tail:
        name = str(it.get("name") or "").strip()
        amt = str(it.get("amount") or "").strip()
        attrs = it.get("attributes") or []
        extra = ""
        if isinstance(attrs, list) and attrs and isinstance(attrs[0], dict):
            fl = str(attrs[0].get("label") or "").strip()
            fv = str(attrs[0].get("value") or "").strip()
            if fl or fv:
                extra = f" [{fl}: {fv}]" if fl else f" [{fv}]"
        lines.append(f"  - {name} — {amt}{extra}")
    tail_block = "\n".join(lines)
    return (
        "\n\n"
        "IMPORTANT — THIS IS A CONTINUATION. You have already extracted the "
        f"first {captured} line items of THIS invoice. The most recent ones you "
        "returned, in order, were:\n"
        f"{tail_block}\n"
        "Continue from the line item that comes IMMEDIATELY AFTER the last one "
        "listed above. Do NOT repeat any item at or before it, and do NOT "
        "restart from the beginning of the invoice. Return ONLY the remaining "
        "line items, in the same JSON array format described above. Return [] "
        "if there are no further line items."
    )


def _strip_overlap(all_items: list[dict], batch: list[dict], anchor_keys: set[str]) -> list[dict]:
    """Remove any leading run of a continuation batch that overlaps what we have.

    Two guards, both leading-run only (never touch the interior, so legitimate
    duplicates elsewhere survive): (1) drop leading items whose key is in the
    anchor set we told the model to resume after; (2) if the model ignored that
    and replayed from the top, drop the leading run that matches the head of
    ``all_items`` in order.
    """
    i = 0
    while i < len(batch) and _norm_li_key(batch[i]) in anchor_keys:
        i += 1
    batch = batch[i:]
    j = 0
    while j < len(batch) and j < len(all_items) and _norm_li_key(batch[j]) == _norm_li_key(all_items[j]):
        j += 1
    return batch[j:]


def _coerce_line_items(items: list) -> list[dict]:
    """Fail-safe coercion for the free-form (no output_schema) line-items call:
    drop non-item rows, force an invalid source_grounding to not_found, and force
    a malformed attributes shape to [] rather than propagating garbage."""
    valid_grounding = {"grounded", "inferred", "not_found"}
    good: list[dict] = []
    for i in items:
        if not (isinstance(i, dict) and (i.get("name") or i.get("amount"))):
            continue
        if i.get("source_grounding") not in valid_grounding:
            i["source_grounding"] = "not_found"
        attrs = i.get("attributes")
        if not isinstance(attrs, list) or not all(
            isinstance(a, dict) and "label" in a and "value" in a for a in attrs
        ):
            i["attributes"] = []
        good.append(i)
    return good


def _line_items_single_call(
    att_bytes: bytes, att_mime: str, prompt_text: str, model: str = MODEL_EXTRACTION
) -> tuple[list[dict], int, int, int, int, bool]:
    """One free-form Gemini line-items call. Returns
    (items, in_tok, out_tok, think_tok, truncated). ``truncated`` is True when the
    model stopped on MAX_TOKENS or the JSON needed lenient (partial) recovery —
    the signal the orchestrator uses to fire a continuation call. Raises on a
    hard API error (the orchestrator catches it). ``model`` defaults to the
    static flash tier but the caller passes this invocation's own dynamically
    resolved tier (see emit_extraction_card) so this call bills at the SAME
    rate the main extraction_agent call used, instead of always flash."""
    import json
    import os

    import google.genai as genai

    # GOOGLE_CLOUD_PROJECT is always set by app/agent.py's ADC bootstrap (or by
    # Cloud Run) before this runs — no hardcoded project fallback needed here.
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-west1")
    client = genai.Client(vertexai=True, project=project, location=location)
    response = client.models.generate_content(
        model=model,
        config=genai_types.GenerateContentConfig(
            max_output_tokens=65535,
            # This call also does free-form per-item attribute/label
            # recognition (deciding, with no fixed schema, which fields are
            # actually present on each line) — real reasoning, not pure
            # transcription. This is a standalone genai.Client call (no
            # LlmAgent/planner involved), so thinking_config on
            # generate_content_config is the only mechanism here.
            # thinking_budget is a Gemini-2.5-era knob that can't disable
            # thinking on Gemini 3.x; thinking_level=LOW is a deliberate
            # cost/quality tradeoff — the smallest step that still budgets
            # real reasoning, chosen over MEDIUM/HIGH to bound cost/latency
            # across up to MAX_LINE_ITEM_CALLS continuation calls on huge
            # invoices.
            thinking_config=genai_types.ThinkingConfig(
                thinking_level=genai_types.ThinkingLevel.LOW
            ),
        ),
        contents=[
            genai_types.Content(
                role="user",
                parts=[
                    genai_types.Part.from_bytes(data=att_bytes, mime_type=att_mime),
                    genai_types.Part.from_text(text=prompt_text),
                ],
            )
        ],
    )
    usage = getattr(response, "usage_metadata", None)
    in_tok = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
    out_tok = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
    think_tok = int(getattr(usage, "thoughts_token_count", 0) or 0) if usage else 0
    cache_tok = int(getattr(usage, "cached_content_token_count", 0) or 0) if usage else 0

    truncated = False
    cands = getattr(response, "candidates", None) or []
    if cands:
        fr = getattr(cands[0], "finish_reason", None)
        if fr is not None and "MAX_TOKENS" in str(fr):
            truncated = True

    # response.text returns None when the model stops on MAX_TOKENS, so read
    # the (possibly partial) text straight from the candidate parts instead.
    text = ""
    try:
        text = response.text or ""
    except Exception:
        text = ""
    if not text:
        chunks: list[str] = []
        for cand in cands:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", None) or []:
                t = getattr(part, "text", None)
                if t:
                    chunks.append(t)
        text = "".join(chunks)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 1)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        # Truncated array (large invoices can exceed the output-token limit):
        # recover every complete object, discard only the cut-off tail. This IS
        # a truncation signal even if finish_reason didn't say so.
        items = _lenient_json_objects(text)
        truncated = True
        logger.warning(
            "line_items JSON truncated (out_tok=%d); recovered %d items", out_tok, len(items)
        )
    good = _coerce_line_items(items) if isinstance(items, list) else []
    return good, in_tok, out_tok, think_tok, cache_tok, truncated


def _reconciles_with_total(items: list[dict], expected_total_val: float | None) -> bool:
    """True when the items' amounts sum within the policy's
    reconcile_tolerance_pct of expected_total_val, OR there's no total to check
    against (nothing to contradict, so treated as satisfied — preserves prior
    no-total behavior).
    """
    if expected_total_val is None:
        return True
    line_sum = sum(
        amt for li in items if (amt := _safe_float(li.get("amount"))) is not None
    )
    tolerance = max(abs(expected_total_val) * get_policy().reconcile_tolerance_pct, 0.01)
    return abs(line_sum - expected_total_val) <= tolerance


def _extract_line_items_direct(
    att_bytes: bytes,
    att_mime: str,
    vendor: str = "",
    invoice_no: str = "",
    expected_total: str = "",
    model: str = MODEL_EXTRACTION,
) -> tuple[list[dict], int, int, int, int, bool]:
    """Extract ALL line items via one or more free-form Gemini calls.

    Returns (items, input_tokens, output_tokens, thinking_tokens, cache_read_tokens, incomplete).
    Tokens are summed across every call so the caller folds total usage into the
    extraction step's cost accounting. ``incomplete`` is True when extraction was
    still truncated after MAX_LINE_ITEM_CALLS (or a continuation stopped making
    progress while still truncated) — the caller surfaces this explicitly
    rather than silently under-reporting.

    Separated from the main extraction_agent because constrained structured
    output hits the model's output-token ceiling on large multi-page invoices.
    A single free-form call also hits that 65,536-token ceiling once line items
    carry rich per-line attributes (~450 items/call), so this loops: each
    continuation resumes after the last captured item until the model returns a
    non-truncated response THAT RECONCILES with ``expected_total`` (a long,
    highly repetitive enumeration can make the model stop early without
    hitting the token ceiling — a natural but premature stop; the API gives no
    truncation signal for that case, so reconciliation is the only way to
    detect it), adds nothing new on retry, or the call budget is exhausted.

    ``vendor`` / ``invoice_no`` are the header fields the main extraction already
    committed to; when the PDF bundles several invoices they scope line items to
    that same invoice — no assumption about who the buyer or seller is.
    ``expected_total`` is the invoice's own header total (also already
    committed to by the main pass); when absent/unparseable, the
    reconciliation-driven retry is simply skipped (falls back to trusting a
    non-truncated stop, the prior behavior).
    ``model`` is this invocation's dynamically resolved tier (same one the
    main extraction_agent call used), passed through by emit_extraction_card
    so this pass bills at that same rate instead of always flash.
    """
    scope = ""
    ident = " / ".join(x for x in (vendor.strip(), invoice_no.strip()) if x)
    if ident:
        scope = (
            f"This PDF may bundle several separate invoices. Extract line items "
            f"belonging ONLY to the invoice identified by [{ident}]. If the file "
            f"contains just one invoice, extract all of its line items regardless "
            f"of who it is billed to.\n"
        )
    base_prompt = _base_line_items_prompt(scope)
    expected_total_val = _safe_float(expected_total)

    all_items: list[dict] = []
    tot_in = tot_out = tot_think = tot_cache = 0
    incomplete = False
    for call_idx in range(MAX_LINE_ITEM_CALLS):
        if call_idx == 0:
            prompt, tail = base_prompt, []
        else:
            tail = all_items[-LINE_ITEM_CONTINUATION_ANCHOR_K:]
            prompt = base_prompt + _continuation_suffix(tail, len(all_items))
        try:
            items, in_tok, out_tok, think_tok, cache_tok, truncated = _line_items_single_call(
                att_bytes, att_mime, prompt, model=model
            )
        except Exception as exc:
            logger.warning("line_items call %d failed: %s", call_idx, exc)
            # A mid-continuation failure leaves partial data with more expected —
            # mark incomplete. A first-call failure yields an empty list (same as
            # the pre-loop behavior); no line items to reconcile against.
            incomplete = call_idx > 0
            break
        tot_in += in_tok
        tot_out += out_tok
        tot_think += think_tok
        tot_cache += cache_tok
        if call_idx == 0:
            new_items = items
        else:
            new_items = _strip_overlap(all_items, items, {_norm_li_key(x) for x in tail})
        all_items.extend(new_items)

        if not truncated:
            if _reconciles_with_total(all_items, expected_total_val):
                break  # captured everything and the numbers add up
            if call_idx > 0 and not new_items:
                # A natural (non-truncated) stop that still doesn't reconcile,
                # and asking again added nothing new: the model insists it's
                # done. Accept what we have rather than loop uselessly.
                # incomplete stays False — this was a good-faith stop, not a
                # cut-off one; reconcile_totals downstream (same tolerance)
                # will raise its own accurate flag if the numbers genuinely
                # don't add up, which we should never overclaim as "incomplete".
                logger.warning(
                    "line_items stopped without reconciling (call %d, %d items, "
                    "no new items on retry) — deferring to reconcile_totals",
                    call_idx, len(all_items),
                )
                break
            # Naturally stopped short of the known total — the model may have
            # concluded early on a long repetitive list. Ask it to keep going
            # rather than trust a stop the API itself never flagged as cut off.
            continue
        if call_idx > 0 and not new_items:
            # still truncated but the continuation added nothing new → stuck;
            # stop rather than loop to the cap with no progress.
            incomplete = True
            break
    else:
        # Exhausted MAX_LINE_ITEM_CALLS with the last call still truncated (or
        # still short of the expected total).
        incomplete = True

    if incomplete:
        logger.warning(
            "line_items incomplete after %d call(s): captured %d items",
            MAX_LINE_ITEM_CALLS, len(all_items),
        )
    return all_items, tot_in, tot_out, tot_think, tot_cache, incomplete


# ---------------------------------------------------------------------------
# 4b. emit_extraction_card  (renders the extracted invoice as an A2UI surface)
#     Sits between extraction_agent and validate_invoice. Forwards the
#     InvoiceRecord dict downstream unchanged so validate_invoice still parses it.
# ---------------------------------------------------------------------------


_GROUNDING_LABEL = {"grounded": "Grounded", "inferred": "Inferred", "not_found": "Not found"}


def _fv_str(record: dict, key: str) -> tuple[str, str]:
    """Pull (value, source_grounding) from a FieldValue-shaped dict key."""
    fv = record.get(key) or {}
    value = fv.get("value")
    return (
        str(value) if value not in (None, "") else "—",
        str(fv.get("source_grounding", "not_found")),
    )


def _format_extraction_summary(record: dict) -> str:
    """Markdown summary of the extracted invoice (shown in the ADK playground;
    suppressed in the React UI where the extraction card renders instead)."""
    vendor, g_vendor = _fv_str(record, "vendor")
    inv_no, g_inv = _fv_str(record, "invoice_no")
    po_ref, g_po = _fv_str(record, "po_ref")
    total, g_total = _fv_str(record, "total")
    currency, _ = _fv_str(record, "currency")
    amount = f"{currency} {total}".strip() if total != "—" and currency != "—" else total

    rows = [
        ("Vendor", vendor, g_vendor),
        ("Invoice #", inv_no, g_inv),
        ("PO Reference", po_ref, g_po),
        ("Amount", amount, g_total),
    ]
    table = "\n".join(
        f"| {label} | {value} | {_GROUNDING_LABEL.get(grounding, grounding)} |"
        for label, value, grounding in rows
    )

    line_items = record.get("line_items") or []
    li_block = ""
    if line_items:
        li_lines = []
        for li in line_items:
            name = li.get("name") or "—"
            amt = li.get("amount") or ""
            g = _GROUNDING_LABEL.get(li.get("source_grounding", "not_found"), "Not found")
            li_lines.append(f"- {name}" + (f" — {amt}" if amt else "") + f" ({g})")
        li_block = "\n\n**Line items**\n\n" + "\n".join(li_lines)

    return (
        "## Invoice Extracted\n\n"
        "The invoice PDF has been read and structured data extracted. Each "
        "field's source-grounding evidence is shown — how the value was "
        "obtained, not a self-graded confidence score.\n\n"
        "| Field | Value | Source Grounding |\n"
        "|---|---|---|\n"
        f"{table}"
        f"{li_block}"
    )


@node(name="emit_extraction_card")
def emit_extraction_card(ctx: Context, node_input: Any):  # type: ignore[return]
    """Emit the extracted invoice as an A2UI card and forward the record.

    Reads the InvoiceRecord dict from ``node_input`` (the extraction_agent's
    structured output) with a fallback to ``state['invoice_record']``. Emits an
    A2UI extraction surface (when enabled) and forwards the same dict as the
    node output so the downstream ``validate_invoice`` receives it as
    ``node_input`` exactly as before.
    """
    record = node_input if isinstance(node_input, dict) else (ctx.state.get("invoice_record") or {})

    # --- Line items ---------------------------------------------------------
    # The main extraction pass already returns line_items in its structured
    # output, so a normal invoice needs NO second model call here. The free-form
    # pass fires as a FALLBACK for any invoice whose structured output truncated
    # (which would curtail the line items), and UNCONDITIONALLY for the
    # "multipass" strategy (route_extraction_strategy) — a very-long invoice's
    # line items are dedicated to their own pass rather than left to a single
    # constrained call. We always pop the stashed PDF to avoid a _INVOICE_PDF
    # leak, whether or not we use it.
    li_in = li_out = li_think = li_cache = li_ms = 0
    li_incomplete = False
    ran_line_items = False
    # Deliberately NOT tied to the main call's dynamically-selected tier: this
    # is a throughput/coverage-bound task (enumerate every line item) under a
    # hard shared thinking+output token ceiling (max_output_tokens=65535,
    # _line_items_single_call). A larger/more deliberative model consumes more
    # of that shared budget on thinking alone, leaving less room for actual
    # item JSON before hitting MAX_TOKENS -- on a multipass invoice (tier
    # always "pro") with rich per-line attributes, this cut items-per-call
    # from ~450 (flash) to single digits, causing a silent under-capture
    # regression (33/500+ items). Pricing consistency with the main call's
    # tier is a real but lower-priority goal than not losing invoice data;
    # flash is the empirically reliable choice for this specific call.
    li_model = MODEL_EXTRACTION
    stashed = _INVOICE_PDF.pop(getattr(ctx, "invocation_id", None), None)
    if stashed and isinstance(record, dict):
        finish = str(ctx.state.get("extraction_finish_reason") or "").upper()
        out_tok = int(ctx.state.get("extraction_output_tokens", 0) or 0)
        # Near-cap or a MAX_TOKENS stop reason means the structured output was
        # truncated. Compares against the SAME ceiling extraction_agent is
        # actually configured with (app/models.py), not a copy-pasted literal.
        truncated = "MAX_TOKENS" in finish or out_tok >= EXTRACTION_MAX_OUTPUT_TOKENS - 200
        multipass = ctx.state.get("extraction_strategy") == "multipass"
        vendor = (record.get("vendor") or {}).get("value") or ""
        total = (record.get("total") or {}).get("value") or ""
        header_corrupt = not vendor and not total
        if truncated or header_corrupt or multipass:
            ran_line_items = True
            att_bytes, att_mime = stashed
            invoice_no = (record.get("invoice_no") or {}).get("value") or ""
            li_t0 = time.perf_counter()
            record["line_items"], li_in, li_out, li_think, li_cache, li_incomplete = _extract_line_items_direct(
                att_bytes, att_mime, vendor=vendor, invoice_no=invoice_no, expected_total=total, model=li_model
            )
            li_ms = int((time.perf_counter() - li_t0) * 1000)
            logger.info(
                "line_items fallback fired (finish=%s out_tok=%d header_corrupt=%s multipass=%s) -> %d items (incomplete=%s, ms=%d)",
                finish or "?", out_tok, header_corrupt, multipass,
                len(record.get("line_items") or []), li_incomplete, li_ms,
            )

    # Completeness flag travels on the record (so it survives InvoiceRecord(**raw)
    # in validate_invoice) and in state (for the UI). Only the free-form line-items
    # pass can truncate mid-list; when it didn't run, the main structured pass
    # wasn't truncated, so completeness is True. Set explicitly in BOTH cases so a
    # spurious model-emitted value (the field is in extraction_agent's output
    # schema) never leaks through — this flag is pipeline-controlled, not model-set.
    if isinstance(record, dict):
        record["line_items_complete"] = (not li_incomplete) if ran_line_items else True

    summary = _format_extraction_summary(record)
    messages = build_extraction_surface(record) if a2ui_enabled() else None
    # Write the record back to state (line_items may have been backfilled above)
    # so the native extraction card and downstream validation read the same value.
    state = {"invoice_record": record}
    # Model Armor: the free text the model copied out of the document (vendor,
    # identifiers, line-item names and attributes) goes on into later prompts,
    # cards and memory, so screen it here. The verdict is only recorded; the
    # decision is validate_invoice's, which quarantines the invoice on a block.
    if model_armor.is_enabled("extraction") and isinstance(record, dict):
        armor = model_armor.scan(
            "extraction",
            text=model_armor.flatten_record_text(record),
            labels=["Text extracted from the invoice"],
        )
        state["armor_extraction"] = armor.to_state()
        with enrich_node_span("emit_extraction_card") as span:
            span.set("armor.matched", armor.matched)
            span.set("armor.blocked", armor.blocked)
            span.set("armor.filters", ",".join(armor.filters))
            span.set("armor.latency_ms", armor.latency_ms or 0)
    if ran_line_items:
        state["line_items_incomplete"] = li_incomplete
    # Fold the line-items call's tokens AND wall time into the extraction step's
    # totals so the cost card and latency badge reflect BOTH Gemini calls — the
    # extraction_agent model callback only times itself (app/agent.py), so
    # without this the badge silently omits the (often much longer) line-items
    # pass entirely.
    # Real spend, priced per call's ACTUAL model: the main extraction_agent
    # call and the line-items pass can each run on a different model — folding
    # both token counts together and pricing the total at one model's rate (as
    # the frontend used to do) misprices whichever call didn't use that rate.
    # Computed here, server-side, where both models are actually known.
    # (li_model is always MODEL_EXTRACTION -- see the comment where it's set
    # above -- so this is currently always a flash-rate cost for that portion,
    # but stays generic in case that ever changes.)
    main_model = ctx.state.get("extraction_model") or MODEL_EXTRACTION
    main_in = int(ctx.state.get("extraction_input_tokens", 0) or 0)
    main_out = int(ctx.state.get("extraction_output_tokens", 0) or 0)
    main_think = int(ctx.state.get("extraction_thinking_tokens", 0) or 0)
    main_cache = int(ctx.state.get("extraction_cache_read_tokens", 0) or 0)
    cost = compute_cost(main_model, main_in, main_out, main_think, main_cache)
    if li_in or li_out or li_think:
        cost += compute_cost(li_model, li_in, li_out, li_think, li_cache)
    state["extraction_cost_usd"] = cost
    state["extraction_cache_read_tokens"] = main_cache + li_cache
    if li_in or li_out or li_think:
        state["extraction_input_tokens"] = main_in + li_in
        state["extraction_output_tokens"] = main_out + li_out
        state["extraction_thinking_tokens"] = main_think + li_think
    if li_ms:
        state["extraction_latency_ms"] = int(ctx.state.get("extraction_latency_ms", 0) or 0) + li_ms
    if messages:
        yield Event(
            content=_content_with_a2ui(summary, messages),
            output=record,  # ty: ignore[unknown-argument]
            state=state,  # ty: ignore[unknown-argument]
        )
    else:
        yield Event(
            content=_content_msg(summary),
            output=record,  # ty: ignore[unknown-argument]
            state=state,  # ty: ignore[unknown-argument]
        )
