"""Batch orchestrator for EEE-Taxi invoice generation + DSC signing.

Flow per batch:
  1. Validate PIN by opening mToken session once.
  2. For each CSV row: generate PDF -> sign PDF -> mark invoice done.
  3. Failed rows are marked 'failed'; batch continues to next row.
  4. Batch status set to 'completed' (all ok), 'partial' (some failed), or 'failed' (all failed).
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
from app.services.eee_taxi_rental_calc import calculate_rental_fare
from app.services.eee_taxi_signer import (
    SigningError,
    TokenNotFound,
    WrongPIN,
    sign_eee_taxi_pdf,
    sign_eee_taxi_pdf_dummy,
)


def run_batch(
    batch_id: str,
    rows: list[EeeTaxiRow],
    invoice_date: date,
    start_suffix: int,
    pin: str,
    output_dir: Path,
    db: Session,
    sign_mode: str = "usb",
) -> None:
    """Process all rows for a batch. Runs inside a BackgroundTask."""
    output_dir.mkdir(parents=True, exist_ok=True)
    batch = db.get(EeeTaxiBatch, batch_id)
    if batch is None:
        logger.error("Batch {} not found in DB — aborting pipeline.", batch_id)
        return

    batch.status = EeeTaxiBatchStatus.PROCESSING
    db.commit()

    fy = financial_year(invoice_date)
    done = 0
    failed = 0

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
            failed += 1
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
                fr = calculate_rental_fare(row)
                base_h, base_k = {920: (4, 40), 1840: (8, 80)}.get(fr.effective_package, (8, 80))
                rental_kwargs = dict(
                    is_rental=True,
                    rental_base_label=f"{base_h}/{base_k}",
                    rental_base_fare=Decimal(str(fr.effective_package)),
                    extra_hrs=fr.extra_time_hours,
                    extra_hrs_charge=fr.extra_time_charge,
                    extra_km_count=fr.extra_km,
                    extra_km_charge_val=fr.extra_km_charge,
                    night_charge_val=fr.night_charge,
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

            if sign_mode == "dummy":
                signed_path = sign_eee_taxi_pdf_dummy(pdf_path, sig_box=sig_box)
            else:
                signed_path = sign_eee_taxi_pdf(pdf_path, pin, sig_box=sig_box)

            inv_rec.pdf_path        = str(pdf_path)
            inv_rec.signed_pdf_path = str(signed_path)
            inv_rec.status          = EeeTaxiInvoiceStatus.DONE
            db.commit()
            done += 1
            logger.info("Batch {}: invoice {} done.", batch_id, invoice_no)

        except (TokenNotFound, WrongPIN) as exc:
            # Token problem stops the whole batch.
            inv_rec.status        = EeeTaxiInvoiceStatus.FAILED
            inv_rec.error_message = str(exc)
            db.commit()
            failed += len(rows) - idx
            logger.error("Batch {}: token error at row {} — aborting: {}", batch_id, idx, exc)
            break

        except Exception as exc:
            inv_rec.status        = EeeTaxiInvoiceStatus.FAILED
            inv_rec.error_message = str(exc)
            db.commit()
            failed += 1
            logger.error("Batch {}: row {} failed: {}", batch_id, idx, exc)

    if failed == 0:
        batch.status = EeeTaxiBatchStatus.COMPLETED
    elif done == 0:
        batch.status = EeeTaxiBatchStatus.FAILED
    else:
        batch.status = EeeTaxiBatchStatus.PARTIAL
    db.commit()
    logger.info(
        "Batch {} finished — done={} failed={} status={}",
        batch_id, done, failed, batch.status,
    )


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
            p = Path(inv.signed_pdf_path) if inv.signed_pdf_path else None
            if p and p.exists():
                zf.write(p, p.name)
    return buf.getvalue()
