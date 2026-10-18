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

import logging
import os


def setup_telemetry() -> str | None:
    """Configure OpenTelemetry and GenAI telemetry with GCS upload.

    Cloud Trace itself is wired by ``otel_to_cloud=True`` in fast_api_app.py (this
    function does not touch the tracer). Custom per-node decision spans
    (app/app_utils/tracing.py) are always emitted; set ``INVOICE_TRACE_VERBOSE=true``
    to additionally record sensitive business values (vendor, total, currency,
    PO ref) as span attributes. Default is metadata-only (off).

    LLM prompt/response content on the ``generate_content`` spans (the Cloud Trace
    Input/Output preview) is controlled by the OTel GenAI env vars:
      * ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` — ``NO_CONTENT`` (default),
        ``SPAN_ONLY`` (content on spans — surfaces in the Trace preview), ``EVENT_ONLY``,
        or ``SPAN_AND_EVENT``.
      * ``OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`` — required for the
        instrumentation to honor the modes above; set automatically below when a content
        mode is requested.
    Capturing content records invoice/prompt PII into telemetry — restrict Cloud Trace
    access accordingly.
    """

    bucket = os.environ.get("LOGS_BUCKET_NAME")
    capture_content = os.environ.get(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false"
    )
    # Operator explicitly requested on-span/on-event content capture. These modes are
    # only honored under the experimental semconv opt-in; ensure it and never downgrade
    # the chosen mode to NO_CONTENT in the GCS branch below.
    if capture_content.strip().upper() in {"SPAN_ONLY", "EVENT_ONLY", "SPAN_AND_EVENT"}:
        os.environ.setdefault(
            "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental"
        )
        logging.info(
            "GenAI message-content capture enabled - mode: %s (prompt/response recorded on spans)",
            capture_content.strip().upper(),
        )
    elif bucket and capture_content != "false":
        logging.info(
            "Prompt-response logging enabled - mode: NO_CONTENT (metadata only, no prompts/responses)"
        )
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "NO_CONTENT"
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT", "jsonl")
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
        os.environ.setdefault(
            "OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental"
        )
        commit_sha = os.environ.get("COMMIT_SHA", "dev")
        os.environ.setdefault(
            "OTEL_RESOURCE_ATTRIBUTES",
            f"service.namespace=invoice-processing-agent,service.version={commit_sha}",
        )
        path = os.environ.get("GENAI_TELEMETRY_PATH", "completions")
        os.environ.setdefault(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH",
            f"gs://{bucket}/{path}",
        )
    else:
        logging.info(
            "Prompt-response logging disabled (set LOGS_BUCKET_NAME=gs://your-bucket and OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT to enable)"
        )

    return bucket
