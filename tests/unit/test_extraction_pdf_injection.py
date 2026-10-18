"""Regression tests for the extraction PDF hand-off.

The extraction agent runs as a single-turn Workflow node, so ADK windows the
intake PDF turn out of its request. The fetch node stashes the PDF in
nodes._INVOICE_PDF and the extraction before_model_callback re-injects it; without
that the model hallucinates instead of reading the document.
"""

from types import SimpleNamespace

from app import agent, nodes


class _Req:
    def __init__(self, contents: list) -> None:
        self.contents = contents


def _ctx(invocation_id: str) -> SimpleNamespace:
    return SimpleNamespace(invocation_id=invocation_id, state={})


def _pdf_mimes(req: _Req) -> list[str | None]:
    return [
        getattr(getattr(p, "inline_data", None), "mime_type", None)
        for c in req.contents
        for p in getattr(c, "parts", [])
    ]


def test_extraction_reinjects_stashed_pdf() -> None:
    inv = "inv-inject-1"
    nodes._INVOICE_PDF[inv] = (b"%PDF-1.4 fake bytes", "application/pdf")
    req = _Req(contents=[])

    agent._extraction_before(_ctx(inv), req)
    assert "application/pdf" in _pdf_mimes(req)

    # PDF is now consumed by emit_extraction_card (line-items phase), not here.
    agent._extraction_after(_ctx(inv), SimpleNamespace(usage_metadata=None))
    assert inv in nodes._INVOICE_PDF  # cache entry stays until emit_extraction_card pops it
    nodes._INVOICE_PDF.pop(inv)  # cleanup for test isolation


def test_extraction_no_pdf_when_cache_empty() -> None:
    inv = "inv-inject-missing"
    nodes._INVOICE_PDF.pop(inv, None)
    req = _Req(contents=[])

    agent._extraction_before(_ctx(inv), req)
    assert "application/pdf" not in _pdf_mimes(req)
