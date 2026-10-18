"""AgentMail request/response helpers: message fetch, attachment selection,
catch-up listing and message-id round-tripping."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import quote, unquote

from app import agentmail_tools
from app.nodes._helpers import _MSG_ID_RE

INBOX = "ap@agentmail.to"
# Characters JavaScript's encodeURIComponent leaves unescaped.
_JS_SAFE = "!'()*-._~"


def _att(attachment_id, filename, content_type, *, disposition=None, content_id=None):
    return SimpleNamespace(
        attachment_id=attachment_id,
        filename=filename,
        content_type=content_type,
        content_disposition=disposition,
        content_id=content_id,
    )


def _install(monkeypatch, *, messages):
    fake = SimpleNamespace(inboxes=SimpleNamespace(messages=messages))
    monkeypatch.setattr(agentmail_tools, "_client", fake)
    monkeypatch.setattr(agentmail_tools, "AGENTMAIL_INBOX_ID", INBOX)


# -- attachment selection ------------------------------------------------------


def test_pick_attachment_prefers_pdf_over_image():
    img = _att("a1", "scan.png", "image/png")
    pdf = _att("a2", "invoice.pdf", "application/pdf")
    assert agentmail_tools._pick_attachment([img, pdf]) is pdf


def test_pick_attachment_skips_inline_and_unknown_types():
    logo = _att("a1", "logo.png", "image/png", disposition="inline")
    cid = _att("a2", "sig.png", "image/png", content_id="<sig@x>")
    zipf = _att("a3", "data.zip", "application/zip")
    assert agentmail_tools._pick_attachment([logo, cid, zipf]) is None


def test_pick_attachment_keeps_attachment_that_also_has_content_id():
    # Apple Mail stamps a Content-ID on ordinary attachments; the disposition wins.
    pdf = _att(
        "a1", "sample_invoice.pdf", "application/pdf",
        disposition="attachment", content_id="<A130DBAF-5C36-4956-A950-CB42D9C7BE72>",
    )
    assert agentmail_tools._pick_attachment([pdf]) is pdf


def test_pick_attachment_inline_disposition_wins_over_missing_content_id():
    logo = _att("a1", "logo.png", "image/png", disposition="inline")
    assert agentmail_tools._pick_attachment([logo]) is None


def test_pick_attachment_matches_by_extension_when_mime_is_generic():
    f = _att("a1", "Invoice.PDF", "application/octet-stream")
    assert agentmail_tools._pick_attachment([f]) is f


# -- fetch_invoice -------------------------------------------------------------


def test_fetch_invoice_returns_expected_shape(monkeypatch):
    msg = SimpleNamespace(
        subject="Invoice INV-1",
        from_="Vendor <v@example.com>",
        timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        text="Please pay.",
        html=None,
        attachments=[_att("a1", "inv.pdf", "application/pdf")],
    )

    class _Messages:
        def get(self, inbox_id, message_id):
            assert (inbox_id, message_id) == (INBOX, "<m1@agentmail.to>")
            return msg

        def get_attachment(self, inbox_id, message_id, attachment_id):
            assert attachment_id == "a1"
            return SimpleNamespace(download_url="https://cdn.example/a1")

    _install(monkeypatch, messages=_Messages())
    monkeypatch.setattr(agentmail_tools, "_download", lambda url: b"%PDF-fake")

    out = agentmail_tools.fetch_invoice("<m1@agentmail.to>")

    assert set(out) == {
        "message_id", "subject", "sender", "date", "body_text",
        "attachment_bytes", "attachment_filename", "attachment_mime", "pdf_text",
    }
    assert out["sender"] == "Vendor <v@example.com>"
    assert out["body_text"] == "Please pay."
    assert out["attachment_bytes"] == b"%PDF-fake"
    assert out["attachment_filename"] == "inv.pdf"
    assert out["attachment_mime"] == "application/pdf"


def test_fetch_invoice_falls_back_to_html_body_without_attachment(monkeypatch):
    msg = SimpleNamespace(
        subject="Hello", from_="a@b.c", timestamp=None, text=None,
        html="<style>p{}</style><p>Invoice <b>attached</b></p>", attachments=None,
    )
    _install(monkeypatch, messages=SimpleNamespace(get=lambda i, m: msg))

    out = agentmail_tools.fetch_invoice("m1")

    assert out["body_text"] == "Invoice attached"
    assert out["attachment_bytes"] is None
    assert out["date"] == ""


# -- catch-up listing ----------------------------------------------------------


def _item(message_id, labels):
    return SimpleNamespace(message_id=message_id, labels=labels)


def test_list_unhandled_filters_sent_and_ap_labels_and_orders_oldest_first(monkeypatch):
    page = SimpleNamespace(
        messages=[  # API order: newest first
            _item("new", ["received", "unread"]),
            _item("done", ["received", agentmail_tools.PROCESSED_LABEL]),
            _item("ours", ["sent"]),
            _item("old", ["received"]),
        ],
        next_page_token=None,
    )
    _install(monkeypatch, messages=SimpleNamespace(list=lambda inbox_id, **kw: page))

    assert agentmail_tools.list_unhandled_message_ids(30) == ["old", "new"]


def test_list_unhandled_disabled_when_window_is_zero(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("should not call the API")

    _install(monkeypatch, messages=SimpleNamespace(list=_boom))
    assert agentmail_tools.list_unhandled_message_ids(0) == []


def test_is_handled():
    assert agentmail_tools.is_handled(["received", agentmail_tools.IN_PROGRESS_LABEL])
    assert not agentmail_tools.is_handled(["received"])
    assert not agentmail_tools.is_handled(None)


# -- message-id round trip through the run prompt ------------------------------


def test_message_id_round_trips_through_prompt_regex():
    message_id = "<CAF+abc123=xyz@mail.gmail.com>"
    # Mirrors usePipelineStore.autoStart: encodeURIComponent(messageId)
    prompt = (
        "Fetch and process the latest invoice. "
        f"MSG_ID={quote(message_id, safe=_JS_SAFE)}"
    )

    m = _MSG_ID_RE.search(prompt)
    assert m is not None
    assert unquote(m.group(1)) == message_id
