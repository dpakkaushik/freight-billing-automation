"""Ollama vision-LLM extractor for Mahindra Logistics LR images.

Sends a base64-encoded image to a locally-running Ollama instance and
asks the model to return structured LR fields as JSON.  Raises
OllamaUnavailable when the service is not reachable or the model has not
been pulled, so callers can fall back to EasyOCR gracefully.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import random
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from PIL import Image

from app.config import settings


class OllamaUnavailable(Exception):
    """Raised when Ollama is not running, the model is not pulled, or inference fails."""


_PROMPT = (
    "You are reading a Mahindra Logistics Goods Consignment Note (GCN / LR). "
    "Extract exactly the fields below. Read the image carefully.\n\n"

    "FIELD LOCATIONS on this specific form:\n"
    "- vehicle_no: top-left area, labeled 'Vehicle No.' e.g. NL01AK0496\n"
    "- gcn_no: top-right area, labeled 'G.C.N.No.' — a 9-digit number e.g. 112032145. "
    "  This is DIFFERENT from the Delivery Order No.\n"
    "- gcn_date: top-right area, labeled 'Date.' next to G.C.N.No. e.g. 07-Apr-2026\n"
    "- from_city: right side, small box labeled 'From' — just the city name e.g. Rajkot\n"
    "- destination: right side, small box labeled 'To' — just the city name e.g. Gabhana. "
    "  Do NOT use the full consignee address.\n"
    "- qty: table column 'No of Pkg' e.g. 9 Units\n"
    "- delivery_order_nos: table column labeled 'Delivery Order No' (NOT the GCN No). "
    "  These are 10-digit numbers e.g. 7412304508. There may be multiple separated by "
    "  commas. Return ALL as a JSON array of strings.\n\n"

    "CRITICAL RULES:\n"
    "- delivery_order_nos MUST come only from the 'Delivery Order No' table column. "
    "  NEVER put the GCN number here.\n"
    "- destination is ONLY the city from the 'To' box, not from the address block.\n"
    "- Do NOT invent or guess any value not visible in the image.\n"
    "- delivery_date: look in the 'Proof of Delivery' box at the bottom-right, "
    "labeled 'Date:' — it is handwritten, e.g. 15/04/2026. Extract it if readable, "
    "otherwise null.\n"
    "- Return ONLY a JSON object, no explanation, no markdown.\n\n"

    "{\n"
    '  "vehicle_no": "<read from image>",\n'
    '  "gcn_no": "<read from image>",\n'
    '  "gcn_date": "<read from image>",\n'
    '  "from_city": "<read from image>",\n'
    '  "destination": "<read from image>",\n'
    '  "qty": "<read from image>",\n'
    '  "delivery_order_nos": ["<read from image>"],\n'
    '  "delivery_date": "<read from image or null>"\n'
    "}"
)

_MAX_SIDE = 1536  # pixels; higher res helps read small text on document forms


def _prepare_image(path: Path) -> str:
    """Resize to _MAX_SIDE on the longest side, convert to JPEG, return base64 string."""
    img = Image.open(path)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > _MAX_SIDE:
        scale = _MAX_SIDE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def extract_lr_fields_ollama(image_path: str | Path) -> dict[str, Any]:
    """Extract LR fields by calling a local Ollama vision model.

    Returns the same dict shape as extraction.extract_lr_fields():
      vehicle_no, gcn_no, gcn_date, from_city, destination,
      delivery_order_nos (list[str]), qty, delivery_date (always None).

    Raises:
        OllamaUnavailable: service unreachable, model not pulled, or inference error.
        ValueError: image file missing.
    """
    path = Path(image_path)
    if not path.exists():
        raise ValueError(f"Image not found: {path}")

    image_b64 = _prepare_image(path)
    img_hash = hashlib.md5(image_b64[:500].encode()).hexdigest()[:8]
    logger.info("Sending image to Ollama | file={} hash={} size={}chars",
                path.name, img_hash, len(image_b64))

    payload = {
        "model": settings.ollama_model,
        "messages": [
            {
                "role": "user",
                "content": _PROMPT,
                "images": [image_b64],
            }
        ],
        "format": "json",
        "stream": False,
        "keep_alive": 0,
        "options": {
            "temperature": 0,
            "seed": random.randint(1, 999999),  # breaks Ollama response cache
        },
    }

    try:
        resp = httpx.post(
            f"{settings.ollama_base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=180.0,
        )
    except httpx.ConnectError as exc:
        raise OllamaUnavailable(
            f"Cannot connect to Ollama at {settings.ollama_base_url}. "
            "Make sure Ollama is running."
        ) from exc
    except httpx.TimeoutException as exc:
        raise OllamaUnavailable("Ollama request timed out.") from exc

    if resp.status_code == 404:
        raise OllamaUnavailable(
            f"Model '{settings.ollama_model}' not found in Ollama. "
            f"Run: ollama pull {settings.ollama_model}"
        )
    if resp.status_code >= 500:
        raise OllamaUnavailable(
            f"Ollama returned {resp.status_code} (model may be out of memory). "
            f"Details: {resp.text[:300]}"
        )
    resp.raise_for_status()

    raw_response = resp.json().get("message", {}).get("content", "")
    logger.debug("Ollama raw response ({} chars): {}", len(raw_response), raw_response[:300])

    try:
        data = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        logger.warning("Ollama returned invalid JSON: {}", raw_response[:500])
        raise OllamaUnavailable(f"Ollama returned non-JSON response: {exc}") from exc

    return _normalize(data)


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Map Ollama's response to the canonical fields dict."""
    do_nos_raw = data.get("delivery_order_nos") or []
    if isinstance(do_nos_raw, str):
        # Model occasionally returns a comma-separated string instead of an array.
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
    logger.info("Ollama LR extraction: {} fields found -> {}", len(found), fields)
    return fields


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("null", "none", "") else None
