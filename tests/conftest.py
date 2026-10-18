from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# The invoice fixture PDFs are gitignored (repo "Invoice PII — never commit"
# policy) and regenerated on demand, so a fresh clone / CI has them on disk
# before test collection. Regeneration is skipped when the fixtures are present.

_FIXTURES = Path(__file__).parent / "fixtures"
_INVOICES = _FIXTURES / "invoices"
_GENERATOR = _FIXTURES / "generate_invoices.py"

_EXPECTED = (
    _FIXTURES / "sample_invoice.pdf",
    _INVOICES / "01_direct_straight_through.pdf",
    _INVOICES / "02_indirect_straight_through.pdf",
    _INVOICES / "03_high_value_approval.pdf",
    _INVOICES / "04_duplicate_invoice_number.pdf",
    _INVOICES / "05_duplicate_po.pdf",
    _INVOICES / "06_malformed_po_missing_fields.pdf",
    _INVOICES / "07_blurry_scan.pdf",
    _INVOICES / "07_blurry_scan.png",
    _INVOICES / "08_business_rule_violation_zero_total.pdf",
)


def _regenerate_fixtures() -> None:
    spec = importlib.util.spec_from_file_location("generate_invoices", _GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()


def pytest_configure(config: object) -> None:
    if not all(path.is_file() for path in _EXPECTED):
        _regenerate_fixtures()


@pytest.fixture(autouse=True)
def firestore_backend():
    """Run every test against an in-memory config store — never the live Firestore.

    The PO ledger and the posted-invoice history are seeded from the in-code mock
    data (what deployment/seed_firestore.py writes); no policy documents exist, so
    the in-code policy defaults apply. Tests that need specific policy / PO
    documents take this fixture and call ``.set(collection, doc_id, data)``.
    """
    from app import config as app_config
    from app.duplicate_check import history_document
    from app.firestore_store import InMemoryBackend, set_backend
    from app.policy import reset_policy_cache

    backend = InMemoryBackend(
        {
            app_config.PURCHASE_ORDERS_COLLECTION: {
                po_no: dict(record) for po_no, record in app_config.MOCK_PO_DATABASE.items()
            },
            app_config.POSTED_INVOICES_COLLECTION: {
                e["document_id"]: history_document(e) for e in app_config.MOCK_POSTED_INVOICES
            },
        }
    )
    set_backend(backend)
    reset_policy_cache()
    yield backend
    set_backend(None)
    reset_policy_cache()


@pytest.fixture(autouse=True)
def no_review_summary_llm(monkeypatch):
    """The approval-card summary calls Gemini; tests use the deterministic fallback."""
    from app import review_summary

    monkeypatch.setattr(review_summary, "ENABLED", False)
