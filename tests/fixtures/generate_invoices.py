"""Generate the demo / test invoice fixtures for the invoice-processing agent.

Run once (or whenever the scenarios change):

    uv run python tests/fixtures/generate_invoices.py

It produces a polished, branded set of invoice PDFs covering the happy paths and
the negative / edge cases, plus a deliberately blurry scanned image (PNG + PDF)
to exercise the low-confidence → human-review route. The committed artifacts are
what the pipeline consumes; reportlab + pillow are only needed to (re)generate
them (declared in the ``dev`` dependency group).

Each fixture's expected pipeline outcome is documented in
``tests/fixtures/invoices/README.md`` and asserted (at the rule-engine level) in
``tests/unit/test_scenarios.py``.

Scenarios are intentionally aligned with the mock master data in
``app/config.py`` (the S/4HANA PO ledger and the posted-invoice history that duplicate detection queries) so the PO
matching, duplicate detection and routing all resolve coherently.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
INVOICE_DIR = os.path.join(_THIS_DIR, "invoices")
SAMPLE_PDF = os.path.join(_THIS_DIR, "sample_invoice.pdf")

# Buyer (bill-to) - the fictional enterprise running AP Copilot.
BILL_TO = [
    "Northwind Industries, Inc.",
    "Accounts Payable Dept.",
    "1400 Lakeshore Parkway, Suite 600",
    "Chicago, IL 60601, USA",
    "ap@northwind.example",
]

PAGE_W, PAGE_H = LETTER


# ---------------------------------------------------------------------------
# PDF invoice rendering
# ---------------------------------------------------------------------------


def _money(value: float, currency: str = "USD") -> str:
    return f"{currency} {value:,.2f}"


# Typographic punctuation -> ASCII (keys built from code points so the source
# stays plain ASCII) so the base-14 Helvetica font renders reliably in any PDF
# viewer with no missing-glyph boxes.
_PUNCT_TO_ASCII = {
    chr(0x2014): "-",  # em dash
    chr(0x2013): "-",  # en dash
    chr(0x00B7): "-",  # middle dot
    chr(0x2018): "'",  # left single quote
    chr(0x2019): "'",  # right single quote
    chr(0x201C): '"',  # left double quote
    chr(0x201D): '"',  # right double quote
    chr(0x00D7): "x",  # multiplication sign
}


def _ascii(text: str) -> str:
    """Map typographic punctuation to ASCII for reliable PDF rendering."""
    out = str(text)
    for src, dst in _PUNCT_TO_ASCII.items():
        out = out.replace(src, dst)
    return out


def draw_invoice(c: canvas.Canvas, spec: dict) -> None:
    """Draw one professional-looking invoice page onto the canvas."""
    currency = spec.get("currency", "USD")
    left = 0.75 * inch
    right = PAGE_W - 0.75 * inch
    y = PAGE_H - 0.9 * inch

    # ---- Vendor header (top-left) ----
    c.setFillColorRGB(0.10, 0.12, 0.20)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(left, y, _ascii(spec["vendor"]))
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0.35, 0.38, 0.45)
    vy = y - 16
    for line in spec.get("vendor_addr", []):
        c.drawString(left, vy, _ascii(line))
        vy -= 12

    # ---- INVOICE label + meta (top-right) ----
    c.setFillColorRGB(0.10, 0.12, 0.20)
    c.setFont("Helvetica-Bold", 24)
    c.drawRightString(right, y, "INVOICE")
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.25, 0.28, 0.35)
    meta = [("Invoice No.", spec["invoice_no"]), ("Date", spec["date"])]
    if spec.get("show_po", True):
        meta.append(("PO Reference", spec.get("po", "")))
    meta.append(("Terms", spec.get("terms", "Net 30")))
    my = y - 22
    for label, val in meta:
        c.setFillColorRGB(0.45, 0.48, 0.55)
        c.drawRightString(right - 1.6 * inch, my, f"{label}:")
        c.setFillColorRGB(0.12, 0.14, 0.22)
        c.drawRightString(right, my, _ascii(str(val)))
        my -= 15

    # ---- Divider ----
    y = min(vy, my) - 10
    c.setStrokeColorRGB(0.80, 0.82, 0.88)
    c.setLineWidth(1)
    c.line(left, y, right, y)
    y -= 22

    # ---- Bill To ----
    c.setFont("Helvetica-Bold", 9)
    c.setFillColorRGB(0.45, 0.48, 0.55)
    c.drawString(left, y, "BILL TO")
    y -= 14
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.15, 0.17, 0.25)
    for line in BILL_TO:
        c.drawString(left, y, _ascii(line))
        y -= 13
    y -= 14

    # ---- Line-item table header ----
    col_desc = left
    col_qty = right - 3.2 * inch
    col_unit = right - 1.9 * inch
    col_amt = right
    c.setFillColorRGB(0.10, 0.12, 0.20)
    c.rect(left - 4, y - 4, (right - left) + 8, 20, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(col_desc, y + 2, "DESCRIPTION")
    c.drawRightString(col_qty, y + 2, "QTY")
    c.drawRightString(col_unit, y + 2, "UNIT PRICE")
    c.drawRightString(col_amt, y + 2, "AMOUNT")
    y -= 24

    # ---- Line items ----
    c.setFont("Helvetica", 10)
    for item in spec["line_items"]:
        desc, qty, unit, amount = item
        c.setFillColorRGB(0.18, 0.20, 0.28)
        c.drawString(col_desc, y, _ascii(str(desc)))
        if qty != "":
            c.drawRightString(col_qty, y, str(qty))
        if unit != "":
            c.drawRightString(col_unit, y, _money(float(unit), currency))
        c.drawRightString(col_amt, y, _money(float(amount), currency))
        y -= 16
        c.setStrokeColorRGB(0.90, 0.91, 0.94)
        c.setLineWidth(0.5)
        c.line(left, y + 6, right, y + 6)

    # ---- Totals ----
    y -= 8
    subtotal = spec.get("subtotal", spec["total"])
    tax = spec.get("tax", 0.0)
    total_label = spec.get("total_label", "TOTAL DUE")
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.35, 0.38, 0.45)
    c.drawRightString(col_unit, y, "Subtotal")
    c.setFillColorRGB(0.18, 0.20, 0.28)
    c.drawRightString(col_amt, y, _money(float(subtotal), currency))
    y -= 15
    c.setFillColorRGB(0.35, 0.38, 0.45)
    c.drawRightString(col_unit, y, "Tax (0%)")
    c.setFillColorRGB(0.18, 0.20, 0.28)
    c.drawRightString(col_amt, y, _money(float(tax), currency))
    y -= 8
    c.setStrokeColorRGB(0.80, 0.82, 0.88)
    c.line(col_unit - 0.3 * inch, y, right, y)
    y -= 18
    c.setFont("Helvetica-Bold", 12)
    c.setFillColorRGB(0.10, 0.12, 0.20)
    c.drawRightString(col_unit, y, total_label)
    c.drawRightString(col_amt, y, _money(float(spec["total"]), currency))

    # ---- Notes / footer ----
    y -= 40
    if spec.get("notes"):
        c.setFont("Helvetica-Oblique", 9)
        c.setFillColorRGB(0.40, 0.43, 0.50)
        c.drawString(left, y, _ascii(spec["notes"]))
        y -= 16
    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.55, 0.58, 0.65)
    c.drawString(
        left,
        0.7 * inch,
        _ascii(
            f"Remit to {spec['vendor']}  -  Payment due {spec.get('terms', 'Net 30')}"
            "  -  Thank you for your business."
        ),
    )


def write_pdf(path: str, spec: dict) -> None:
    c = canvas.Canvas(path, pagesize=LETTER)
    c.setTitle(f"{spec['vendor']} - {spec['invoice_no']}")
    draw_invoice(c, spec)
    c.showPage()
    c.save()
    print(f"  wrote {os.path.relpath(path, _REPO_ROOT)}")


# ---------------------------------------------------------------------------
# Blurry scanned-image invoice (PNG + PDF)
# ---------------------------------------------------------------------------


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def write_blurry(png_path: str, pdf_path: str, spec: dict) -> None:
    """Render an invoice as a low-quality, blurry 'scan' (PNG) and wrap in a PDF.

    The blur + downscale-upscale + noise makes field extraction uncertain, which
    should drive overall_confidence below threshold and route to human review.
    """
    scale = 2
    W, H = 1000, 1294  # ~ letter aspect
    img = Image.new("RGB", (W * scale, H * scale), (250, 250, 248))
    d = ImageDraw.Draw(img)
    f_big = _font(34 * scale)
    f_hdr = _font(20 * scale)
    f_med = _font(16 * scale)

    m = 60 * scale
    d.text((m, m), spec["vendor"], fill=(20, 22, 40), font=f_big)
    d.text((W * scale - 320 * scale, m), "INVOICE", fill=(20, 22, 40), font=f_hdr)
    y = m + 70 * scale
    for label, val in [
        ("Invoice No.", spec["invoice_no"]),
        ("Date", spec["date"]),
        ("PO Reference", spec.get("po", "")),
        ("Terms", spec.get("terms", "Net 30")),
    ]:
        d.text((W * scale - 320 * scale, y), f"{label}: {val}", fill=(60, 62, 80), font=f_med)
        y += 28 * scale

    y = m + 150 * scale
    d.text((m, y), "BILL TO:", fill=(80, 82, 100), font=f_med)
    y += 30 * scale
    for line in BILL_TO:
        d.text((m, y), line, fill=(40, 42, 60), font=f_med)
        y += 26 * scale

    y += 30 * scale
    d.line((m, y, W * scale - m, y), fill=(150, 150, 160), width=2 * scale)
    y += 20 * scale
    d.text((m, y), "DESCRIPTION", fill=(80, 82, 100), font=f_med)
    d.text((W * scale - 260 * scale, y), "AMOUNT", fill=(80, 82, 100), font=f_med)
    y += 36 * scale
    for desc, _qty, _unit, amount in spec["line_items"]:
        d.text((m, y), str(desc), fill=(30, 32, 50), font=f_med)
        d.text((W * scale - 260 * scale, y), _money(float(amount)), fill=(30, 32, 50), font=f_med)
        y += 34 * scale
    y += 20 * scale
    d.text((W * scale - 420 * scale, y), "TOTAL DUE", fill=(20, 22, 40), font=f_hdr)
    d.text((W * scale - 260 * scale, y), _money(float(spec["total"])), fill=(20, 22, 40), font=f_hdr)

    # Degrade: downscale, blur, add a slight rotation + noise to mimic a phone photo.
    img = img.resize((W, H), Image.BILINEAR)
    img = img.rotate(1.2, expand=False, fillcolor=(250, 250, 248))
    img = img.filter(ImageFilter.GaussianBlur(radius=2.4))
    img = img.resize((int(W * 0.7), int(H * 0.7)), Image.BILINEAR).resize((W, H), Image.BILINEAR)
    img.save(png_path, "PNG")
    print(f"  wrote {os.path.relpath(png_path, _REPO_ROOT)}")

    # Wrap the blurry image full-page into a PDF the intake pipeline can ingest.
    c = canvas.Canvas(pdf_path, pagesize=LETTER)
    c.setTitle(f"{spec['vendor']} - {spec['invoice_no']} (scan)")
    c.drawImage(png_path, 0.4 * inch, 0.4 * inch, width=PAGE_W - 0.8 * inch, height=PAGE_H - 0.8 * inch)
    c.showPage()
    c.save()
    print(f"  wrote {os.path.relpath(pdf_path, _REPO_ROOT)}")


# ---------------------------------------------------------------------------
# Scenario specs
# ---------------------------------------------------------------------------

DEFAULT_SAMPLE = {
    "vendor": "Helix Cloud Services",
    "vendor_addr": ["2200 Innovation Way", "Austin, TX 78758, USA", "billing@helixcloud.example"],
    "invoice_no": "INV-2026-0042",
    "date": "15 Jun 2026",
    "po": "PO-2026-2007",
    "terms": "Net 30",
    "currency": "USD",
    "line_items": [
        ("Managed cloud platform - Q2 subscription", 1, 3500.00, 3500.00),
        ("Premium 24x7 support", 1, 750.00, 750.00),
        ("Data egress & monitoring", 1, 500.00, 500.00),
    ],
    "total": 4750.00,
    "notes": "Covers managed cloud platform services for Q2 2026.",
}

SCENARIOS = {
    # ----- POSITIVE -----
    "01_direct_straight_through": {
        "vendor": "BluePeak Plastics",
        "vendor_addr": ["870 Polymer Drive", "Akron, OH 44301, USA", "ar@bluepeak.example"],
        "invoice_no": "INV-2026-0301",
        "date": "03 Jun 2026",
        "po": "PO-2026-1003",
        "terms": "Net 30",
        "line_items": [
            ("HDPE resin pellets, grade A (kg)", 500, 4.50, 2250.00),
            ("Colour masterbatch (kg)", 100, 10.00, 1000.00),
        ],
        "total": 3250.00,
        "notes": "Direct material against open PO. Within auto-approval limit.",
    },
    "02_indirect_straight_through": {
        "vendor": "BrightOffice Supplies Co",
        "vendor_addr": ["15 Commerce Park", "Columbus, OH 43215, USA", "billing@brightoffice.example"],
        "invoice_no": "INV-2026-0302",
        "date": "22 May 2026",
        "po": "PO-2026-2002",
        "terms": "Net 30",
        "line_items": [
            ("A4 multipurpose paper (box of 5 reams)", 20, 32.00, 640.00),
            ("High-capacity toner cartridge", 2, 140.00, 280.00),
        ],
        "total": 920.00,
        "notes": "Indirect / office supplies against open PO. Within auto-approval limit.",
    },
    # ----- HUMAN-REVIEW (high value), then approve -----
    "03_high_value_approval": {
        "vendor": "Meridian Steel & Alloys",
        "vendor_addr": ["4500 Foundry Road", "Gary, IN 46402, USA", "accounts@meridiansteel.example"],
        "invoice_no": "INV-2026-0303",
        "date": "12 Jun 2026",
        "po": "PO-2026-1010",
        "terms": "Net 45",
        "line_items": [
            ("Cold-rolled steel coil (metric ton)", 5, 2200.00, 11000.00),
            ("Cutting, slitting & prep", 1, 1500.00, 1500.00),
        ],
        "total": 12500.00,
        "notes": "High-value direct spend - exceeds auto-approval limit; needs human approval.",
    },
    # ----- NEGATIVE: duplicate invoice number -----
    "04_duplicate_invoice_number": {
        "vendor": "Cascade Components Inc",
        "vendor_addr": ["310 Industrial Blvd", "Portland, OR 97203, USA", "ar@cascadecomponents.example"],
        "invoice_no": "INV-2024-001",  # already booked (seeded in posted_invoices, config.MOCK_POSTED_INVOICES)
        "date": "10 Jun 2026",
        "po": "PO-2026-1002",
        "terms": "Net 30",
        "line_items": [
            ("Injection-molded housing, type C", 300, 18.00, 5400.00),
        ],
        "total": 5400.00,
        "notes": "Re-submission of an invoice number already on file.",
    },
    # ----- NEGATIVE: duplicate PO (already fully invoiced) -----
    "05_duplicate_po": {
        "vendor": "Globex Trading Co",
        "vendor_addr": ["77 Harbor Street", "Newark, NJ 07102, USA", "billing@globextrading.example"],
        "invoice_no": "INV-2026-0305",
        "date": "11 Jun 2026",
        "po": "PO-2026-0098",  # already invoiced (same vendor/PO/amount seeded in posted_invoices)
        "terms": "Net 30",
        "line_items": [
            ("Industrial solvent (55-gal drum)", 12, 500.00, 6000.00),
        ],
        "total": 6000.00,
        "notes": "Second invoice billed against a PO that is already fully invoiced.",
    },
    # ----- NEGATIVE: malformed / missing PO + non-standard invoice format -----
    "06_malformed_po_missing_fields": {
        "vendor": "Globex Trading Co",
        "vendor_addr": ["77 Harbor Street", "Newark, NJ 07102, USA", "billing@globextrading.example"],
        "invoice_no": "2026/887-A",  # non-standard format
        "date": "08 Jun 2026",
        "show_po": False,  # no PO reference printed anywhere
        "terms": "Due on receipt",
        "line_items": [
            ("Professional services - Q2 (no PO provided)", 1, 2750.00, 2750.00),
        ],
        "total": 2750.00,
        "notes": "No purchase order referenced - cannot be matched; requires review.",
    },
    # ----- NEGATIVE: business-rule violation (zero / invalid total) -----
    "08_business_rule_violation_zero_total": {
        "vendor": "Globex Trading Co",
        "vendor_addr": ["77 Harbor Street", "Newark, NJ 07102, USA", "billing@globextrading.example"],
        "invoice_no": "INV-2026-0308",
        "date": "09 Jun 2026",
        "po": "PO-2026-2011",
        "terms": "Net 30",
        "line_items": [
            ("Catering - annual offsite (amount pending)", 1, 0.00, 0.00),
        ],
        "total": 0.00,
        "notes": "Total is zero / not finalized - fails the amount-validity rule.",
    },
}

# Blurry scan reuses the Helix Cloud Services data but as a low-quality image.
BLURRY = {
    "vendor": "Helix Cloud Services",
    "invoice_no": "INV-2026-0307",
    "date": "14 Jun 2026",
    "po": "PO-2026-2007",
    "terms": "Net 30",
    "line_items": [
        ("Managed cloud platform - Q2", "", "", 4000.00),
        ("Premium support", "", "", 750.00),
    ],
    "total": 4750.00,
}


def main() -> None:
    os.makedirs(INVOICE_DIR, exist_ok=True)
    print("Generating invoice fixtures…")

    # Default sample invoice PDF (handy to email to the demo mailbox for a run).
    write_pdf(SAMPLE_PDF, DEFAULT_SAMPLE)

    for name, spec in SCENARIOS.items():
        write_pdf(os.path.join(INVOICE_DIR, f"{name}.pdf"), spec)

    write_blurry(
        os.path.join(INVOICE_DIR, "07_blurry_scan.png"),
        os.path.join(INVOICE_DIR, "07_blurry_scan.pdf"),
        BLURRY,
    )
    print("Done.")


if __name__ == "__main__":
    main()
