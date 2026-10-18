"""Email a fixture invoice PDF to the live AgentMail inbox for local/eval testing.

Intake is live-AgentMail-only (no mock mode, see tests/fixtures/invoices/README.md) —
this automates that README's documented manual step ("email the corresponding
fixture PDF to the demo mailbox just before a run") using
app.agentmail_tools.send_invoice_email, instead of changing intake itself.

Usage:
    uv run python tests/fixtures/seed_inbox.py 01_direct_straight_through.pdf
    uv run python tests/fixtures/seed_inbox.py sample_invoice.pdf
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.agentmail_tools import send_invoice_email
from app.config import AGENTMAIL_INBOX_ID

FIXTURES_DIR = Path(__file__).parent / "invoices"

# invoice_no / vendor per tests/fixtures/invoices/README.md, used to build a
# subject line that matches the subject filter in agentmail_tools.search_latest_invoice
# (Invoice OR INV- OR Bill OR Statement OR Receipt).
_SUBJECTS: dict[str, str] = {
    "01_direct_straight_through.pdf": "Invoice INV-2026-0301 from BluePeak Plastics",
    "02_indirect_straight_through.pdf": "Invoice INV-2026-0302 from BrightOffice Supplies Co",
    "03_high_value_approval.pdf": "Invoice INV-2026-0303 from Meridian Steel & Alloys",
    "04_duplicate_invoice_number.pdf": "Invoice INV-2024-001 from Cascade Components Inc",
    "05_duplicate_po.pdf": "Invoice INV-2026-0305 from Globex Trading Co",
    "06_malformed_po_missing_fields.pdf": "Invoice 2026/887-A from Globex Trading Co",
    "07_blurry_scan.pdf": "Invoice INV-2026-0307 from Helix Cloud Services",
    "08_business_rule_violation_zero_total.pdf": "Invoice INV-2026-0308 from Globex Trading Co",
}

# Deliberately varied body phrasing per fixture — a fixed template here would
# only ever exercise the screening_agent's body_signal rubric against one
# exact wording, understating how well it generalizes to real vendor emails
# (some restate the amount, some cite payment terms, some are a bare
# one-line transmittal with nothing beyond "invoice attached"). Keep
# 01_direct_straight_through.pdf's body terse/reference-only on purpose — it's
# the case that originally exposed the body_signal prompt-ambiguity
# regression (see app/prompts.py's INVOICE_SCREENING_INSTRUCTION), so it
# should keep testing exactly that boundary.
_BODIES: dict[str, str] = {
    "01_direct_straight_through.pdf": "Please find the attached invoice.",
    "02_indirect_straight_through.pdf": (
        "Hi team, please process the attached invoice for USD 920.00 at your "
        "earliest convenience."
    ),
    "03_high_value_approval.pdf": (
        "Attached is our invoice against PO-2026-1010. Payment terms are net 30 "
        "from the invoice date."
    ),
    "04_duplicate_invoice_number.pdf": (
        "Please see the attached invoice referencing PO-2026-1002 for the items "
        "delivered last month."
    ),
    "05_duplicate_po.pdf": (
        "Kindly remit payment per the attached invoice and PO-2026-0098. Let us "
        "know if you need anything else."
    ),
    "06_malformed_po_missing_fields.pdf": "Invoice attached.",
    "07_blurry_scan.pdf": "Apologies, our scanner was acting up — please see the attached scanned invoice.",
    "08_business_rule_violation_zero_total.pdf": (
        "Please find enclosed our invoice for the recent engagement. Let us know "
        "if any adjustments are needed."
    ),
    "sample_invoice.pdf": (
        "Attached please find our invoice for services rendered. We appreciate "
        "prompt payment per the agreed terms."
    ),
}


def seed(filename: str) -> None:
    if not AGENTMAIL_INBOX_ID:
        raise SystemExit("AGENTMAIL_INBOX_ID env var is not set — see ENV_SETUP.md")
    path = FIXTURES_DIR / filename if filename != "sample_invoice.pdf" else FIXTURES_DIR.parent / filename
    if not path.exists():
        raise SystemExit(f"fixture not found: {path}")
    subject = _SUBJECTS.get(filename, f"Invoice — {filename}")
    body = _BODIES.get(filename, "Please find the attached invoice.")
    ok = send_invoice_email(
        to=AGENTMAIL_INBOX_ID,
        subject=subject,
        body=body,
        attachment_bytes=path.read_bytes(),
        attachment_filename=path.name,
    )
    if not ok:
        raise SystemExit(f"send_invoice_email failed for {path.name} — check logs")
    print(f"Seeded {path.name} -> {AGENTMAIL_INBOX_ID} (subject: {subject!r})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"usage: {sys.argv[0]} <fixture_filename.pdf>")
    seed(sys.argv[1])
