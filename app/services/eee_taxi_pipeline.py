"""EEE-Taxi invoice generation + DSC signing, driven one invoice at a time.

Nothing here runs in a background thread. On a serverless host the function
instance is suspended as soon as the HTTP response is sent, so a thread
started during a request stops getting CPU and the batch silently stalls.
The browser therefore drives the work, one short request per step:

  1. POST /batch                      -> create the batch + one row per invoice
                                         (status 'pending'), store the inputs.
  2. POST /batch/{id}/generate-next   -> generate ONE invoice PDF.
       sign_mode 'dummy' : stamp a placeholder here        -> 'done'
       sign_mode 'usb'   : store the unsigned PDF + box    -> 'awaiting_signature'
  3. For each awaiting invoice the browser has PalliaSignHelper.exe sign it
     with the USB token and uploads the result, so the PIN and the token stay
     on the user's PC.

Each generate-next call rebuilds the row it needs from the CSV stored on the
batch, because consecutive requests may land on different instances and
nothing can be kept in memory between them.

PDF bytes live in the database for the same reason: a serverless disk is
per-instance and ephemeral. File paths are only a fallback for local runs.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EeeTaxiBatch, EeeTaxiBatchStatus, EeeTaxiInvoice, EeeTaxiInvoiceStatus
from app.services.eee_taxi_clients import UnknownClientError, is_local, lookup_client
from app.services.eee_taxi_csv import EeeTaxiRow, financial_year, format_invoice_no, parse_eee_taxi_csv
from app.services.eee_taxi_fare_check import apply_card_fares
from app.services.eee_taxi_pdf import InvoiceContext, compute_tax, generate_eee_taxi_invoice_pdf
from app.services.eee_taxi_rates import DEFAULT_RATE_CARD, RateCard, _from_json
from app.services.eee_taxi_rental_calc import (
    apply_fare_overrides,
    calculate_rental_fare,
    parse_calc_csv,
)
from app.services.eee_taxi_signer import _find_signature_box, sign_eee_taxi_pdf_dummy


# ── PDF byte helpers ──────────────────────────────────────────────────────────

def unsigned_pdf_bytes(inv: EeeTaxiInvoice) -> bytes | None:
    """Unsigned PDF from the DB, falling back to the file on disk."""
    if inv.pdf_data:
        return bytes(inv.pdf_data)
    if inv.pdf_path and Path(inv.pdf_path).exists():
        return Path(inv.pdf_path).read_bytes()
    return None


def signed_pdf_bytes(inv: EeeTaxiInvoice) -> bytes | None:
    """Signed PDF from the DB, falling back to the file on disk."""
    if inv.signed_pdf_data:
        return bytes(inv.signed_pdf_data)
    if inv.signed_pdf_path and Path(inv.signed_pdf_path).exists():
        return Path(inv.signed_pdf_path).read_bytes()
    return None


def has_signed_pdf(inv: EeeTaxiInvoice) -> bool:
    return bool(inv.signed_pdf_data) or bool(
        inv.signed_pdf_path and Path(inv.signed_pdf_path).exists()
    )


def signed_filename(inv: EeeTaxiInvoice) -> str:
    if inv.signed_pdf_path:
        return Path(inv.signed_pdf_path).name
    if inv.pdf_path:
        return f"{Path(inv.pdf_path).stem}_signed.pdf"
    safe = (inv.invoice_no or inv.id).replace("/", "-")
    return f"{safe}_signed.pdf"


def finalize_batch_status(batch: EeeTaxiBatch, db: Session) -> EeeTaxiBatchStatus:
    """Derive the batch status from its invoices and persist it.

    Called when the generation thread finishes and after every signed-PDF
    upload, so the batch flips to completed/partial as soon as the browser
    has signed the last invoice.
    """
    invoices = db.query(EeeTaxiInvoice).filter(EeeTaxiInvoice.batch_id == batch.id).all()
    statuses = [inv.status for inv in invoices]

    if any(s in (EeeTaxiInvoiceStatus.PENDING, EeeTaxiInvoiceStatus.GENERATING) for s in statuses):
        status = EeeTaxiBatchStatus.PROCESSING
    elif any(s == EeeTaxiInvoiceStatus.AWAITING_SIGNATURE for s in statuses):
        status = EeeTaxiBatchStatus.AWAITING_SIGNATURE
    else:
        done   = sum(1 for s in statuses if s == EeeTaxiInvoiceStatus.DONE)
        failed = sum(1 for s in statuses if s == EeeTaxiInvoiceStatus.FAILED)
        if failed == 0:
            status = EeeTaxiBatchStatus.COMPLETED
        elif done == 0:
            status = EeeTaxiBatchStatus.FAILED
        else:
            status = EeeTaxiBatchStatus.PARTIAL

    batch.status = status
    db.commit()
    return status


# ── Per-invoice generation ────────────────────────────────────────────────────

def batch_rates(batch: EeeTaxiBatch) -> RateCard:
    """The rate card snapshot taken when the batch started."""
    return _from_json(batch.rates_snapshot) if batch.rates_snapshot else DEFAULT_RATE_CARD


def batch_rows(batch: EeeTaxiBatch, rates: RateCard) -> list[EeeTaxiRow]:
    """Rebuild the parsed, fare-adjusted rows for *batch* from its stored CSV.

    Deterministic: same CSV, same stored overrides and same rate-card snapshot
    give the same rows every time, so it does not matter which request or which
    instance asks for them.
    """
    if not batch.csv_data:
        raise RuntimeError("Batch has no stored CSV; it cannot be generated.")

    _, rows = parse_eee_taxi_csv(bytes(batch.csv_data), rates)

    card_rows = set(batch.card_fare_rows or [])
    if card_rows:
        rows = apply_card_fares(rows, card_rows, rates)

    overrides: dict = {}
    if batch.calc_csv_data:
        overrides = parse_calc_csv(bytes(batch.calc_csv_data))
    rows = apply_fare_overrides(rows, overrides, rates)
    return rows


def build_invoice_pdf(
    row: EeeTaxiRow,
    invoice_no: str,
    invoice_date: date,
    rates: RateCard,
    output_dir: Path,
) -> tuple[Path, tuple[float, float, float, float]]:
    """Render one invoice PDF and return its path and signature box."""
    client_gstin = row.client_gstin.strip().upper()
    try:
        buyer = lookup_client(client_gstin)
    except UnknownClientError as exc:
        raise RuntimeError(str(exc)) from exc

    local = is_local(client_gstin)
    taxable = row.tax_base + row.parking   # GST applies to trip fare + toll
    cgst, sgst, igst = compute_tax(taxable, local)
    total = row.total_amount
    if total == Decimal("0"):
        total = taxable + cgst + sgst + igst

    # Build rental breakdown for invoice particulars
    rental_kwargs: dict = {}
    if row.booking_type == "rental":
        fr = calculate_rental_fare(row, rates)
        rental_kwargs = dict(
            is_rental=True,
            rental_base_label=f"{fr.base_hours}/{fr.base_kms}",
            rental_base_fare=Decimal(str(fr.effective_package)),
            extra_hrs=fr.extra_time_hours,
            extra_hrs_charge=fr.extra_time_charge,
            extra_km_count=fr.extra_km,
            extra_km_charge_val=fr.extra_km_charge,
            night_charge_val=fr.night_charge,
            extra_hr_rate=rates.extra_hour_rate,
            extra_km_rate=rates.extra_km_rate,
            night_window_label=rates.night_label,
        )

    ctx = InvoiceContext(
        invoice_no=invoice_no,
        invoice_date=invoice_date,
        trip_date=row.trip_date,
        is_local=local,
        buyer=buyer,
        car_no=row.car_no,
        route_no=row.route_no,
        guest_name=row.guest_name,
        pickup_location=row.pickup_location,
        drop_location=row.drop_location,
        trip_fare=row.trip_fare,
        parking=row.parking,
        tax_base=row.tax_base,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        total_amount=total,
        **rental_kwargs,
    )

    safe_name = row.route_no.strip().replace("/", "-").replace("\\", "-") or invoice_no.replace("/", "-")
    pdf_path = output_dir / f"{safe_name}.pdf"
    pdf_path, sig_box = generate_eee_taxi_invoice_pdf(ctx, pdf_path)
    if sig_box is None:
        # Locate the footer signature zone here (pypdf only) so the local
        # helper stamps the right place; its own finder only knows the
        # Pallia Trans layout.
        sig_box = _find_signature_box(pdf_path)
    return pdf_path, sig_box


def generate_next_invoice(batch_id: str, db: Session) -> dict:
    """Generate the next pending invoice of a batch. Returns a progress dict.

    One invoice per call keeps every request short, so no serverless timeout
    and no reliance on work continuing after the response.
    """
    batch = db.get(EeeTaxiBatch, batch_id)
    if batch is None:
        raise LookupError("Batch not found.")

    inv_rec: EeeTaxiInvoice | None = (
        db.query(EeeTaxiInvoice)
        .filter(
            EeeTaxiInvoice.batch_id == batch_id,
            EeeTaxiInvoice.status == EeeTaxiInvoiceStatus.PENDING,
        )
        .order_by(EeeTaxiInvoice.row_index)
        .first()
    )
    if inv_rec is None:
        status = finalize_batch_status(batch, db)
        return {"generated": None, "remaining": 0, "batch_status": status}

    if batch.status == EeeTaxiBatchStatus.PENDING:
        batch.status = EeeTaxiBatchStatus.PROCESSING
        db.commit()

    sign_mode  = batch.sign_mode or "usb"
    invoice_no = format_invoice_no(
        financial_year(batch.invoice_date),
        batch.start_suffix + inv_rec.row_index,
    )
    inv_rec.invoice_no = invoice_no
    inv_rec.status     = EeeTaxiInvoiceStatus.GENERATING
    db.commit()

    try:
        rates = batch_rates(batch)
        rows  = batch_rows(batch, rates)
        row   = next((r for r in rows if r.row_index == inv_rec.row_index), None)
        if row is None:
            raise RuntimeError(f"Row {inv_rec.row_index} is no longer present in the stored CSV.")

        output_dir = Path(settings.eee_taxi_output_dir) / batch_id
        output_dir.mkdir(parents=True, exist_ok=True)

        pdf_path, sig_box = build_invoice_pdf(
            row, invoice_no, batch.invoice_date, rates, output_dir,
        )

        inv_rec.booking_type = row.booking_type
        inv_rec.pdf_path     = str(pdf_path)
        inv_rec.pdf_data     = pdf_path.read_bytes()
        inv_rec.sig_box      = [float(v) for v in sig_box] if sig_box else None

        if sign_mode == "dummy":
            signed_path = sign_eee_taxi_pdf_dummy(pdf_path, sig_box=sig_box)
            inv_rec.signed_pdf_path = str(signed_path)
            inv_rec.signed_pdf_data = signed_path.read_bytes()
            inv_rec.pdf_data        = None   # signed copy supersedes it
            inv_rec.status          = EeeTaxiInvoiceStatus.DONE
            logger.info("Batch {}: invoice {} done (dummy signature).", batch_id, invoice_no)
        else:
            inv_rec.status = EeeTaxiInvoiceStatus.AWAITING_SIGNATURE
            logger.info("Batch {}: invoice {} generated, awaiting USB signature.", batch_id, invoice_no)
        db.commit()

    except Exception as exc:
        db.rollback()
        inv_rec = db.get(EeeTaxiInvoice, inv_rec.id)
        if inv_rec is not None:
            inv_rec.status        = EeeTaxiInvoiceStatus.FAILED
            inv_rec.error_message = str(exc)[:1000]
            db.commit()
        logger.error("Batch {}: row {} failed: {}", batch_id, invoice_no, exc)

    remaining = (
        db.query(EeeTaxiInvoice)
        .filter(
            EeeTaxiInvoice.batch_id == batch_id,
            EeeTaxiInvoice.status == EeeTaxiInvoiceStatus.PENDING,
        )
        .count()
    )
    batch  = db.get(EeeTaxiBatch, batch_id)
    status = finalize_batch_status(batch, db)
    return {
        "generated":    {"id": inv_rec.id, "invoice_no": invoice_no, "status": inv_rec.status}
                        if inv_rec is not None else None,
        "remaining":    remaining,
        "batch_status": status,
    }


def build_zip(batch_id: str, db: Session, booking_type: str | None = None) -> bytes:
    """Return a ZIP archive of signed PDFs in the batch.

    booking_type=None  → all invoices
    booking_type="p2p" → P2P only
    booking_type="rental" → Rental only
    """
    q = db.query(EeeTaxiInvoice).filter(
        EeeTaxiInvoice.batch_id == batch_id,
        EeeTaxiInvoice.status == EeeTaxiInvoiceStatus.DONE,
    )
    if booking_type is not None:
        q = q.filter(EeeTaxiInvoice.booking_type == booking_type)
    invoices = q.all()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for inv in invoices:
            data = signed_pdf_bytes(inv)
            if data:
                zf.writestr(signed_filename(inv), data)
    return buf.getvalue()
