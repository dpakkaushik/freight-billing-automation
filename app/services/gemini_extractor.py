"""Gemini vision extractor for Mahindra Logistics LR images.

Uses Google's Gemini API (free tier: 1500 req/day on Flash).
Raises GeminiUnavailable so callers can fall back to EasyOCR gracefully.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from loguru import logger
from PIL import Image

from app.config import settings


class GeminiUnavailable(Exception):
    """Raised when Gemini API call fails."""


_PROMPT = (
    "You are reading a Mahindra Logistics Goods Consignment Note (GCN / LR). "
    "Extract exactly the fields below. Read the image carefully.\n\n"

    "FIELD LOCATIONS on this specific form:\n"
    "- vehicle_no: top-left area, labeled 'Vehicle No.' e.g. NL01AK0496\n"
    "- gcn_no: top-right area, labeled 'G.C.N.No.' — a 6-10 digit number. "
    "  This is DIFFERENT from the Delivery Order No.\n"
    "- gcn_date: top-right area, labeled 'Date.' next to G.C.N.No. e.g. 07-Apr-2026\n"
    "- from_city: small box labeled 'From' — just the city name e.g. Rajkot\n"
    "- destination: small box labeled 'To' — just the city name e.g. Gabhana. "
    "  Do NOT use the full consignee address.\n"
    "- qty: table column 'No of Pkg' e.g. 9 Units\n"
    "- delivery_order_nos: table column labeled 'Delivery Order No' (NOT the GCN No). "
    "  These are 10-digit numbers. There may be multiple. Return ALL as a JSON array of strings.\n"
    "- delivery_date: look in the 'Proof of Delivery' box at the bottom-right, "
    "  labeled 'Date:' — it is handwritten. Extract if readable, otherwise null.\n\n"

    "CRITICAL RULES:\n"
    "- delivery_order_nos MUST come only from the 'Delivery Order No' table column. "
    "  NEVER put the GCN number here.\n"
    "- destination is ONLY the city from the 'To' box, not from the address block.\n"
    "- Do NOT invent or guess any value not visible in the image.\n"
    "- Return ONLY a valid JSON object, no explanation, no markdown fences.\n\n"

    "{\n"
    '  "vehicle_no": "...",\n'
    '  "gcn_no": "...",\n'
    '  "gcn_date": "...",\n'
    '  "from_city": "...",\n'
    '  "destination": "...",\n'
    '  "qty": "...",\n'
    '  "delivery_order_nos": ["..."],\n'
    '  "delivery_date": "... or null"\n'
    "}"
)


def extract_lr_fields_gemini(image_path: str | Path) -> dict[str, Any]:
    """Extract LR fields using the Gemini vision API."""
    path = Path(image_path)
    if not path.exists():
        raise ValueError(f"Image not found: {path}")

    if not settings.gemini_api_key:
        raise GeminiUnavailable(
            "GEMINI_API_KEY not set in .env. "
            "Get a free key at https://aistudio.google.com/apikey"
        )

    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    logger.info("Sending image to Gemini | file={} size={}", path.name, img.size)

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[_PROMPT, img],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0,
            ),
        )
    except Exception as exc:
        raise GeminiUnavailable(f"Gemini API error: {exc}") from exc

    raw = response.text
    logger.debug("Gemini raw response ({} chars): {}", len(raw), raw[:300])

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Gemini returned invalid JSON: {}", raw[:500])
        raise GeminiUnavailable(f"Gemini returned non-JSON: {exc}") from exc

    return _normalize(data)


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
    logger.info("Gemini LR extraction: {} fields found -> {}", len(found), fields)
    return fields


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("null", "none", "") else None
