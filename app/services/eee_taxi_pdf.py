"""Generate EEE-Taxi GST tax invoice PDF using ReportLab."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from loguru import logger
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.amount_words import amount_in_words
from app.services.eee_taxi_clients import ClientRecord

_CO_NAME   = "EEE-TAXI MOBILITY SOLUTIONS PRIVATE LIMITED"
_CO_ADDR1  = "Flat No 2486, Sector D, Pocket 2, Church Mall Road"
_CO_ADDR2  = "Sheetla Balloon Decoration Shop"
_CO_ADDR3  = "Vasant Kunj, South Delhi - 110070"
_CO_UDYAM  = "UDYAM REGISTRATION NUMBER :- UDYAM-HR-05-0019335"
_CO_GSTIN  = "07AANCA3858Q1ZU"
_CO_STATE  = "Delhi"
_CO_CODE   = "07"
_CO_EMAIL  = "accounts@eeetaxi.com"
_BANK_NAME = "HDFC BANK"
_BANK_ACNO = "50200012625970"
_BANK_IFSC = "HDFC0009141"
_BANK_BR   = "MILLENIUM TOWER, GURGAON -122002"
_HSN       = "996601"

LOGO_PATH = Path(__file__).parent.parent / "static" / "assets" / "eee_taxi_logo.png"

_FONT_NORMAL = "Helvetica"
_FONT_BOLD   = "Helvetica-Bold"
try:
    pdfmetrics.registerFont(TTFont("Arial",      r"C:\Windows\Fonts\arial.ttf"))
    pdfmetrics.registerFont(TTFont("Arial-Bold", r"C:\Windows\Fonts\arialbd.ttf"))
    _FONT_NORMAL = "Arial"
    _FONT_BOLD   = "Arial-Bold"
except Exception:
    pass


@dataclass
class InvoiceContext:
    invoice_no: str
    invoice_date: date
    trip_date: date
    is_local: bool
    buyer: ClientRecord
    car_no: str
    route_no: str
    guest_name: str
    pickup_location: str
    drop_location: str
    trip_fare: Decimal
    parking: Decimal
    tax_base: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total_amount: Decimal
    # Rental breakdown (only populated for rental bookings)
    is_rental: bool = False
    rental_base_label: str = ""        # e.g. "8/80"
    rental_base_fare: Decimal = Decimal("0")
    extra_hrs: int = 0
    extra_hrs_charge: Decimal = Decimal("0")
    extra_km_count: int = 0
    extra_km_charge_val: Decimal = Decimal("0")
    night_charge_val: Decimal = Decimal("0")


def compute_tax(tax_base: Decimal, is_local: bool) -> tuple[Decimal, Decimal, Decimal]:
    """Returns (cgst, sgst, igst)."""
    if is_local:
        h = (tax_base * Decimal("0.09")).quantize(Decimal("0.01"))
        return h, h, Decimal("0")
    igst = (tax_base * Decimal("0.18")).quantize(Decimal("0.01"))
    return Decimal("0"), Decimal("0"), igst


def _d(d: date) -> str:
    return f"{d.day}-{d.strftime('%b')}-{str(d.year)[2:]}"


def _amt(v: Decimal) -> str:
    return f"{v:,.2f}"


def _styles() -> dict[str, ParagraphStyle]:
    def ps(name, font=None, size=7.5, align=TA_LEFT, bold=False, leading=10):
        fn = _FONT_BOLD if bold else (font if font is not None else _FONT_NORMAL)
        return ParagraphStyle(name, fontName=fn, fontSize=size, alignment=align, leading=leading)
    return {
        "title": ps("title", size=10, align=TA_CENTER, bold=True, leading=14),
        "co":    ps("co", size=8.5, bold=True),
        "sm":    ps("sm"),
        "smb":   ps("smb", bold=True),
        "smc":   ps("smc", align=TA_CENTER),
        "smbc":  ps("smbc", bold=True, align=TA_CENTER),
        "smr":   ps("smr", align=TA_RIGHT),
        "smbr":  ps("smbr", bold=True, align=TA_RIGHT),
        "xs":    ps("xs", size=7, leading=9),
        "xsb":   ps("xsb", size=7, bold=True, leading=9),
        "xsc":   ps("xsc", size=7, align=TA_CENTER, leading=9),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def generate_eee_taxi_invoice_pdf(
    ctx: InvoiceContext, output_path: Path
) -> tuple[Path, None]:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    PAGE_W, _ = A4
    M = 10 * mm
    CW = PAGE_W - 2 * M

    doc = SimpleDocTemplate(
        str(output_path), pagesize=A4,
        leftMargin=M, rightMargin=M, topMargin=8*mm, bottomMargin=8*mm,
    )
    st = _styles()
    story: list = []

    story.append(Spacer(1, 5*mm))
    story.append(_p("<b>Tax Invoice</b>", st["title"]))
    story.append(Spacer(1, 3*mm))

    # ── Header: company (left top) + buyer (left bottom) + meta (right, full height) ───
    HPAD     = 3
    lw       = CW * 0.55
    rw       = CW * 0.45
    inner_lw = lw - HPAD * 2

    logo_img = None
    if LOGO_PATH.exists():
        try:
            logo_img = Image(str(LOGO_PATH), width=18*mm, height=14*mm)
        except Exception:
            pass

    co_w = inner_lw - (20*mm if logo_img else 0)
    co_lines = [
        [_p(f"<b>{_CO_NAME}</b>", st["co"])],
        [_p(_CO_ADDR1, st["xs"])],
        [_p(_CO_ADDR2, st["xs"])],
        [_p(_CO_ADDR3, st["xs"])],
        [_p(_CO_UDYAM, st["xs"])],
        [_p(f"GSTIN/UIN: {_CO_GSTIN}", st["xs"])],
        [_p(f"State Name : {_CO_STATE}, Code : {_CO_CODE}", st["xs"])],
        [_p(f"E-Mail : {_CO_EMAIL}", st["xs"])],
    ]
    co_tbl = Table(co_lines, colWidths=[co_w])
    co_tbl.setStyle(TableStyle([
        ("LEFTPADDING",(0,0),(-1,-1),2), ("RIGHTPADDING",(0,0),(-1,-1),2),
        ("TOPPADDING",(0,0),(-1,-1),1),  ("BOTTOMPADDING",(0,0),(-1,-1),1),
    ]))

    if logo_img:
        co_section = Table([[logo_img, co_tbl]], colWidths=[20*mm, co_w])
        co_section.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP")]))
    else:
        co_section = co_tbl

    inv_d  = _d(ctx.invoice_date)
    trip_d = _d(ctx.trip_date)
    ref    = f"{ctx.invoice_no} dt. {inv_d}"
    meta_rows = [
        [_p("Invoice No.", st["xsb"]),         _p(ctx.invoice_no, st["xs"]), _p("Dated", st["xsb"]),          _p(inv_d, st["xs"])],
        [_p("Delivery Note", st["xs"]),         _p("", st["xs"]),             _p("", st["xs"]),                 _p("", st["xs"])],
        [_p("Reference No. &amp; Date.", st["xsb"]), _p(ref, st["xs"]),      _p("Other References", st["xs"]), _p("", st["xs"])],
        [_p("Buyer's Order No.", st["xsb"]),    _p(ctx.route_no, st["xs"]),  _p("Dated", st["xsb"]),          _p(trip_d, st["xs"])],
        [_p("Dispatch Doc No.", st["xs"]),       _p("", st["xs"]),             _p("Delivery Note Date", st["xs"]), _p("", st["xs"])],
        [_p("Dispatched through", st["xs"]),     _p("", st["xs"]),             _p("Destination", st["xs"]),     _p("", st["xs"])],
        [_p("Bill of Lading/LR-RR No.", st["xsb"]), _p(f"dt. {trip_d}", st["xs"]), _p("Motor Vehicle No.", st["xsb"]), _p(ctx.car_no, st["xs"])],
    ]
    meta_tbl = Table(meta_rows, colWidths=[rw*0.32, rw*0.24, rw*0.22, rw*0.22])
    meta_tbl.setStyle(TableStyle([
        ("BOX",(0,0),(-1,-1),0.5,colors.black), ("INNERGRID",(0,0),(-1,-1),0.3,colors.black),
        ("LEFTPADDING",(0,0),(-1,-1),3), ("RIGHTPADDING",(0,0),(-1,-1),3),
        ("TOPPADDING",(0,0),(-1,-1),2),  ("BOTTOMPADDING",(0,0),(-1,-1),2),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
    ]))

    b = ctx.buyer
    bw0 = inner_lw * 0.55
    bw1 = inner_lw * 0.45
    buyer_tbl = Table([
        [_p("<b>Buyer (Bill to)</b>", st["smb"]), ""],
        [_p(f"<b>{b.entity_name}</b>", st["smb"]), ""],
        [_p(b.address, st["xs"]), ""],
        [_p(f"GSTIN/UIN : {b.gstin}", st["xs"]),
         _p(f"State Name : {b.state_name}, Code : {b.state_code}", st["xs"])],
    ], colWidths=[bw0, bw1])
    buyer_tbl.setStyle(TableStyle([
        ("SPAN",(0,0),(1,0)), ("SPAN",(0,1),(1,1)), ("SPAN",(0,2),(1,2)),
        ("LEFTPADDING",(0,0),(-1,-1),2), ("RIGHTPADDING",(0,0),(-1,-1),2),
        ("TOPPADDING",(0,0),(-1,-1),2),  ("BOTTOMPADDING",(0,0),(-1,-1),2),
    ]))

    hdr = Table([[co_section, meta_tbl], [buyer_tbl, ""]], colWidths=[lw, rw])
    hdr.setStyle(TableStyle([
        ("BOX",(0,0),(-1,-1),0.5,colors.black),
        ("SPAN",(1,0),(1,1)),
        ("LINEBELOW",(0,0),(0,0),0.5,colors.black),
        ("LINEBEFORE",(1,0),(1,-1),0.5,colors.black),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(0,-1),HPAD), ("RIGHTPADDING",(0,0),(0,-1),HPAD),
        ("TOPPADDING",(0,0),(-1,-1),3), ("BOTTOMPADDING",(0,0),(-1,-1),3),
        ("LEFTPADDING",(1,0),(1,-1),0), ("RIGHTPADDING",(1,0),(1,-1),0),
        ("TOPPADDING",(1,0),(1,-1),0), ("BOTTOMPADDING",(1,0),(1,-1),0),
    ]))
    story.append(hdr)

    # ── Items ─────────────────────────────────────────────────────────────────
    rental_lbl  = "CAR RENTAL LOCAL-18%" if ctx.is_local else "Car Rental Interstate-18%"
    parking_lbl = "Toll and Parking" if ctx.is_local else "Toll &amp; Parking"

    base_lines = (
        f"{rental_lbl}<br/>"
        f"Guest Name:-{ctx.guest_name}<br/>"
        f"From:-{ctx.pickup_location}<br/>"
        f"To:-{ctx.drop_location}"
    )
    if ctx.is_rental:
        ekc = "0" if ctx.extra_km_charge_val == 0 else f"{ctx.extra_km_charge_val:.2f}"
        night_line = (
            f"<br/>Night Charge (23:00-05:00= {ctx.night_charge_val:.2f})"
            if ctx.night_charge_val > 0 else ""
        )
        breakdown = (
            f"<br/>Rental ({ctx.rental_base_label}= {ctx.rental_base_fare:.2f})<br/>"
            f"Extra Hrs ({ctx.extra_hrs}*180= {ctx.extra_hrs_charge:.2f})<br/>"
            f"Extra Kms ({ctx.extra_km_count}*15.50= {ekc})"
            f"{night_line}<br/>"
            "G TO G KM (MAX=20 KM)) &amp; HRS (1 HRS) INCLUDED"
        )
        particulars = base_lines + breakdown
    else:
        particulars = base_lines
    cw = [8*mm, CW-8*mm-20*mm-14*mm-16*mm-22*mm, 20*mm, 14*mm, 16*mm, 22*mm]

    def th(t: str) -> Paragraph:
        return _p(f"<b>{t}</b>", st["smbc"])

    item_rows: list = [
        [th("Sl\nNo."), th("Particulars"), th("HSN/SAC"), th("GST\nRate"), th("Quantity"), th("Amount")],
        [_p("1", st["smc"]), _p(particulars, st["xs"]), _p(_HSN, st["smc"]), _p("18 %", st["smc"]), _p("", st["smc"]), _p(_amt(ctx.trip_fare), st["smr"])],
        [_p("2", st["smc"]), _p(parking_lbl, st["sm"]), _p(_HSN, st["smc"]), _p("18 %", st["smc"]), _p("", st["smc"]), _p(_amt(ctx.parking), st["smr"])],
    ]
    if ctx.is_local:
        item_rows.append(["", _p("OUTPUT CGST @ 9%", st["sm"]), "", "", "", _p(_amt(ctx.cgst), st["smr"])])
        item_rows.append(["", _p("OUTPUT SGST @ 9%", st["sm"]), "", "", "", _p(_amt(ctx.sgst), st["smr"])])
    else:
        item_rows.append(["", _p("Output IGST @18%", st["sm"]), "", "", "", _p(_amt(ctx.igst), st["smr"])])

    for _ in range(8):
        item_rows.append(["", "", "", "", "", ""])

    n = len(item_rows)
    item_rows.append(["", "", "", "", _p("Total", st["smb"]), _p(f"₹ {_amt(ctx.total_amount)}", st["smbr"])])

    sc = [
        ("BOX",(0,0),(-1,-1),0.5,colors.black),
        ("LINEBELOW",(0,0),(-1,0),0.5,colors.black),
        ("BACKGROUND",(0,0),(-1,0),colors.Color(0.88,0.88,0.88)),
        ("LINEABOVE",(0,n),(-1,n),0.5,colors.black),
        ("LINEBEFORE",(-1,n),(-1,n),0.5,colors.black),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),3), ("RIGHTPADDING",(0,0),(-1,-1),3),
        ("TOPPADDING",(0,0),(-1,-1),2),  ("BOTTOMPADDING",(0,0),(-1,-1),2),
    ]
    for col in [1, 2, 3, 4, 5]:
        sc.append(("LINEBEFORE", (col, 0), (col, n), 0.3, colors.black))
    items_tbl = Table(item_rows, colWidths=cw)
    items_tbl.setStyle(TableStyle(sc))
    story.append(items_tbl)

    # ── Footer ────────────────────────────────────────────────────────────────
    words = amount_in_words(ctx.total_amount).replace("Rupees ", "INR ", 1)
    arow = Table(
        [[_p("Amount Chargeable (in words)", st["smb"]), _p("E. &amp; O.E", st["smr"])]],
        colWidths=[CW*0.7, CW*0.3],
    )
    arow.setStyle(TableStyle([
        ("BOX",(0,0),(-1,-1),0.5,colors.black),
        ("LEFTPADDING",(0,0),(-1,-1),4), ("RIGHTPADDING",(0,0),(-1,-1),4),
        ("TOPPADDING",(0,0),(-1,-1),2),  ("BOTTOMPADDING",(0,0),(-1,-1),2),
    ]))
    story.append(arow)
    story.append(_p(f"<b>{words}</b>", st["smb"]))
    story.append(Spacer(1,1*mm))
    story.append(_p("<b>Remarks:</b>", st["smb"]))
    story.append(_p(ctx.route_no, st["sm"]))
    story.append(Spacer(1,2*mm))

    bank = (
        f"Company's Bank Details<br/>"
        f"Bank Name : {_BANK_NAME}<br/>"
        f"A/c No. : {_BANK_ACNO}<br/>"
        f"Branch &amp; IFS Code : {_BANK_BR} &amp; {_BANK_IFSC}"
    )

    # Right cell: company name bold at top, blank sig space, Authorised Signatory at bottom.
    right_inner = Table(
        [
            [_p(f"<b>for {_CO_NAME}</b>", st["xs"])],
            [Spacer(1, 32)],
            [_p("Authorised Signatory", st["xs"])],
        ],
        colWidths=[CW * 0.4 - 8],
    )
    right_inner.setStyle(TableStyle([
        ("LEFTPADDING",  (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING",   (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 1),
    ]))

    footer_tbl = Table(
        [[_p(bank, st["xs"]), right_inner]],
        colWidths=[CW*0.6, CW*0.4],
    )
    footer_tbl.setStyle(TableStyle([
        ("BOX",(0,0),(-1,-1),0.5,colors.black),
        ("LINEBEFORE",(1,0),(1,-1),0.5,colors.black),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),4), ("RIGHTPADDING",(0,0),(-1,-1),4),
        ("TOPPADDING",(0,0),(-1,-1),3),  ("BOTTOMPADDING",(0,0),(-1,-1),3),
    ]))
    story.append(footer_tbl)
    story.append(Spacer(1,1*mm))
    story.append(_p("This is a Computer Generated Invoice", st["xsc"]))

    doc.build(story)
    logger.info("Generated EEE-Taxi PDF: {}", output_path)
    return output_path, None
