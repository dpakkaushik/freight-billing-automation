"""Generate Pallia Trans invoice PDF directly using ReportLab.

Replaces the Excel fill + LibreOffice conversion pipeline so the
output format exactly matches the agreed Swaraj Gujarat layout.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from loguru import logger
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.amount_words import amount_in_words
from app.services.excel_filler import _to_decimal, build_rows

# ── Static letterhead ─────────────────────────────────────────────────────────

COMPANY_NAME  = "PALLIA TRANS LOGISTICS PRIVATE LIMITED"
COMPANY_ADDR  = (
    "Plot No.1 D, Shivshakti Co.-Op.HSG Society, PCNTDA, "
    "Moshi, Pune, Maharashtra, 412105"
)
COMPANY_PAN   = "AAFCM8426Q"
COMPANY_GSTIN = "27AAFCM8426Q1ZQ"

CUSTOMER_NAME  = "Mahindra Logistics Limited"
CUSTOMER_ADDR1 = "Address - BUNGALOW NO. 12, N.C. MARC,"
CUSTOMER_ADDR2 = "AUTO SECTOR, OPP. POLICE STADIUM, AHMEDABAD – 380004"
CUSTOMER_STATE = "State : GUJARAT     State Code : 24"
CUSTOMER_PAN   = "AAFCM2530H"
CUSTOMER_GSTIN = "24AAFCM2530H1ZU"
PLACE_OF_SUPPLY   = "Rajkot, Gujarat"
NATURE_OF_SERVICE = "Transportation of Goods by Road (GTA)"
HSN = "996511"

MAX_ROWS = 12


# ── Style helpers ─────────────────────────────────────────────────────────────

def _make_styles() -> dict[str, ParagraphStyle]:
    return {
        "h1":    ParagraphStyle("h1",    fontName="Helvetica-Bold", fontSize=11, alignment=TA_CENTER, leading=14),
        "h2":    ParagraphStyle("h2",    fontName="Helvetica",      fontSize=8,  alignment=TA_CENTER, leading=11),
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=10, alignment=TA_CENTER, leading=13),
        "norm":  ParagraphStyle("norm",  fontName="Helvetica",      fontSize=8,  alignment=TA_LEFT,   leading=11),
        "sm":    ParagraphStyle("sm",    fontName="Helvetica",      fontSize=7,  alignment=TA_LEFT,   leading=9),
        "sm_b":  ParagraphStyle("sm_b",  fontName="Helvetica-Bold", fontSize=7,  alignment=TA_LEFT,   leading=9),
        "sm_c":  ParagraphStyle("sm_c",  fontName="Helvetica",      fontSize=7,  alignment=TA_CENTER, leading=9),
        "sm_bc": ParagraphStyle("sm_bc", fontName="Helvetica-Bold", fontSize=7,  alignment=TA_CENTER, leading=9),
        "sm_r":  ParagraphStyle("sm_r",  fontName="Helvetica",      fontSize=7,  alignment=TA_RIGHT,  leading=9),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def _fmt(amount: Decimal) -> str:
    return f"{amount:,.2f}"


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_invoice_pdf(
    suffix: str,
    invoice_date: date,
    lrs: list[dict[str, Any]],
    output_path: Path,
) -> Path:
    """Build invoice PDF and write to output_path. Returns output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    PAGE_W, _ = A4
    MARGIN = 10 * mm
    CW = PAGE_W - 2 * MARGIN  # usable content width

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=10 * mm,
    )

    st = _make_styles()
    story: list = []

    # ── Company header ────────────────────────────────────────────────────────
    story.append(_p(COMPANY_NAME, st["h1"]))
    story.append(_p(COMPANY_ADDR, st["h2"]))
    story.append(Spacer(1, 2 * mm))
    story.append(_p("Original for Customer / Duplicate for Supplier", st["sm"]))
    story.append(Spacer(1, 2 * mm))
    story.append(_p("<b>TAX INVOICE</b>", st["title"]))
    story.append(Spacer(1, 2 * mm))

    # ── To / Invoice meta (2-column) ─────────────────────────────────────────
    invoice_no_str   = f"PTLM-2627SWM-{suffix}"
    invoice_date_str = invoice_date.strftime("%d/%m/%Y")

    left_txt = (
        f"<b>To,</b><br/>"
        f"<b>{CUSTOMER_NAME}</b><br/>"
        f"{CUSTOMER_ADDR1}<br/>"
        f"{CUSTOMER_ADDR2}<br/>"
        f"{CUSTOMER_STATE}<br/><br/>"
        f"<b>Customer PAN No:</b> {CUSTOMER_PAN}<br/>"
        f"<b>Customer GSTIN:</b> {CUSTOMER_GSTIN}<br/>"
        f"Place of Supply (City, State): {PLACE_OF_SUPPLY}<br/>"
        f"Address of Delivery (City, State): {PLACE_OF_SUPPLY}<br/>"
        f"<b>GST payable under reverse charge: NO</b>"
    )
    right_txt = (
        f"<b>Invoice No :</b> {invoice_no_str}<br/>"
        f"<b>Invoice Date :</b> {invoice_date_str}<br/>"
        f"<b>PAN No :</b> {COMPANY_PAN}<br/>"
        f"<b>GSTIN:</b> {COMPANY_GSTIN}<br/>"
        f"<b>Nature of Services :</b> {NATURE_OF_SERVICE}<br/>"
        f"<b>HSN :</b> {HSN}"
    )

    meta = Table(
        [[_p(left_txt, st["sm"]), _p(right_txt, st["sm"])]],
        colWidths=[CW * 0.55, CW * 0.45],
    )
    meta.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("BOX",           (0, 0), (-1, -1), 0.5, colors.black),
        ("LINEBEFORE",    (1, 0), (1,  0),  0.5, colors.black),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta)
    story.append(Spacer(1, 2 * mm))

    # ── Data table ────────────────────────────────────────────────────────────
    rows = build_rows(lrs)

    # Column widths in mm — last column gets the remainder
    _cw_mm = [8, 27, 22, 20, 20, 23, 20, 12, 18]
    last_col = CW / mm - sum(_cw_mm)
    col_widths = [w * mm for w in _cw_mm] + [last_col * mm]

    def th(text: str) -> Paragraph:
        return _p(f"<b>{text}</b>", st["sm_c"])

    headers = [
        th("S.No."), th("Invoice No."), th("LR No."), th("LR Date"),
        th("Vehicle No."), th("Place of\nDestination"), th("Delivery\nDate"),
        th("Qty."), th("Rate Per\nTractor"), th("Total\nAmount"),
    ]

    table_data: list = [headers]
    total_amount = Decimal("0")

    for i in range(MAX_ROWS):
        if i < len(rows):
            r = rows[i]
            amt = _to_decimal(r.get("total_amount"))
            if amt > 0:
                total_amount += amt
                amt_str = _fmt(amt)
            else:
                amt_str = ""
            row = [
                _p(str(i + 1), st["sm_c"]),
                _p(r.get("delivery_order_no") or "", st["sm_c"]),
                _p(r.get("gcn_no") or "",            st["sm_c"]),
                _p(r.get("gcn_date") or "",           st["sm_c"]),
                _p(r.get("vehicle_no") or "",         st["sm_c"]),
                _p(r.get("destination") or "",        st["sm_c"]),
                _p(r.get("delivery_date") or "",      st["sm_c"]),
                _p(r.get("qty") or "",                st["sm_c"]),
                _p("Fixed", st["sm_c"]) if i == 0 else "",
                _p(amt_str,                           st["sm_r"]),
            ]
        else:
            row = [_p(str(i + 1), st["sm_c"])] + [""] * 9
        table_data.append(row)

    igst        = total_amount * Decimal("0.18")
    grand_total = total_amount + igst
    words       = amount_in_words(grand_total)

    n_data = MAX_ROWS + 1  # header + 12 data rows

    table_data.append([_p("<b>Total</b>", st["sm_b"]),                    "", "", "", "", "", "", "", "", _p(_fmt(total_amount), st["sm_r"])])
    table_data.append([_p("IGST (Integrated Tax) 18 %", st["sm"]),        "", "", "", "", "", "", "", "", _p(_fmt(igst),         st["sm_r"])])
    table_data.append([_p(f"<i>{words}</i>", st["sm"]),                   "", "", "", "", "", "", "", "", _p(_fmt(grand_total),  st["sm_r"])])

    data_tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    data_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0),      (-1, 0),          colors.Color(0.88, 0.88, 0.88)),
        ("FONTNAME",      (0, 0),      (-1, 0),          "Helvetica-Bold"),
        ("GRID",          (0, 0),      (-1, n_data - 1), 0.3, colors.black),
        ("BOX",           (0, 0),      (-1, n_data - 1), 0.5, colors.black),
        # Totals section
        ("BOX",           (0, n_data), (-1, n_data + 2), 0.5, colors.black),
        ("LINEABOVE",     (0, n_data), (-1, n_data),     0.5, colors.black),
        ("LINEBEFORE",    (-1, n_data),(-1, n_data + 2), 0.5, colors.black),
        ("LINEBELOW",     (0, n_data), (-1, n_data),     0.3, colors.black),
        ("LINEBELOW",     (0, n_data + 1), (-1, n_data + 1), 0.3, colors.black),
        ("SPAN",          (0, n_data),     (8, n_data)),
        ("SPAN",          (0, n_data + 1), (8, n_data + 1)),
        ("SPAN",          (0, n_data + 2), (8, n_data + 2)),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 2),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
        ("TOPPADDING",    (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("ROWHEIGHT",     (0, 1), (-1, n_data - 1), 10),
    ]))
    story.append(data_tbl)
    story.append(Spacer(1, 4 * mm))

    # ── Terms & Conditions ────────────────────────────────────────────────────
    story.append(_p("<b>Terms and Conditions:</b>", st["sm"]))
    story.append(Spacer(1, 1 * mm))
    story.append(_p(
        "1. All Payments by cheques / drafts in favor of “Pallia Trans Logistics Private Limited” "
        "should be crossed to payees account.<br/>"
        "2. No Claims and / or discrepancy if any shall be considered unless brought to the notice of the "
        "company in writing within 3 days of the receipt of the bill.<br/>"
        "3. Dispute if any shall be subjected to the jurisdiction of Mumbai Courts only.",
        st["sm"],
    ))
    story.append(Spacer(1, 3 * mm))

    # ── Declaration ───────────────────────────────────────────────────────────
    story.append(_p("<b>Declaration</b>", st["sm"]))
    story.append(Spacer(1, 1 * mm))
    story.append(_p(
        "<i>1. We hereby declare that though our aggregate turnover in any preceding financial year from "
        "2017-18 onwards is more than the aggregate turnover notified under sub-rule (4) of rule 48, we are "
        "not required to prepare an invoice in terms of the provisions of the said sub-rule</i><br/>"
        "2. We have taken registration under the CGST Act 2017 and have exercised the option to pay tax on "
        "services of Goods Transport Agency in relation to transport of goods supplied by us during the "
        "Financial year 2026-2027 under forward charge\"",
        st["sm"],
    ))
    story.append(Spacer(1, 8 * mm))

    # ── Signature ─────────────────────────────────────────────────────────────
    story.append(_p("<b>For Pallia Trans Logistics Private Limited</b>", st["sm"]))
    story.append(Spacer(1, 20 * mm))   # 20 mm gives room for the DSC stamp between the two lines
    story.append(_p("<b>Authorised Signatory</b>", st["sm_b"]))
    story.append(Spacer(1, 3 * mm))
    story.append(_p(
        "Head Office address : Unit 701-705, 07th Floor, Good Earth Business Bay-I, Sector 58, "
        "Gurugram, Haryana 122098<br/>"
        "Regd office address : Unit 701-705, 07th Floor, Good Earth Business Bay-I, Sector 58, "
        "Gurugram, Haryana 122098",
        st["sm"],
    ))

    doc.build(story)
    logger.info("Generated invoice PDF: {} ({} rows)", output_path, len(rows))
    return output_path
