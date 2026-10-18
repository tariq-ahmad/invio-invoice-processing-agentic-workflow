"""Shared, LLM-free helpers used across the invoice-processing node modules."""

import re
from datetime import UTC, datetime
from typing import Any

from google.genai import types as genai_types

from app.a2ui_views import a2ui_parts
from app.config import BUYER_ORG, PRODUCT_NAME

_HR = "---"


def _lenient_json_objects(text: str) -> list[dict]:
    """Recover every complete top-level object from a (possibly truncated) JSON array.

    Decodes objects one at a time and stops at the first incomplete one, so a tail
    cut off by the model's output-token limit never discards the items that did
    finish. Nesting-agnostic (unlike a naive last-brace trim).
    """
    import json

    s = text.strip()
    i = s.find("[")
    if i == -1:
        return []
    s = s[i + 1:]
    dec = json.JSONDecoder()
    out: list[dict] = []
    while True:
        s = s.lstrip().lstrip(",").lstrip()
        if not s or s[0] == "]":
            break
        try:
            obj, end = dec.raw_decode(s)
        except json.JSONDecodeError:
            break  # first incomplete object → keep what we have
        if isinstance(obj, dict):
            out.append(obj)
        s = s[end:]
    return out


# Pattern embedded by store.autoStart when an email triggers via SSE.
# Carries the AgentMail message_id (URL-encoded: ids are RFC Message-IDs that
# contain characters like <, @ and >) so fetch_invoice_email fetches that exact
# message. Callers must unquote() the captured group.
_MSG_ID_RE = re.compile(r"MSG_ID=([A-Za-z0-9_.~!*'()%\-]+)")


def _extract_text(content: Any) -> str:
    """Pull plain text from types.Content or fall back to str()."""
    if hasattr(content, "parts"):
        return " ".join(p.text for p in content.parts if hasattr(p, "text") and p.text)
    return str(content)


def _content_msg(text: str) -> genai_types.Content:
    return genai_types.Content(
        role="model", parts=[genai_types.Part.from_text(text=text)]
    )


def _content_with_a2ui(
    text: str, a2ui_messages: list[dict] | None
) -> genai_types.Content:
    """Build a model Content with a markdown text part plus optional A2UI parts.

    The A2UI parts (inline-data, MIME application/a2ui+json) are appended after
    the markdown so the ADK dev playground still renders the text while the
    custom React front end picks up the A2UI surface. Emission is gated by
    ENABLE_A2UI via the caller passing None to skip.
    """
    parts = [genai_types.Part.from_text(text=text)]
    if a2ui_messages:
        parts += a2ui_parts(a2ui_messages)
    return genai_types.Content(role="model", parts=parts)


def _normalize_resume(value: Any) -> Any:
    """Normalize a HITL resume input that may be a plain string or a dict.

    The web front end may resume an interrupt with either a bare decision word
    (delivered as a string) or a structured A2UI action payload (a dict, e.g.
    {"decision": "send", "subject": ..., "body": ...}). Returns the value
    unchanged; callers branch on isinstance(..., dict).
    """
    return value


def _safe_float(value: str | None) -> float | None:
    """Parse a numeric string, stripping currency symbols/codes and commas."""
    if not value:
        return None
    cleaned = value.replace(",", "").strip().lstrip("$")
    cleaned = re.sub(r"^[A-Za-z]{3}\s*", "", cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    """UTC timestamp, ISO-8601, second precision (e.g. 2026-06-17T14:05:09Z)."""
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_display() -> str:
    """Human-readable UTC timestamp for chat summaries."""
    return _now().strftime("%d %b %Y, %H:%M UTC")


def _brand_footer() -> str:
    """Consistent, understated footer line for chat summaries."""
    return f"*{PRODUCT_NAME} · {BUYER_ORG} · Accounts Payable Automation*"
