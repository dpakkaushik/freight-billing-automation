"""EEE-Taxi invoice batch API endpoints."""
from __future__ import annotations

import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from loguru import logger

from app.config import settings
from app.database import SessionLocal
from app.models import EeeTaxiBatch, EeeTaxiBatchStatus, EeeTaxiInvoice, EeeTaxiInvoiceStatus
from app.services.eee_taxi_csv import parse_eee_taxi_csv
from app.services.eee_taxi_pipeline import build_zip, run_batch
from app.services.eee_taxi_rental_calc import (
    apply_fare_overrides,
    generate_full_calc_csv,
    parse_calc_csv,
)

router = APIRouter(prefix="/api/eee-taxi", tags=["eee-taxi"])


# ── Calculate fares for all rows ─────────────────────────────────────────────

@router.post("/calculate")
async def calculate_fares(csv_file: UploadFile):
    """Parse CSV, calculate all fares (P2P + rental), return enriched CSV.

    Response headers carry row counts:
        X-Total-Count, X-P2P-Count, X-Rental-Count
    User downloads the CSV, optionally edits Calc_Trip_Fare, and re-uploads
    that file when starting the batch to override any calculated fare.
    """
    csv_bytes = await csv_file.read()
    try:
        original_headers, rows = parse_eee_taxi_csv(csv_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not rows:
        raise HTTPException(status_code=400, detail="No valid data rows found in CSV.")

    try:
        calc_bytes = generate_full_calc_csv(rows, original_headers)
    except Exception as exc:
        logger.exception("generate_full_calc_csv failed")
        raise HTTPException(status_code=500, detail=f"Fare calculation failed: {exc}") from exc
    p2p_count    = sum(1 for r in rows if r.booking_type == "p2p")
    rental_count = sum(1 for r in rows if r.booking_type == "rental")
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    base     = (csv_file.filename or "batch").rsplit(".", 1)[0]
    filename = f"calculated_{base}_{ts}.csv"
    return Response(
        content=calc_bytes,
        media_type="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Count":  str(len(rows)),
            "X-P2P-Count":    str(p2p_count),
            "X-Rental-Count": str(rental_count),
            "Access-Control-Expose-Headers": "X-Total-Count, X-P2P-Count, X-Rental-Count",
        },
    )


# ── Start batch ───────────────────────────────────────────────────────────────

@router.post("/batch")
async def start_batch(
    csv_file: UploadFile,
    invoice_date: str = Form(...),
    start_suffix: int = Form(...),
    pin: str = Form(""),
    sign_mode: str = Form("usb"),
    calc_csv: Optional[UploadFile] = File(None),
):
    """Upload trip CSV (and optional modified calculated CSV) and start batch generation."""
    csv_bytes = await csv_file.read()

    try:
        _, rows = parse_eee_taxi_csv(csv_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"CSV parse error: {exc}") from exc

    if not rows:
        raise HTTPException(status_code=400, detail="No valid data rows found in CSV.")

    if sign_mode not in ("usb", "dummy"):
        raise HTTPException(status_code=400, detail=f"Invalid sign_mode: {sign_mode!r}.")

    if sign_mode == "usb" and not pin:
        raise HTTPException(status_code=400, detail="PIN is required for USB DSC signing.")

    try:
        inv_date = date.fromisoformat(invoice_date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid invoice_date: {invoice_date!r}")

    # ── Apply fare overrides from re-uploaded calculated CSV (if provided) ─────
    overrides: dict = {}
    if calc_csv is not None:
        calc_bytes = await calc_csv.read()
        if calc_bytes.strip():
            overrides = parse_calc_csv(calc_bytes)
            logger.info("Fare overrides from calculated CSV: {} rows", len(overrides))

    rows = apply_fare_overrides(rows, overrides)

    p2p_count    = sum(1 for r in rows if r.booking_type == "p2p")
    rental_count = sum(1 for r in rows if r.booking_type == "rental")

    batch_id   = uuid.uuid4().hex
    output_dir = Path(settings.eee_taxi_output_dir) / batch_id

    with SessionLocal() as db:
        batch = EeeTaxiBatch(
            id=batch_id,
            invoice_date=inv_date,
            start_suffix=start_suffix,
            total_rows=len(rows),
            csv_filename=csv_file.filename,
        )
        db.add(batch)
        db.flush()

        for row in rows:
            db.add(EeeTaxiInvoice(
                batch_id=batch_id,
                row_index=row.row_index,
                entity_name=row.entity_name,
                client_gstin=row.client_gstin,
                booking_type=row.booking_type,
            ))
        db.commit()

    logger.info(
        "EEE-Taxi batch {} created: {} rows ({} p2p, {} rental), suffix starts at {}",
        batch_id, len(rows), p2p_count, rental_count, start_suffix,
    )

    def _run():
        with SessionLocal() as db_thread:
            run_batch(
                batch_id=batch_id,
                rows=rows,
                invoice_date=inv_date,
                start_suffix=start_suffix,
                pin=pin,
                output_dir=output_dir,
                db=db_thread,
                sign_mode=sign_mode,
            )

    threading.Thread(target=_run, daemon=True).start()

    return {
        "batch_id":     batch_id,
        "total_rows":   len(rows),
        "p2p_count":    p2p_count,
        "rental_count": rental_count,
    }


# ── Batch status ──────────────────────────────────────────────────────────────

@router.get("/batch/{batch_id}")
def get_batch(batch_id: str):
    with SessionLocal() as db:
        batch = db.get(EeeTaxiBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found.")

        invoices = (
            db.query(EeeTaxiInvoice)
            .filter(EeeTaxiInvoice.batch_id == batch_id)
            .order_by(EeeTaxiInvoice.row_index)
            .all()
        )

        done   = sum(1 for i in invoices if i.status == EeeTaxiInvoiceStatus.DONE)
        failed = sum(1 for i in invoices if i.status == EeeTaxiInvoiceStatus.FAILED)
        p2p_done    = sum(1 for i in invoices if i.status == EeeTaxiInvoiceStatus.DONE and i.booking_type == "p2p")
        rental_done = sum(1 for i in invoices if i.status == EeeTaxiInvoiceStatus.DONE and i.booking_type == "rental")

        return {
            "batch_id":    batch_id,
            "status":      batch.status,
            "total_rows":  batch.total_rows,
            "done":        done,
            "failed":      failed,
            "p2p_done":    p2p_done,
            "rental_done": rental_done,
            "invoices": [
                {
                    "id":             inv.id,
                    "row_index":      inv.row_index,
                    "invoice_no":     inv.invoice_no,
                    "entity_name":    inv.entity_name,
                    "client_gstin":   inv.client_gstin,
                    "booking_type":   inv.booking_type,
                    "status":         inv.status,
                    "error_message":  inv.error_message,
                    "has_signed_pdf": bool(
                        inv.signed_pdf_path and Path(inv.signed_pdf_path).exists()
                    ),
                }
                for inv in invoices
            ],
        }


# ── Download single invoice ───────────────────────────────────────────────────

@router.get("/batch/{batch_id}/invoice/{invoice_id}/download")
def download_invoice(batch_id: str, invoice_id: str):
    with SessionLocal() as db:
        inv = db.get(EeeTaxiInvoice, invoice_id)
        if inv is None or inv.batch_id != batch_id:
            raise HTTPException(status_code=404, detail="Invoice not found.")
        if inv.status != EeeTaxiInvoiceStatus.DONE or not inv.signed_pdf_path:
            raise HTTPException(status_code=409, detail="Signed PDF not yet available.")
        p = Path(inv.signed_pdf_path)
        if not p.exists():
            raise HTTPException(status_code=404, detail="Signed PDF file missing on disk.")
        return FileResponse(str(p), media_type="application/pdf", filename=p.name)


# ── Download ZIPs ─────────────────────────────────────────────────────────────

@router.get("/batch/{batch_id}/download-all")
def download_all(batch_id: str):
    """ZIP of all signed PDFs (both P2P and Rental)."""
    with SessionLocal() as db:
        batch = db.get(EeeTaxiBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found.")
        if batch.status not in (EeeTaxiBatchStatus.COMPLETED, EeeTaxiBatchStatus.PARTIAL):
            raise HTTPException(status_code=409, detail="Batch not yet completed.")
        zip_bytes = build_zip(batch_id, db)

    filename = f"eee_taxi_all_{batch_id[:8]}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/batch/{batch_id}/download-p2p")
def download_p2p(batch_id: str):
    """ZIP of P2P signed PDFs only."""
    with SessionLocal() as db:
        batch = db.get(EeeTaxiBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found.")
        if batch.status not in (EeeTaxiBatchStatus.COMPLETED, EeeTaxiBatchStatus.PARTIAL):
            raise HTTPException(status_code=409, detail="Batch not yet completed.")
        zip_bytes = build_zip(batch_id, db, booking_type="p2p")

    filename = f"eee_taxi_p2p_{batch_id[:8]}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/batch/{batch_id}/download-rental")
def download_rental(batch_id: str):
    """ZIP of Rental signed PDFs only."""
    with SessionLocal() as db:
        batch = db.get(EeeTaxiBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Batch not found.")
        if batch.status not in (EeeTaxiBatchStatus.COMPLETED, EeeTaxiBatchStatus.PARTIAL):
            raise HTTPException(status_code=409, detail="Batch not yet completed.")
        zip_bytes = build_zip(batch_id, db, booking_type="rental")

    filename = f"eee_taxi_rental_{batch_id[:8]}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
