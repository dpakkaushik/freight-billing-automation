"""Billing API — Invoice + LR endpoints.

Routes
------
POST   /api/billing/invoices                        Create draft invoice
GET    /api/billing/invoices                        List invoices
GET    /api/billing/invoices/{id}                   Invoice detail (incl. LRs)
PATCH  /api/billing/invoices/{id}                   Update suffix / date
DELETE /api/billing/invoices/{id}                   Delete draft invoice

POST   /api/billing/invoices/{id}/lrs               Upload an LR image, runs OCR
PATCH  /api/billing/invoices/{id}/lrs/{lr_id}       Confirm LR fields (incl. DO# rows)
DELETE /api/billing/invoices/{id}/lrs/{lr_id}       Remove an LR

POST   /api/billing/invoices/{id}/generate          Fill Excel + convert to PDF
GET    /api/billing/invoices/{id}/pdf               Download generated PDF
GET    /api/billing/invoices/{id}/xlsx              Download generated Excel
POST   /api/billing/invoices/{id}/submit-portal     Placeholder (501) — future

POST   /api/billing/invoices/{id}/sign-pdf          Sign PDF with USB DSC token
GET    /api/billing/invoices/{id}/signed-pdf        Download digitally-signed PDF

GET    /api/billing/next-suffix                     Suggest next invoice suffix
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import ExternalApiEvent, Invoice, InvoiceStatus, LR, LRStatus
from app.schemas import (
    InvoiceCreateIn,
    InvoiceListOut,
    InvoiceOut,
    InvoiceUpdateIn,
    LRConfirmIn,
    LROut,
    SuggestedSuffixOut,
)
from app.services.excel_filler import fill_invoice
from app.services.extraction import extract_lr_fields
from app.services.gemini_extractor import GeminiUnavailable, extract_lr_fields_gemini
from app.services.invoice_numbering import suggest_next_suffix
from app.services.llm_extractor import LLMExtractorUnavailable, extract_lr_fields_llm
from app.services.ocr import run_ocr
from app.services.ollama_extractor import OllamaUnavailable, extract_lr_fields_ollama
from app.services.auth import get_current_active_user
from app.services.pdf_converter import LibreOfficeNotFound, xlsx_to_pdf
from app.services.pdf_signer import SigningError, TokenNotFound, WrongPIN, sign_invoice_pdf


router = APIRouter(prefix="/api/billing", tags=["billing"], dependencies=[Depends(get_current_active_user)])


ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
MAX_BYTES = 15 * 1024 * 1024


# ---------- helpers ----------

def _invoice_dir(invoice_id: str) -> Path:
    """Per-invoice folder under UPLOAD_DIR for storing LR images + outputs."""
    p = settings.upload_dir / "invoices" / invoice_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _require_invoice(db: Session, invoice_id: str) -> Invoice:
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        raise HTTPException(404, f"Invoice {invoice_id} not found")
    return inv


def _require_lr(db: Session, invoice_id: str, lr_id: str) -> LR:
    lr = db.get(LR, lr_id)
    if lr is None or lr.invoice_id != invoice_id:
        raise HTTPException(404, f"LR {lr_id} not found in invoice {invoice_id}")
    return lr


# ---------- next-suffix ----------

@router.get("/next-suffix", response_model=SuggestedSuffixOut)
def next_suffix(db: Session = Depends(get_db)) -> SuggestedSuffixOut:
    return SuggestedSuffixOut(suggested=suggest_next_suffix(db))


# ---------- invoices ----------

@router.post("/invoices", response_model=InvoiceOut, status_code=status.HTTP_201_CREATED)
def create_invoice(body: InvoiceCreateIn, db: Session = Depends(get_db)) -> InvoiceOut:
    inv = Invoice(
        suffix=body.suffix or suggest_next_suffix(db),
        invoice_date=body.invoice_date or date.today(),
        status=InvoiceStatus.DRAFT,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    logger.info("Created draft invoice {} suffix={}", inv.id, inv.suffix)
    return InvoiceOut.model_validate(inv)


@router.get("/invoices", response_model=InvoiceListOut)
def list_invoices(limit: int = 100, db: Session = Depends(get_db)) -> InvoiceListOut:
    stmt = select(Invoice).order_by(Invoice.created_at.desc()).limit(limit)
    items = list(db.execute(stmt).scalars())
    return InvoiceListOut(items=[InvoiceOut.model_validate(i) for i in items], total=len(items))


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: str, db: Session = Depends(get_db)) -> InvoiceOut:
    inv = _require_invoice(db, invoice_id)
    return InvoiceOut.model_validate(inv)


@router.patch("/invoices/{invoice_id}", response_model=InvoiceOut)
def update_invoice(invoice_id: str, body: InvoiceUpdateIn, db: Session = Depends(get_db)) -> InvoiceOut:
    inv = _require_invoice(db, invoice_id)
    if inv.status != InvoiceStatus.DRAFT:
        raise HTTPException(409, "Only draft invoices can be edited.")
    if body.suffix is not None:
        inv.suffix = body.suffix.strip()
    if body.invoice_date is not None:
        inv.invoice_date = body.invoice_date
    db.commit()
    db.refresh(inv)
    return InvoiceOut.model_validate(inv)


@router.delete("/invoices/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_invoice(invoice_id: str, db: Session = Depends(get_db)) -> None:
    inv = _require_invoice(db, invoice_id)
    if inv.status not in {InvoiceStatus.DRAFT, InvoiceStatus.FAILED}:
        raise HTTPException(409, "Only draft or failed invoices can be deleted.")
    db.delete(inv)
    db.commit()
    return None


# ---------- LR upload + confirm ----------

@router.post("/invoices/{invoice_id}/lrs", response_model=LROut, status_code=status.HTTP_201_CREATED)
async def upload_lr(
    invoice_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> LROut:
    inv = _require_invoice(db, invoice_id)
    if inv.status != InvoiceStatus.DRAFT:
        raise HTTPException(409, "Cannot add LRs to a non-draft invoice.")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(400, f"Unsupported file type '{ext}'.")

    contents = await file.read()
    if not contents:
        raise HTTPException(400, "Empty file.")
    if len(contents) > MAX_BYTES:
        raise HTTPException(413, f"File too large (max {MAX_BYTES // (1024 * 1024)} MB).")

    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest = _invoice_dir(invoice_id) / safe_name
    dest.write_bytes(contents)

    lr = LR(
        invoice_id=invoice_id,
        original_filename=file.filename or safe_name,
        image_path=str(dest),
        status=LRStatus.OCR_PROCESSING,
    )
    db.add(lr)
    db.commit()
    db.refresh(lr)

    try:
        extraction_note = ""
        if settings.extraction_engine == "gemini":
            t0 = time.monotonic()
            try:
                fields = extract_lr_fields_gemini(dest)
                duration = int((time.monotonic() - t0) * 1000)
                ocr_text = json.dumps(fields, indent=2, ensure_ascii=False)
                extraction_note = f"Extracted by Gemini ({settings.gemini_model})"
                db.add(ExternalApiEvent(api_name="gemini", model=settings.gemini_model, success=True, duration_ms=duration, lr_id=lr.id, invoice_id=invoice_id))
                db.commit()
                logger.info("Gemini extraction succeeded for LR {}", lr.id)
            except GeminiUnavailable as exc:
                duration = int((time.monotonic() - t0) * 1000)
                extraction_note = f"Gemini failed: {exc} — fell back to EasyOCR"
                db.add(ExternalApiEvent(api_name="gemini", model=settings.gemini_model, success=False, error_message=str(exc), duration_ms=duration, lr_id=lr.id, invoice_id=invoice_id))
                db.commit()
                logger.warning("Gemini unavailable ({}), falling back to regex", exc)
                ocr_text = run_ocr(dest)
                fields = extract_lr_fields(ocr_text)
        elif settings.extraction_engine == "llm":
            t0 = time.monotonic()
            try:
                fields, ocr_text = extract_lr_fields_llm(dest)
                duration = int((time.monotonic() - t0) * 1000)
                extraction_note = f"Extracted by OCR + LLM ({settings.ollama_text_model})"
                db.add(ExternalApiEvent(api_name="ollama_llm", model=settings.ollama_text_model, success=True, duration_ms=duration, lr_id=lr.id, invoice_id=invoice_id))
                db.commit()
                logger.info("LLM extraction succeeded for LR {}", lr.id)
            except LLMExtractorUnavailable as exc:
                duration = int((time.monotonic() - t0) * 1000)
                extraction_note = f"LLM failed: {exc} — fell back to EasyOCR"
                db.add(ExternalApiEvent(api_name="ollama_llm", model=settings.ollama_text_model, success=False, error_message=str(exc), duration_ms=duration, lr_id=lr.id, invoice_id=invoice_id))
                db.commit()
                logger.warning("LLM unavailable ({}), falling back to regex", exc)
                ocr_text = run_ocr(dest)
                fields = extract_lr_fields(ocr_text)
        elif settings.extraction_engine == "ollama":
            t0 = time.monotonic()
            try:
                fields = extract_lr_fields_ollama(dest)
                duration = int((time.monotonic() - t0) * 1000)
                ocr_text = json.dumps(fields, indent=2, ensure_ascii=False)
                extraction_note = f"Extracted by Ollama ({settings.ollama_model})"
                db.add(ExternalApiEvent(api_name="ollama", model=settings.ollama_model, success=True, duration_ms=duration, lr_id=lr.id, invoice_id=invoice_id))
                db.commit()
                logger.info("Ollama extraction succeeded for LR {}", lr.id)
            except OllamaUnavailable as exc:
                duration = int((time.monotonic() - t0) * 1000)
                extraction_note = f"Ollama failed: {exc} — fell back to EasyOCR"
                db.add(ExternalApiEvent(api_name="ollama", model=settings.ollama_model, success=False, error_message=str(exc), duration_ms=duration, lr_id=lr.id, invoice_id=invoice_id))
                db.commit()
                logger.warning("Ollama unavailable ({}), falling back to regex", exc)
                ocr_text = run_ocr(dest)
                fields = extract_lr_fields(ocr_text)
        else:
            ocr_text = run_ocr(dest)
            fields = extract_lr_fields(ocr_text)
            extraction_note = "Extracted by EasyOCR + regex"

        # Pre-build do_rows from extracted delivery_order_nos so the user
        # sees one input row per DO# on the confirmation screen.
        do_nos = fields.get("delivery_order_nos") or []
        fields_with_rows = dict(fields)
        fields_with_rows["do_rows"] = [
            {"delivery_order_no": d, "total_amount": ""} for d in do_nos
        ] or [{"delivery_order_no": "", "total_amount": ""}]
        fields_with_rows["_extraction_note"] = extraction_note
        lr.ocr_text = ocr_text
        lr.extracted_fields = fields_with_rows
        lr.status = LRStatus.AWAITING_CONFIRMATION
        db.commit()
        db.refresh(lr)
    except Exception as exc:
        logger.exception("Extraction failed for LR {}", lr.id)
        lr.status = LRStatus.FAILED
        lr.error_message = f"Extraction failed: {exc}"
        db.commit()
        db.refresh(lr)
        raise HTTPException(422, f"Extraction failed: {exc}")

    return LROut.model_validate(lr)


@router.patch("/invoices/{invoice_id}/lrs/{lr_id}", response_model=LROut)
def confirm_lr(
    invoice_id: str,
    lr_id: str,
    body: LRConfirmIn,
    db: Session = Depends(get_db),
) -> LROut:
    inv = _require_invoice(db, invoice_id)
    if inv.status != InvoiceStatus.DRAFT:
        raise HTTPException(409, "Invoice is not editable.")
    lr = _require_lr(db, invoice_id, lr_id)

    lr.confirmed_fields = body.fields.model_dump()
    lr.status = LRStatus.CONFIRMED
    lr.error_message = None
    db.commit()
    db.refresh(lr)
    return LROut.model_validate(lr)


@router.delete("/invoices/{invoice_id}/lrs/{lr_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_lr(invoice_id: str, lr_id: str, db: Session = Depends(get_db)) -> None:
    _require_invoice(db, invoice_id)
    lr = _require_lr(db, invoice_id, lr_id)
    # Also remove the image file
    try:
        p = Path(lr.image_path)
        if p.exists():
            p.unlink()
    except Exception as exc:  # pragma: no cover
        logger.warning("Could not delete LR image {}: {}", lr.image_path, exc)
    db.delete(lr)
    db.commit()
    return None


# ---------- generate ----------

@router.post("/invoices/{invoice_id}/generate", response_model=InvoiceOut)
def generate_invoice(invoice_id: str, db: Session = Depends(get_db)) -> InvoiceOut:
    inv = _require_invoice(db, invoice_id)
    if inv.status != InvoiceStatus.DRAFT:
        raise HTTPException(409, "Invoice has already been generated.")

    confirmed = [lr for lr in inv.lrs if lr.status == LRStatus.CONFIRMED and lr.confirmed_fields]
    if not confirmed:
        raise HTTPException(400, "No confirmed LRs to generate from.")

    out_dir = _invoice_dir(inv.id)
    xlsx_path = out_dir / f"invoice_{inv.suffix}.xlsx"
    try:
        fill_invoice(
            suffix=inv.suffix,
            invoice_date=inv.invoice_date,
            lrs=[lr.confirmed_fields for lr in confirmed],
            output_path=xlsx_path,
        )
    except Exception as exc:
        logger.exception("Excel filler failed for invoice {}", inv.id)
        inv.status = InvoiceStatus.FAILED
        inv.error_message = f"Excel generation failed: {exc}"
        db.commit()
        raise HTTPException(500, f"Excel generation failed: {exc}")

    inv.excel_path = str(xlsx_path)

    try:
        pdf_path = xlsx_to_pdf(xlsx_path, out_dir)
    except LibreOfficeNotFound as exc:
        inv.error_message = str(exc)
        db.commit()
        # Don't mark FAILED — Excel is still good. User can install LO and retry.
        raise HTTPException(503, str(exc))
    except Exception as exc:
        logger.exception("PDF conversion failed for invoice {}", inv.id)
        inv.error_message = f"PDF conversion failed: {exc}"
        db.commit()
        raise HTTPException(500, f"PDF conversion failed: {exc}")

    inv.pdf_path = str(pdf_path)
    inv.status = InvoiceStatus.GENERATED
    inv.error_message = None
    db.commit()
    db.refresh(inv)
    logger.info("Invoice {} generated -> {}", inv.id, pdf_path)
    return InvoiceOut.model_validate(inv)


@router.get("/invoices/{invoice_id}/pdf")
def download_pdf(invoice_id: str, db: Session = Depends(get_db)):
    inv = _require_invoice(db, invoice_id)
    if not inv.pdf_path or not Path(inv.pdf_path).exists():
        raise HTTPException(404, "PDF not generated for this invoice yet.")
    return FileResponse(
        inv.pdf_path,
        media_type="application/pdf",
        filename=f"PTLM-2627SWM-{inv.suffix}.pdf",
    )


@router.get("/invoices/{invoice_id}/xlsx")
def download_xlsx(invoice_id: str, db: Session = Depends(get_db)):
    inv = _require_invoice(db, invoice_id)
    if not inv.excel_path or not Path(inv.excel_path).exists():
        raise HTTPException(404, "Excel not generated for this invoice yet.")
    return FileResponse(
        inv.excel_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"PTLM-2627SWM-{inv.suffix}.xlsx",
    )


# ---------- PDF signing ----------

class SignPdfIn(BaseModel):
    pin: str


@router.post("/invoices/{invoice_id}/sign-pdf", response_model=InvoiceOut)
def sign_pdf(invoice_id: str, body: SignPdfIn, db: Session = Depends(get_db)) -> InvoiceOut:
    """Sign the generated PDF with the USB DSC token.

    Returns the updated invoice (signed_pdf_path will be populated on success).
    """
    inv = _require_invoice(db, invoice_id)
    if not inv.pdf_path or not Path(inv.pdf_path).exists():
        raise HTTPException(400, "PDF has not been generated yet. Generate the invoice first.")

    try:
        sign_invoice_pdf(Path(inv.pdf_path), body.pin)
    except TokenNotFound as exc:
        raise HTTPException(503, str(exc))
    except WrongPIN as exc:
        raise HTTPException(401, str(exc))
    except SigningError as exc:
        raise HTTPException(500, str(exc))

    db.refresh(inv)
    logger.info("Signed PDF for invoice {}", inv.id)
    return InvoiceOut.model_validate(inv)


@router.get("/invoices/{invoice_id}/signed-pdf")
def download_signed_pdf(invoice_id: str, db: Session = Depends(get_db)):
    inv = _require_invoice(db, invoice_id)
    signed_path = inv.signed_pdf_path
    if not signed_path:
        raise HTTPException(404, "Signed PDF not found. Please sign the invoice first.")
    return FileResponse(
        signed_path,
        media_type="application/pdf",
        filename=f"PTLM-2627SWM-{inv.suffix}_signed.pdf",
    )


# ---------- LR image ----------

@router.get("/invoices/{invoice_id}/lrs/{lr_id}/image")
def get_lr_image(invoice_id: str, lr_id: str, db: Session = Depends(get_db)):
    """Serve the uploaded LR image so the frontend can preview it."""
    lr = _require_lr(db, invoice_id, lr_id)
    path = Path(lr.image_path)
    if not path.exists():
        raise HTTPException(404, "Image file not found on disk.")
    suffix = path.suffix.lower()
    media = {"jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp", ".bmp": "image/bmp", ".tiff": "image/tiff"}
    return FileResponse(path, media_type=media.get(suffix, "image/jpeg"))


# ---------- TMS portal placeholder ----------

@router.post("/invoices/{invoice_id}/submit-portal", status_code=status.HTTP_501_NOT_IMPLEMENTED)
def submit_to_portal(invoice_id: str, db: Session = Depends(get_db)):
    """Placeholder. Future: drive Playwright to upload the PDF on Pallia TMS."""
    _require_invoice(db, invoice_id)
    raise HTTPException(
        status_code=501,
        detail="Submit to TMS Portal is not enabled yet. Coming in the next iteration.",
    )
