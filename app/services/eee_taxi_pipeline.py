"""Batch orchestrator for EEE-Taxi invoice generation + DSC signing.

Flow per batch:
  1. For each CSV row: generate the unsigned PDF.
  2. sign_mode == "dummy": stamp a visible DUMMY SIGNATURE on the server -> 'done'.
     sign_mode == "usb":   store the unsigned PDF + signature box -> 'awaiting_signature'.
     The browser then fetches each unsigned PDF, has the local signing helper
     (PalliaSignHelper.exe, http://127.0.0.1:7777) sign it with the USB token,
     and uploads the signed PDF back. The server never sees the PIN and never
     needs the token, so this works from Vercel.
  3. Failed rows are marked 'failed'; the batch continues with the next row.
  4. Batch status: 'awaiting_signature' while unsigned PDFs remain, then
     'completed' (all ok), 'partial' (some failed) or 'failed' (all failed).

PDF bytes are kept in the database (pdf_data / signed_pdf_data) because the
disk on serverless hosts is per-instance and ephemeral; file paths are only a
fallback for local runs.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session

from app.models import EeeTaxiBatch, EeeTaxiBatchStatus, EeeTaxiInvoice, EeeTaxiInvoiceStatus
from app.services.eee_taxi_clients import UnknownClientError, is_local, lookup_client
from app.services.eee_taxi_csv import EeeTaxiRow, financial_year, format_invoice_no
from app.services.eee_taxi_pdf import InvoiceContext, compute_tax, generate_eee_taxi_invoice_pdf
from app.services.eee_taxi_rates import RateCard
from app.services.eee_taxi_rental_calc import calculate_rental_fare
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


# ── Batch generation ──────────────────────────────────────────────────────────

def run_batch(
    batch_id: str,
    rows: list[EeeTaxiRow],
    invoice_date: date,
    start_suffix: int,
    output_dir: Path,
    db: Session,
    rates: RateCard,
    sign_mode: str = "usb",
) -> None:
    """Generate every invoice PDF for a batch. Runs in a background thread.

    ``rates`` is the rate-card snapshot taken when the batch started, so the
    invoice breakdown matches the fares calculated for this batch.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    batch = db.get(EeeTaxiBatch, batch_id)
    if batch is None:
        logger.error("Batch {} not found in DB — aborting pipeline.", batch_id)
        return

    batch.status = EeeTaxiBatchStatus.PROCESSING
    db.commit()

    fy = financial_year(invoice_date)

    for idx, row in enumerate(rows):
        suffix = start_suffix + idx
        invoice_no = format_invoice_no(fy, suffix)

        inv_rec: EeeTaxiInvoice | None = (
            db.query(EeeTaxiInvoice)
            .filter(EeeTaxiInvoice.batch_id == batch_id, EeeTaxiInvoice.row_index == row.row_index)
            .one_or_none()
        )
        if inv_rec is None:
            logger.warning("Batch {}: no DB record for row_index={}; skipping.", batch_id, row.row_index)
            continue

        inv_rec.invoice_no   = invoice_no
        inv_rec.booking_type = row.booking_type
        inv_rec.status       = EeeTaxiInvoiceStatus.GENERATING
        db.commit()

        try:
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
                # Locate the footer signature zone here (pypdf only) so the
                # local helper stamps the right place; its own finder only
                # knows the Pallia Trans layout.
                sig_box = _find_signature_box(pdf_path)

            inv_rec.pdf_path = str(pdf_path)
            inv_rec.pdf_data = pdf_path.read_bytes()
            inv_rec.sig_box  = [float(v) for v in sig_box] if sig_box else None

            if sign_mode == "dummy":
                signed_path = sign_eee_taxi_pdf_dummy(pdf_path, sig_box=sig_box)
                inv_rec.signed_pdf_path = str(signed_path)
                inv_rec.signed_pdf_data = signed_path.read_bytes()
                inv_rec.pdf_data        = None   # signed copy supersedes it
                inv_rec.status          = EeeTaxiInvoiceStatus.DONE
                logger.info("Batch {}: invoice {} done (dummy signature).", batch_id, invoice_no)
            else:
                # Real signing happens in the browser via the local helper.
                inv_rec.status = EeeTaxiInvoiceStatus.AWAITING_SIGNATURE
                logger.info("Batch {}: invoice {} generated, awaiting USB signature.", batch_id, invoice_no)
            db.commit()

        except Exception as exc:
            inv_rec.status        = EeeTaxiInvoiceStatus.FAILED
            inv_rec.error_message = str(exc)
            db.commit()
            logger.error("Batch {}: row {} failed: {}", batch_id, idx, exc)

    status = finalize_batch_status(batch, db)
    logger.info("Batch {} generation finished — status={}", batch_id, status)


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
