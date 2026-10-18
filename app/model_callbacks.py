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

"""Pure model-request mutators used by the extraction/screening before-callbacks.

Extracted verbatim from ``app/agent.py``. Each mutates an ``llm_request`` in
place; the timing/telemetry wiring that composes them stays in ``app/agent.py``.
"""

import logging

from google.genai import types as genai_types

logger = logging.getLogger(__name__)


def _strip_parts_by_mime(llm_request, mimes: set[str]) -> None:
    """Drop inline-data parts whose MIME is in ``mimes`` from a model request.

    Always used to remove A2UI surfaces (application/a2ui+json): those are UI-only
    and Vertex rejects the unknown MIME with 400 INVALID_ARGUMENT when a prior
    turn replays into a later agent's request (they stay in session history for
    the client). The screening agent additionally drops the invoice PDF
    (application/pdf) so it judges only surface signals — filename, body, subject
    — never the PDF contents; reading the document is the extraction agent's job.
    """
    contents = getattr(llm_request, "contents", None)
    if not contents:
        return
    kept: list = []
    for content in contents:
        parts = getattr(content, "parts", None)
        if not parts:
            kept.append(content)
            continue
        filtered = [
            p
            for p in parts
            if getattr(getattr(p, "inline_data", None), "mime_type", None) not in mimes
        ]
        if filtered:
            content.parts = filtered
            kept.append(content)
    llm_request.contents = kept


def _apply_masked_text(callback_context, llm_request) -> None:
    """Swap PII-masked text into the model request (screening / extraction).

    The intake node gets the email header summary and body de-identified by the
    Model Armor template (app/model_armor.py, ``scan(mask=True)``) and records ``{original text: masked text}`` in ``nodes._MASKED_TEXT`` for this
    invocation. Any text part whose text is exactly one of those originals is
    replaced here, so the LLM reads the masked version while the UI, session state
    and the reply address keep the original. Pure in-memory lookup (no network),
    so it cannot fail a model call. Builds new Content objects rather than
    mutating the request's parts in place.
    """
    from app import nodes

    mapping = nodes._MASKED_TEXT.get(getattr(callback_context, "invocation_id", "default"))
    contents = getattr(llm_request, "contents", None)
    if not mapping or not contents:
        return
    out: list = []
    for content in contents:
        parts = getattr(content, "parts", None) or []
        if not any(getattr(p, "text", None) in mapping for p in parts):
            out.append(content)
            continue
        new_parts = [
            genai_types.Part.from_text(text=mapping[p.text]) if getattr(p, "text", None) in mapping else p
            for p in parts
        ]
        out.append(genai_types.Content(role=getattr(content, "role", None), parts=new_parts))
    llm_request.contents = out


def _drop_model_turns(llm_request) -> None:
    """Remove prior model-role turns from a request.

    Extraction shares the ADK session with the screening agent, so by default its
    request replays the screening verdict (JSON) and screening markdown as model
    turns — which can anchor extraction on screening-stage phrasing instead of the
    PDF. Extraction only needs the intake user turn (PDF + email summary/body), so
    drop the sibling model turns for it.
    """
    contents = getattr(llm_request, "contents", None)
    if not contents:
        return
    llm_request.contents = [c for c in contents if getattr(c, "role", None) != "model"]


def _upload_to_gcs(att_bytes: bytes, att_mime: str, invocation_id: str) -> str | None:
    """Stage a large invoice PDF in GCS and return its gs:// URI.

    Vertex AI (unlike the Gemini Developer API) has no ephemeral Files API —
    files that exceed the 20 MB inline-request limit must be referenced via a
    Cloud Storage URI instead. Returns None when EXTRACTION_GCS_BUCKET isn't
    configured or the upload fails; the caller falls back to inline transport.
    """
    from app.config import EXTRACTION_GCS_BUCKET

    if not EXTRACTION_GCS_BUCKET:
        logger.warning(
            "extraction: PDF exceeds the inline-transport threshold but "
            "EXTRACTION_GCS_BUCKET is not configured — falling back to inline "
            "transport (may exceed Vertex AI's 20MB inline request cap)"
        )
        return None
    try:
        from google.cloud import storage

        client = storage.Client()
        blob_name = f"invoice-extraction-staging/{invocation_id}"
        blob = client.bucket(EXTRACTION_GCS_BUCKET).blob(blob_name)
        blob.upload_from_string(att_bytes, content_type=att_mime)
        return f"gs://{EXTRACTION_GCS_BUCKET}/{blob_name}"
    except Exception as exc:
        logger.warning("extraction: GCS upload failed (%s); falling back to inline transport", exc)
        return None


def _inject_invoice_pdf(callback_context, llm_request) -> None:
    """Append the intake PDF (stashed by the fetch node) to the extraction request.

    The extraction agent is a single-turn Workflow node, so ADK builds its request
    from the current turn only and windows out the fetch node's user-role PDF turn.
    Without the document the model fabricates values; re-injecting the real PDF here
    makes extraction read the actual invoice.

    For the files_api/multipass strategies (route_extraction_strategy), the PDF
    is staged in GCS and referenced by URI instead of embedded inline — Gemini's
    inline-request payload is capped at 20 MB.
    """
    from app import nodes

    inv = getattr(callback_context, "invocation_id", "default")
    stashed = nodes._INVOICE_PDF.get(inv)
    if not stashed:
        logger.warning("extraction: no invoice attachment stashed for invocation %s", inv)
        return
    att_bytes, att_mime = stashed

    strategy = None
    state = getattr(callback_context, "state", None)
    if state is not None:
        strategy = state.get("extraction_strategy")

    document_part = None
    if strategy in ("files_api", "multipass"):
        gs_uri = _upload_to_gcs(att_bytes, att_mime, inv)
        if gs_uri:
            document_part = genai_types.Part.from_uri(file_uri=gs_uri, mime_type=att_mime)
    if document_part is None:
        document_part = genai_types.Part.from_bytes(data=att_bytes, mime_type=att_mime)

    contents = getattr(llm_request, "contents", None)
    if contents is None:
        llm_request.contents = contents = []
    contents.append(
        genai_types.Content(
            role="user",
            parts=[
                genai_types.Part.from_text(
                    text="Extract the invoice below. Read every value directly from "
                    "this document; do not infer or invent."
                ),
                document_part,
            ],
        )
    )
