"""Extract GCN pages from a multi-page Mahindra Logistics PDF.

A single PDF from the billing team contains (in document order):
  - One or more GCN / Lorry Receipt pages  (max 3)
  - Tax Invoice pages interspersed (ignored for extraction)

Returns GCN field dicts in the exact page order they appear in the PDF.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger
from pypdf import PdfReader

from app.config import settings
from app.services.extraction import extract_lr_fields
from app.services.gemini_extractor import GeminiUnavailable, extract_lr_fields_gemini

MAX_GCNS = 3

_GCN_MARKERS = [
    "goods consignment note",
    "g.c.n.",
    "delivery order no",
    "lorry receipt",
]


class PDFProcessingError(Exception):
    pass


def _is_gcn_page(text: str) -> bool:
    t = text.lower()
    return any(marker in t for marker in _GCN_MARKERS)


def _has_gcn_fields(fields: dict[str, Any]) -> bool:
    """Return True if the extracted dict contains at least one GCN identifier."""
    if not fields:
        return False
    if fields.get("gcn_no"):
        return True
    dos = fields.get("delivery_order_nos") or []
    return bool(dos)


def extract_gcns_from_pdf(pdf_path: Path) -> list[dict[str, Any]]:
    """Scan PDF pages in document order, extract up to MAX_GCNS GCN pages.

    Each returned dict has the same shape as extraction.extract_lr_fields():
      vehicle_no, gcn_no, gcn_date, from_city, destination,
      delivery_order_nos (list[str]), qty, delivery_date.
    """
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as exc:
        raise PDFProcessingError(f"Cannot open PDF: {exc}") from exc

    gcns: list[dict[str, Any]] = []

    for page_num, page in enumerate(reader.pages):
        if len(gcns) >= MAX_GCNS:
            break

        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""

        is_text_gcn = _is_gcn_page(text)
        # Scanned/image-based PDFs yield little or no extractable text.
        # Try Gemini on those pages too; skip only if text was extracted but had no GCN markers.
        is_scanned_page = len(text.strip()) < 100

        if not is_text_gcn and not is_scanned_page:
            logger.debug("PDF page {} — text extracted, no GCN markers, skipping", page_num + 1)
            continue

        if is_text_gcn:
            logger.info("GCN markers found at page {} of {}", page_num + 1, pdf_path.name)
        else:
            logger.info("Page {} has little text (scanned?), attempting Gemini extraction", page_num + 1)

        fields: dict[str, Any] | None = None

        if settings.extraction_engine == "gemini":
            try:
                fields = extract_lr_fields_gemini(pdf_path, page_num=page_num)
                if is_scanned_page and not is_text_gcn and not _has_gcn_fields(fields):
                    logger.info("Page {} — Gemini found no GCN identifiers, skipping (likely Tax Invoice)", page_num + 1)
                    fields = None
                else:
                    logger.info("Gemini extracted GCN {} from page {}", len(gcns) + 1, page_num + 1)
            except GeminiUnavailable as exc:
                logger.warning("Gemini failed on PDF page {} ({}), falling back to text", page_num + 1, exc)

        if fields is None and is_text_gcn:
            # Regex fallback only makes sense when text was actually extracted
            fields = extract_lr_fields(text)
            logger.info("Text-regex extracted GCN {} from page {}", len(gcns) + 1, page_num + 1)

        if fields is not None:
            gcns.append(fields)

    if not gcns:
        raise PDFProcessingError(
            "No GCN pages were found in this PDF. "
            "Make sure the PDF contains a Mahindra Logistics Goods Consignment Note."
        )

    logger.info("Extracted {} GCN(s) from {}", len(gcns), pdf_path.name)
    return gcns
