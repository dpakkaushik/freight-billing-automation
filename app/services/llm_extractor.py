"""LLM-based LR field extractor.

Pipeline:
  1. EasyOCR reads the image -> raw text
  2. llama3.2:3b (text model) parses that text -> structured JSON

This is more reliable than vision models because:
- OCR handles the image reading (what it's built for)
- A text LLM handles structure extraction (what it's built for)
- 3B text model fits easily in RAM and runs fast
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from app.config import settings
from app.services.ocr import run_ocr


class LLMExtractorUnavailable(Exception):
    """Raised when Ollama text model is unreachable or inference fails."""


_SYSTEM_PROMPT = (
    "You are a data extraction assistant. You will be given raw OCR text from a "
    "Mahindra Logistics Lorry Receipt (LR / GCN). The OCR text may have errors — "
    "letters instead of digits, garbled words, missing spaces. Use context to correct them.\n\n"
    "Extract these fields and return ONLY a JSON object:\n"
    "- vehicle_no: Indian vehicle plate (2 letters + 2 digits + letters + 4 digits, e.g. NL01AK0496)\n"
    "- gcn_no: G.C.N. number, digits only, 6-12 digits (e.g. 112032145)\n"
    "- gcn_date: LR date in DD-Mon-YYYY format (e.g. 07-Apr-2026)\n"
    "- from_city: origin city (after 'From' label)\n"
    "- destination: destination city (after 'To' label or in consignee address)\n"
    "- qty: number of packages with unit (e.g. 9 Units)\n"
    "- delivery_order_nos: array of all Delivery Order numbers from the 'Delivery Order No' "
    "column — these are 10-digit numbers. There may be multiple separated by commas.\n"
    "- delivery_date: date from the Proof of Delivery / POD section (handwritten, e.g. 15/04/2026). "
    "null if not present.\n\n"
    "OCR correction hints:\n"
    "- O/U/Q/D often means digit 0; I/L often means digit 1\n"
    "- GChNo / GCN No / G.C.N.No all refer to the GCN number\n"
    "- Delivery Order numbers are ~10 digits; if truncated (e.g. 12304508) check if "
    "a leading 7 or other digit was dropped\n\n"
    "Return ONLY valid JSON. No explanation, no markdown."
)


def extract_lr_fields_llm(image_path: str | Path) -> tuple[dict[str, Any], str]:
    """Extract LR fields using OCR + Ollama text model.

    Returns:
        (fields_dict, ocr_text) — same dict shape as extraction.extract_lr_fields()

    Raises:
        LLMExtractorUnavailable: Ollama unreachable or inference failed.
        ValueError: image file missing.
    """
    path = Path(image_path)
    if not path.exists():
        raise ValueError(f"Image not found: {path}")

    ocr_text = run_ocr(path)
    logger.debug("OCR produced {} chars, sending to LLM", len(ocr_text))

    prompt = f"Extract the LR fields from this OCR text:\n\n{ocr_text}"

    payload = {
        "model": settings.ollama_text_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "format": "json",
        "stream": False,
        "keep_alive": 0,  # unload from RAM immediately after response
        "options": {"temperature": 0},
    }

    try:
        resp = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=60.0,
        )
    except httpx.ConnectError as exc:
        raise LLMExtractorUnavailable(
            f"Cannot connect to Ollama at {settings.ollama_base_url}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise LLMExtractorUnavailable("Ollama text model timed out.") from exc

    if resp.status_code == 404:
        raise LLMExtractorUnavailable(
            f"Model '{settings.ollama_text_model}' not found. "
            f"Run: ollama pull {settings.ollama_text_model}"
        )
    if resp.status_code >= 500:
        raise LLMExtractorUnavailable(
            f"Ollama returned {resp.status_code}: {resp.text[:200]}"
        )
    resp.raise_for_status()

    raw = resp.json().get("message", {}).get("content", "")
    logger.debug("LLM raw response: {}", raw[:300])

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("LLM returned invalid JSON: {}", raw[:500])
        raise LLMExtractorUnavailable(f"LLM returned non-JSON: {exc}") from exc

    fields = _normalize(data)
    return fields, ocr_text


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    do_nos_raw = data.get("delivery_order_nos") or []
    if isinstance(do_nos_raw, str):
        do_nos_raw = [s.strip() for s in do_nos_raw.split(",") if s.strip()]
    delivery_order_nos = [str(d).strip() for d in do_nos_raw if d]

    fields: dict[str, Any] = {
        "vehicle_no": _clean(data.get("vehicle_no")),
        "gcn_no": _clean(data.get("gcn_no")),
        "gcn_date": _clean(data.get("gcn_date")),
        "from_city": _clean(data.get("from_city")),
        "destination": _clean(data.get("destination")),
        "delivery_order_nos": delivery_order_nos,
        "qty": _clean(data.get("qty")),
        "delivery_date": _clean(data.get("delivery_date")),
    }

    found = [k for k, v in fields.items() if v]
    logger.info("LLM extraction: {} fields found -> {}", len(found), fields)
    return fields


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("null", "none", "") else None
