"""AgentMail helpers for invoice intake and vendor notification.

AgentMail (https://docs.agentmail.to) replaces Gmail as the intake mailbox. The
inbox is addressed by ``AGENTMAIL_INBOX_ID`` (its email address, e.g.
``invoices@agentmail.to``) and authenticated by a single API key — no OAuth
refresh tokens, Pub/Sub topics or watch renewals.

New-mail *triggering* lives in ``app/agentmail_listener.py`` (WebSocket). This
module is the request/response side: fetch a message and its attachment, apply
lifecycle labels, send mail, and list unhandled mail for catch-up after a
reconnect.

Labels are free-form strings on a message, so the AP lifecycle labels need no
creation step (unlike Gmail). Labeling is best-effort: a failure never blocks
intake.
"""

import base64
import io
import logging
import re
from datetime import UTC, datetime, timedelta

import requests
from pypdf import PdfReader

from app.app_utils.tracing import traced_external
from app.config import (
    AGENTMAIL_API_KEY,
    AGENTMAIL_API_KEY_SECRET,
    AGENTMAIL_INBOX_ID,
    AGENTMAIL_SEED_INBOX_ID,
)

logger = logging.getLogger(__name__)

_SYSTEM = "agentmail"
_TIMEOUT = 30

# Subjects the manual "latest invoice" fallback considers (same intent as the old
# Gmail INVOICE_QUERY). AgentMail's subject filter is a substring match, so the
# pattern is applied client-side.
_INVOICE_SUBJECT_RE = re.compile(r"\b(invoice|inv-|bill|statement|receipt)", re.IGNORECASE)
_SEARCH_WINDOW = timedelta(hours=24)

PROCESSED_LABEL = "AP-Processed"
IN_PROGRESS_LABEL = "AP-InProgress"
NO_ACTION_LABEL = "AP-NoAction"
# Stamped when Model Armor flags the email/attachment (see app/model_armor.py). It is
# in _AP_LABELS: retrying a flagged message would just flag it again, so it stays
# in the inbox, out of the retry loop, for AP staff to review by hand.
QUARANTINED_LABEL = "AP-Quarantined"
# Stamped when a run fails part-way (e.g. a Firestore outage during the PO
# lookup). It is deliberately NOT in _AP_LABELS: an errored message must stay
# "unhandled" so the next reconnect catch-up (or the manual latest-invoice
# search) picks it up again. A later retry adds AP-InProgress and then
# AP-Processed; the old AP-Error label is left in place as a record that the
# message failed at least once (it never makes the message count as handled).
ERROR_LABEL = "AP-Error"
_AP_LABELS = frozenset({PROCESSED_LABEL, IN_PROGRESS_LABEL, NO_ACTION_LABEL, QUARANTINED_LABEL})
# Label AgentMail stamps on mail we sent ourselves; never treat it as intake.
_SENT_LABEL = "sent"

_client = None
_async_client = None


def _api_key() -> str:
    """API key from the env, else from Secret Manager (Cloud Run)."""
    if AGENTMAIL_API_KEY:
        return AGENTMAIL_API_KEY
    if AGENTMAIL_API_KEY_SECRET:
        from google.cloud import secretmanager

        sm = secretmanager.SecretManagerServiceClient()
        payload = sm.access_secret_version(name=AGENTMAIL_API_KEY_SECRET).payload.data
        return payload.decode("utf-8-sig").strip()
    raise RuntimeError(
        "AgentMail API key not configured: set AGENTMAIL_API_KEY or "
        "AGENTMAIL_API_KEY_SECRET (see ENV_SETUP.md)."
    )


def get_client():
    """Process-wide synchronous AgentMail client (built lazily)."""
    global _client
    if _client is None:
        from agentmail import AgentMail

        _client = AgentMail(api_key=_api_key())
    return _client


def get_async_client():
    """Process-wide asynchronous AgentMail client (used by the listener)."""
    global _async_client
    if _async_client is None:
        from agentmail import AsyncAgentMail

        _async_client = AsyncAgentMail(api_key=_api_key())
    return _async_client


def _require_inbox() -> str:
    if not AGENTMAIL_INBOX_ID:
        raise RuntimeError("AGENTMAIL_INBOX_ID is not set (see ENV_SETUP.md).")
    return AGENTMAIL_INBOX_ID


def is_handled(labels: list[str] | None) -> bool:
    """True if the message already carries any AP lifecycle label."""
    return bool(_AP_LABELS.intersection(labels or []))


def _is_inbound(labels: list[str] | None) -> bool:
    return _SENT_LABEL not in (labels or [])


def list_unhandled_message_ids(since_minutes: int, max_pages: int = 5) -> list[str]:
    """IDs of inbound messages from the last ``since_minutes`` with no AP label.

    Used for catch-up after a (re)connect: the WebSocket does not replay events
    missed while the service was down or redeploying, but the AP labels are a
    durable record of what has already been picked up. Returned oldest first.
    """
    if since_minutes <= 0:
        return []
    inbox_id = _require_inbox()
    after = datetime.now(UTC) - timedelta(minutes=since_minutes)
    ids: list[str] = []
    page_token = None
    for _ in range(max_pages):
        resp = get_client().inboxes.messages.list(
            inbox_id, after=after, limit=100, page_token=page_token
        )
        for m in resp.messages:
            if _is_inbound(m.labels) and not is_handled(m.labels):
                ids.append(m.message_id)
        page_token = resp.next_page_token
        if not page_token:
            break
    ids.reverse()  # API returns newest first
    logger.info("list_unhandled_message_ids: %d unhandled in last %dm", len(ids), since_minutes)
    return ids


@traced_external("agentmail.search_latest_invoice", system=_SYSTEM)
def search_latest_invoice() -> str | None:
    """Return the id of the newest unhandled invoice-looking email, or None.

    Manual fallback for a run that was not triggered by a specific message.
    """
    inbox_id = _require_inbox()
    resp = get_client().inboxes.messages.list(
        inbox_id, after=datetime.now(UTC) - _SEARCH_WINDOW, limit=50
    )
    for m in resp.messages:  # newest first
        if (
            _is_inbound(m.labels)
            and not is_handled(m.labels)
            and m.attachments
            and _INVOICE_SUBJECT_RE.search(m.subject or "")
        ):
            return m.message_id
    return None


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Best-effort text extraction; the model reads the raw PDF for the real work."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages).strip()
    except Exception as exc:
        logger.warning("pypdf extraction failed: %s", exc)
        return ""


_INVOICE_ATTACHMENT_MIMES = frozenset({
    "application/pdf",
    "image/jpeg", "image/png", "image/tiff", "image/webp",
    "text/html",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
})
_INVOICE_ATTACHMENT_EXTS = frozenset({
    ".pdf",
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp",
    ".html", ".htm",
    ".doc", ".docx",
})


def _is_pdf(att) -> bool:
    return (att.content_type or "").lower() == "application/pdf" or (
        att.filename or ""
    ).lower().endswith(".pdf")


def _is_inline(att) -> bool:
    """True for embedded parts (signature images, CID references).

    The Content-Disposition is authoritative: some mail clients (Apple Mail,
    Outlook) also stamp a Content-ID on ordinary attachments, so a Content-ID
    alone must not mark a part inline. It is only a fallback when the
    disposition is absent.
    """
    disposition = (att.content_disposition or "").lower()
    if disposition:
        return disposition == "inline"
    return bool(att.content_id)


def _pick_attachment(attachments) -> object | None:
    """First invoice-like, non-inline attachment; a PDF wins over an image."""
    candidates = []
    for att in attachments or []:
        fname = (att.filename or "").lower()
        if _is_inline(att) or not fname:
            continue
        if (att.content_type or "").lower() in _INVOICE_ATTACHMENT_MIMES or any(
            fname.endswith(e) for e in _INVOICE_ATTACHMENT_EXTS
        ):
            candidates.append(att)
    return next((a for a in candidates if _is_pdf(a)), candidates[0] if candidates else None)


def _html_to_text(html: str) -> str:
    """Crude tag strip for HTML-only emails (the screening agent just needs words)."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def _download(url: str) -> bytes:
    r = requests.get(url, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.content


@traced_external("agentmail.fetch_invoice", system=_SYSTEM)
def fetch_invoice(msg_id: str) -> dict:
    """Fetch the message: metadata, body text, and the first invoice attachment.

    Returns a dict with keys: message_id, subject, sender, date, body_text,
    attachment_bytes (bytes | None), attachment_filename, attachment_mime, pdf_text.
    Accepts PDF and common image formats (JPEG, PNG, TIFF, WebP).

    Always re-fetches over the REST API rather than trusting the WebSocket event
    payload, which can omit ``text``/``html`` for large messages.
    """
    inbox_id = _require_inbox()
    client = get_client()
    msg = client.inboxes.messages.get(inbox_id, msg_id)

    body_text = (msg.text or "").strip()
    if not body_text and msg.html:
        body_text = _html_to_text(msg.html)

    attachment_bytes: bytes | None = None
    attachment_filename = ""
    attachment_mime = ""
    att = _pick_attachment(msg.attachments)
    if att is not None:
        meta = client.inboxes.messages.get_attachment(inbox_id, msg_id, att.attachment_id)
        attachment_bytes = _download(meta.download_url)
        attachment_filename = att.filename or ""
        attachment_mime = (att.content_type or "").lower() or "application/octet-stream"

    return {
        "message_id": msg_id,
        "subject": msg.subject or "",
        "sender": msg.from_ or "",
        "date": msg.timestamp.strftime("%a, %d %b %Y %H:%M:%S %Z").strip() if msg.timestamp else "",
        "body_text": body_text,
        "attachment_bytes": attachment_bytes,
        "attachment_filename": attachment_filename,
        "attachment_mime": attachment_mime,
        "pdf_text": _extract_pdf_text(attachment_bytes)
        if attachment_bytes and attachment_mime == "application/pdf"
        else "",
    }


def _modify_labels(
    msg_id: str, add: list[str], remove: list[str] | None = None
) -> bool:
    """Add and/or remove labels on a message. Best-effort: returns False on failure."""
    try:
        kwargs: dict = {"add_labels": add}
        if remove:
            kwargs["remove_labels"] = remove
        get_client().inboxes.messages.update(_require_inbox(), msg_id, **kwargs)
        return True
    except Exception as exc:
        logger.warning(
            "label update failed for %s (add=%s remove=%s): %s", msg_id, add, remove, exc
        )
        return False


@traced_external("agentmail.label_in_progress", system=_SYSTEM)
def label_in_progress(msg_id: str) -> bool:
    """Mark a freshly-seen message as in-flight (AP-InProgress). Doubles as the
    'seen' dedupe marker so catch-up and the manual search skip it while it runs."""
    return _modify_labels(msg_id, [IN_PROGRESS_LABEL])


@traced_external("agentmail.mark_processed", system=_SYSTEM)
def mark_processed(msg_id: str) -> bool:
    """Final outcome: the AP pipeline acted on this invoice. Adds AP-Processed and
    clears the interim AP-InProgress."""
    return _modify_labels(msg_id, [PROCESSED_LABEL], [IN_PROGRESS_LABEL])


@traced_external("agentmail.mark_no_action", system=_SYSTEM)
def mark_no_action(msg_id: str) -> bool:
    """Final outcome: screened out as a non-invoice. Adds AP-NoAction and clears
    the interim AP-InProgress."""
    return _modify_labels(msg_id, [NO_ACTION_LABEL], [IN_PROGRESS_LABEL])


@traced_external("agentmail.mark_quarantined", system=_SYSTEM)
def mark_quarantined(msg_id: str) -> bool:
    """Final outcome: Model Armor flagged the content, so the invoice was not
    processed. Adds AP-Quarantined and clears the interim AP-InProgress."""
    return _modify_labels(msg_id, [QUARANTINED_LABEL], [IN_PROGRESS_LABEL])


@traced_external("agentmail.mark_error", system=_SYSTEM)
def mark_error(msg_id: str) -> bool:
    """Final outcome: the run failed before the invoice was handled. Adds AP-Error
    and clears AP-InProgress so the message is eligible for a retry."""
    return _modify_labels(msg_id, [ERROR_LABEL], [IN_PROGRESS_LABEL])


@traced_external("agentmail.send_email", system=_SYSTEM)
def send_email(to: str, subject: str, body: str) -> bool:
    """Send an email from the AP inbox. Returns True on success."""
    try:
        get_client().inboxes.messages.send(
            _require_inbox(), to=to, subject=subject, text=body
        )
        logger.info("send_email: sent to %s subject=%r", to, subject)
        return True
    except Exception as exc:
        logger.warning("send_email failed: %s", exc)
        return False


@traced_external("agentmail.send_invoice_email", system=_SYSTEM)
def send_invoice_email(
    to: str, subject: str, body: str, attachment_bytes: bytes, attachment_filename: str
) -> bool:
    """Deliver an invoice email with a PDF attachment into the AP inbox.

    Not used by the production pipeline (which only ever reads real inbound
    mail) — this seeds ``tests/fixtures/invoices/*.pdf`` into the live inbox for
    local/eval testing. Unlike Gmail there is no direct-insert API, so this sends
    real mail: from ``AGENTMAIL_SEED_INBOX_ID`` if set, else from the AP inbox
    itself (self-send). Returns True on success.
    """
    try:
        from agentmail import SendAttachment

        sender_inbox = AGENTMAIL_SEED_INBOX_ID or _require_inbox()
        get_client().inboxes.messages.send(
            sender_inbox,
            to=to,
            subject=subject,
            text=body,
            attachments=[
                SendAttachment(
                    filename=attachment_filename,
                    content_type="application/pdf",
                    content=base64.b64encode(attachment_bytes).decode("ascii"),
                )
            ],
        )
        logger.info(
            "send_invoice_email: sent to %s subject=%r attachment=%s",
            to, subject, attachment_filename,
        )
        return True
    except Exception as exc:
        logger.warning("send_invoice_email failed: %s", exc)
        return False
