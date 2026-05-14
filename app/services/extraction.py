"""LR field extraction (OCR-tolerant).

Tuned against real EasyOCR output from a Mahindra Logistics LR. The
OCR is messy in predictable ways — digits get read as letters (O/U/Q
for 0, I/L for 1), period dots get read as 'h', columns from a table
get jumbled into adjacent lines. The patterns below are deliberately
permissive: better to extract something the user can correct than
nothing at all. Anything still wrong, the user fixes in the form.

`extract_lr_fields()` is the single entry point — body can be swapped
for an LLM call later without touching callers.
"""
from __future__ import annotations

import re
from typing import Any

from loguru import logger


# ---------- Patterns ----------

# Vehicle plate — match the LABEL ("Vehicle No"/"Vahicle No"/"Vehicle Mo"),
# then grab the next alphanumeric token. Allow OCR confusions in plate chars.
_VEHICLE_LABEL_RE = re.compile(
    r"V[ae]hicle\s*[NM]o[\.\s:]*\n?\s*([A-Z][A-Z0-9OUQILoq]{7,11})",
    re.IGNORECASE,
)

# G.C.N. (LR) number — period dots often get read as 'h', so allow ANY
# non-word char between G / C / N letters. Optional 'No' suffix.
_GCN_RE = re.compile(
    r"G\W?C\W?[NhH]\W?(?:[Nho]\W?)?[oO0]?\W*\n?\s*(\d{6,12})",
    re.IGNORECASE,
)

# Date — first dd-Mon-yyyy on the LR (printed top-right) wins.
_DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2}[-/](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[-/]\d{2,4})\b", re.IGNORECASE),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{2}[/\-\.]\d{2}[/\-\.]\d{4})\b"),
]

# From-city label — sometimes appears on its own line, with the city
# on the next line.
_FROM_RE = re.compile(r"\bFrom\s*[:\-]?\s*\n?\s*([A-Z][a-zA-Z]{3,20})\b")

# Quantity — original column header is "No of Pkg" / "Qty"; OCR often
# mangles this badly so we keep it loose and accept user override.
_QTY_RE = re.compile(
    r"(?:N[o0]\s*0?[fF]?\s*P[krg]+|Qty\.?|Quantity)\s*[:\-]?\s*\n?\s*(\d{1,4}\s*(?:Units?|Nos?|Pcs)?)",
    re.IGNORECASE,
)


# ---------- Helpers ----------

_DIGIT_LOOKALIKES = {"O": "0", "U": "0", "Q": "0", "D": "0",
                     "I": "1", "L": "1"}


def _normalize_plate(s: str) -> str:
    """OCR fix-up for an Indian vehicle plate.

    Real plate format: 2 letters (RTO) + 2 digits (state) + 1-3 letters
    (series) + 4 digits. We only fix lookalikes in the *digit* positions
    so we don't accidentally rewrite real letters.
    """
    s = re.sub(r"[\s\-]", "", s.upper())
    if len(s) < 9 or len(s) > 11:
        return s
    chars = list(s)
    for i in (2, 3):
        if chars[i] in _DIGIT_LOOKALIKES:
            chars[i] = _DIGIT_LOOKALIKES[chars[i]]
    for i in range(len(chars) - 4, len(chars)):
        if chars[i] in _DIGIT_LOOKALIKES:
            chars[i] = _DIGIT_LOOKALIKES[chars[i]]
    return "".join(chars)


def _first_date(text: str) -> str | None:
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip()
    return None


def _extract_delivery_order_nos(text: str) -> list[str]:
    """Find the line containing the comma-separated delivery order numbers.

    Strategy: scan every line, pick the one that's predominantly digits
    + commas (which is what the DO column contains). Skip lines with
    too many letters (those are remarks/chassis numbers).
    """
    for line in text.split("\n"):
        s = line.strip()
        if not s or "," not in s:
            continue
        digits = sum(c.isdigit() for c in s)
        letters = sum(c.isalpha() for c in s)
        if digits < 10 or letters > digits // 2:
            continue
        nums = re.findall(r"\d{7,12}", s)
        if nums:
            return nums
    return []


def _clean_city(name: str | None) -> str | None:
    """Validate a captured city token (used for From / Destination)."""
    if not name:
        return None
    cleaned = re.sub(r"\s+", " ", name).strip().rstrip(",.;:")
    if not cleaned or not cleaned[0].isupper():
        return None
    if len(re.sub(r"[^A-Za-z]", "", cleaned)) < 4:
        return None
    lc = cleaned.lower()
    blacklist = [
        "ba ", "gst", "service", "mode", "pin", "address",
        "bill", "pay", "consignor", "consignee", "be ", "the ",
        "owners", "owner", "risk", "rs.", "rs ", "freight",
        "tax", "invoice", "note", "remarks", "delivery", "order",
        "mahindra", "swaraj", "logistics", "limited",
    ]
    if any(b in lc for b in blacklist):
        return None
    return cleaned


# ---------- Public API ----------

def extract_lr_fields(text: str) -> dict[str, Any]:
    """Extract structured LR fields from OCR text.

    Returns canonical keys with None for misses so the UI can display
    a consistent set of input boxes.
    """
    if not text:
        return {}

    # Vehicle
    m = _VEHICLE_LABEL_RE.search(text)
    vehicle = _normalize_plate(m.group(1)) if m else None

    # GCN
    m = _GCN_RE.search(text)
    gcn_no = m.group(1) if m else None

    # From city (To city deliberately left for user — OCR rarely keeps
    # the To label adjacent to its city on this LR layout)
    m = _FROM_RE.search(text)
    from_city = _clean_city(m.group(1) if m else None)

    # Qty
    m = _QTY_RE.search(text)
    qty = m.group(1).strip() if m else None

    fields: dict[str, Any] = {
        "vehicle_no": vehicle,
        "gcn_no": gcn_no,
        "gcn_date": _first_date(text),
        "from_city": from_city,
        "destination": None,  # user types in — OCR layout for "To" too unreliable
        "delivery_order_nos": _extract_delivery_order_nos(text),
        "qty": qty,
        "delivery_date": None,  # hand-written; user types in
    }

    found = [k for k, v in fields.items() if v]
    logger.debug("LR extraction: found {} keys -> {}", len(found), fields)
    return fields


# Backwards-compatible alias
def extract_fields(text: str) -> dict[str, Any]:
    return extract_lr_fields(text)
