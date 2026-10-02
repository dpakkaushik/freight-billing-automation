"""Parse the EEE-Taxi trip CSV for batch invoice generation.

Column matching is by header name (not position — order does not matter).
Leading/trailing spaces, internal newlines, and spaces around slashes are
normalised before matching so Excel-wrapped headers work transparently.

Required headers (only those needed for invoice creation):
    Date, Cab No, DS no/Route No, Guest Name, Pickup Address, Drop Location,
    Pick up Time, Drop Time, Trip Duration, Total kms,
    Package, Trip Fare, MCD, Parking, Toll,
    Entity, Entity Gst   <- both required; the GSTIN is never guessed

    Toll/MCD passthrough on the invoice = MCD + Parking + Toll.
    Total kms may be "A+B" (billing + dead run); the parts are summed.

Optional headers (used when present, silently ignored when absent):
    Kms Helper    <- legacy "A+B" km split column; summed for rentals
    Drop Zone     <- with Pickup Zone, checks P2P fares against the rate card
    Pickup Zone   <- city/zone name of the pickup (P2P fare check)

Any row with a blank, unknown or mismatched Entity / Entity Gst rejects the
whole file with a list of the offending rows, so nothing is billed to a guess.

Rental-classification (package fares come from the editable rate card):
    Package = small package fare (default 950)  -> 4:40 rental
    Package = large package fare (default 1900) -> 8:80 rental
    Any other value -> P2P fixed-fare trip

If any required header is missing the parser raises ValueError naming the
expected header (and the closest header found in the file) so the caller can
return a 400 and ask for re-upload.
"""
from __future__ import annotations

import csv
import difflib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from loguru import logger

from app.services.eee_taxi_clients import CLIENT_MASTER
from app.services.eee_taxi_rates import RateCard

# ── Required column names: normalised form -> header as the user should type it ─
REQUIRED_HEADERS: dict[str, str] = {
    "date": "Date",
    "cab no": "Cab No",
    "ds no/route no": "DS no/Route No",
    "guest name": "Guest Name",
    "pickup address": "Pickup Address",
    "drop location": "Drop Location",
    "pick up time": "Pick up Time",
    "drop time": "Drop Time",
    "trip duration": "Trip Duration",
    "total kms": "Total kms",
    "package": "Package",
    "trip fare": "Trip Fare",
    "mcd": "MCD",
    "parking": "Parking",
    "toll": "Toll",
    "entity": "Entity",
    "entity gst": "Entity Gst",
}

MAX_ROW_ERRORS_SHOWN = 20

@dataclass(frozen=True)
class EeeTaxiRow:
    row_index: int
    trip_date: date
    car_no: str
    route_no: str
    entity_name: str
    client_gstin: str
    guest_name: str
    pickup_location: str
    drop_location: str
    pickup_time_str: str    # e.g. "8:00 AM" or "20:00"
    drop_time_str: str      # e.g. "22:00" or "10:00 PM"
    trip_duration_str: str  # e.g. "6:30"
    total_kms: Decimal
    billing_kms: Decimal    # first part of Kms Helper for rentals; = total_kms for P2P
    package: int            # raw package value from CSV
    booking_type: str       # "p2p" | "rental"
    trip_fare: Decimal      # base fare (+ extras for rental) — no GST, no toll
    parking: Decimal        # Toll/MCD passthrough
    tax_base: Decimal       # = trip_fare  (GST applies to trip fare only)
    total_amount: Decimal   # from "Total Trip Fare" col if present, else 0 (pipeline computes)
    csv_extra_km: Decimal   # from CSV for cross-check
    csv_extra_time: Decimal # from CSV for cross-check
    csv_night_charge: Decimal  # from CSV for cross-check
    raw_values: tuple = ()  # all original CSV cell values in column order
    pickup_zone: str = ""   # "Pickup Zone" column; matched to rate-card route fares
    drop_zone: str = ""     # "Drop Zone" column


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize_header(h: str) -> str:
    """Collapse whitespace (including newlines), fix slash spacing, lowercase."""
    s = re.sub(r"\s+", " ", h).strip()
    s = re.sub(r"\s*/\s*", "/", s)   # "Km/Extra/ Km Charges" → "Km/Extra/Km Charges"
    return s.lower()


def _parse_date(s: str) -> date:
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {s!r}")


def _dec(s: str) -> Decimal:
    if not s or not s.strip() or s.strip().startswith("#"):
        return Decimal("0")
    try:
        return Decimal(s.strip().replace(",", ""))
    except InvalidOperation:
        return Decimal("0")


def _kms(s: str) -> Decimal:
    """Total kms cell: plain number, or "A+B" (billing + dead run) summed."""
    return sum((_dec(part) for part in s.split("+")), Decimal("0"))


def _header_mismatch_message(missing: list[str], raw_headers: list[str]) -> str:
    """Name each expected header, plus the closest header found in the file."""
    # normalised -> header as the user typed it (newlines collapsed)
    unmatched = {
        _normalize_header(h): " ".join(h.split())
        for h in raw_headers
        if _normalize_header(h) and _normalize_header(h) not in REQUIRED_HEADERS
    }
    lines = []
    for norm in missing:
        expected = REQUIRED_HEADERS[norm]
        close = difflib.get_close_matches(norm, list(unmatched), n=1, cutoff=0.75)
        if not close:   # short forms, e.g. "GST" for "Entity Gst"
            close = [h for h in unmatched if len(h) >= 3 and h in norm][:1]
        if close:
            lines.append(f'  - Found "{unmatched[close[0]]}" -> rename it to "{expected}"')
        else:
            lines.append(f'  - Missing column "{expected}"')
    shown = [" ".join(h.split()) for h in raw_headers if h.strip()]
    for name in dict.fromkeys(shown):
        if shown.count(name) > 1:
            lines.append(f'  - "{name}" appears {shown.count(name)} times; rename the extra column')
    return (
        "Header name mismatch. Please fix these column headers and re-upload:\n"
        + "\n".join(lines)
    )


def _entity_error(entity_name: str, gstin: str) -> str:
    """Empty string if Entity / Entity Gst are valid, else what is wrong."""
    if not entity_name:
        return "Entity is blank"
    if not gstin:
        return "Entity Gst is blank"
    record = CLIENT_MASTER.get(gstin)
    if record is None:
        return f"Entity Gst {gstin} is not in the company list"
    if record.entity_name.lower().strip() != entity_name.lower().strip():
        return f"Entity Gst {gstin} belongs to {record.entity_name}, not {entity_name!r}"
    return ""


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_eee_taxi_csv(
    file_bytes: bytes, rates: RateCard,
) -> tuple[list[str], list[EeeTaxiRow]]:
    """Parse CSV bytes → (original_headers, rows).

    original_headers is the raw header row as-is (for round-trip output).
    Raises ValueError with a descriptive message if required headers are
    missing, or if any row has a bad Entity / Entity Gst / Date, so the
    caller can return a 400 and ask for re-upload.
    """
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))

    rows_out: list[EeeTaxiRow] = []
    header_map: dict[str, int] = {}
    original_headers: list[str] = []
    row_errors: list[str] = []
    idx = 0
    sheet_row = 1   # spreadsheet row number, header = row 1

    for raw in reader:
        # ── Build header map from the first non-empty row ─────────────────────
        if not header_map:
            if not any(c.strip() for c in raw):
                continue                          # skip truly blank rows
            original_headers = [h.strip() for h in raw]
            for i, h in enumerate(raw):
                norm = _normalize_header(h)
                if norm and norm not in header_map:
                    header_map[norm] = i          # keep FIRST occurrence

            # ── Validate required headers ─────────────────────────────────────
            missing = [h for h in REQUIRED_HEADERS if h not in header_map]
            if missing:
                raise ValueError(_header_mismatch_message(missing, raw))
            continue

        sheet_row += 1

        # ── Skip blank / summary rows ─────────────────────────────────────────
        if not any(c.strip() for c in raw):
            continue
        if raw[0].strip().lower() in ("grand total", "total", ""):
            continue

        def col(name: str, _raw: list = raw) -> str:
            i = header_map.get(name)
            if i is None or i >= len(_raw):
                return ""
            return _raw[i].strip()

        entity_name  = col("entity")
        client_gstin = col("entity gst").upper().replace(" ", "")
        drop_zone    = col("drop zone")
        label        = f"Row {sheet_row} ({col('guest name') or 'no guest name'})"

        entity_problem = _entity_error(entity_name, client_gstin)
        if entity_problem:
            row_errors.append(f"{label}: {entity_problem}")
            continue
        try:
            trip_date = _parse_date(col("date"))
        except ValueError:
            row_errors.append(f"{label}: cannot read Date {col('date')!r}")
            continue

        # ── Booking type ──────────────────────────────────────────────────────
        pkg_str = col("package").strip()
        try:
            package = int(float(pkg_str)) if pkg_str else 0
        except (ValueError, InvalidOperation):
            package = 0
        booking_type = "rental" if package in rates.rental_packages else "p2p"

        # ── Kms: total odometer vs billing kms (first part of Kms Helper) ─────
        total_kms_val = _kms(col("total kms"))
        kms_helper    = col("kms helper")  # e.g. "70+20" → sum=90 used for billing
        if booking_type == "rental" and kms_helper:
            try:
                billing_kms_val = sum(Decimal(p.strip()) for p in kms_helper.split("+") if p.strip())
            except Exception:
                billing_kms_val = total_kms_val
        else:
            billing_kms_val = total_kms_val

        # ── Fare columns ──────────────────────────────────────────────────────
        trip_fare = _dec(col("trip fare"))
        toll      = _dec(col("mcd")) + _dec(col("parking")) + _dec(col("toll"))
        tax_base  = trip_fare
        total     = _dec(col("total trip fare"))

        rows_out.append(EeeTaxiRow(
            row_index=idx,
            trip_date=trip_date,
            car_no=col("cab no"),
            route_no=col("ds no/route no"),
            entity_name=entity_name,
            client_gstin=client_gstin,
            guest_name=col("guest name"),
            pickup_location=col("pickup address"),
            drop_location=col("drop location"),
            pickup_time_str=col("pick up time"),
            drop_time_str=col("drop time"),
            trip_duration_str=col("trip duration"),
            total_kms=total_kms_val,
            billing_kms=billing_kms_val,
            package=package,
            booking_type=booking_type,
            trip_fare=trip_fare,
            parking=toll,
            tax_base=tax_base,
            total_amount=total,
            csv_extra_km=_dec(col("km/extra/km charges")),
            csv_extra_time=_dec(col("time extra/time charges")),
            csv_night_charge=_dec(col("night charge 11pm to 5am")),
            raw_values=tuple(raw),
            pickup_zone=col("pickup zone"),
            drop_zone=drop_zone,
        ))
        idx += 1

    if row_errors:
        shown = row_errors[:MAX_ROW_ERRORS_SHOWN]
        more = len(row_errors) - len(shown)
        raise ValueError(
            "Some rows need fixing before invoices can be made. "
            "Nothing was billed; please correct these and re-upload:\n"
            + "\n".join(f"  - {e}" for e in shown)
            + (f"\n  ...and {more} more" if more else "")
        )

    rental_count = sum(1 for r in rows_out if r.booking_type == "rental")
    p2p_count    = sum(1 for r in rows_out if r.booking_type == "p2p")
    logger.info(
        "EEE-Taxi CSV parsed: {} rows  ({} p2p, {} rental)",
        len(rows_out), p2p_count, rental_count,
    )
    return original_headers, rows_out


# ── Utilities used by pipeline / API ─────────────────────────────────────────


def financial_year(d: date) -> str:
    if d.month >= 4:
        return f"{str(d.year)[2:]}-{str(d.year + 1)[2:]}"
    return f"{str(d.year - 1)[2:]}-{str(d.year)[2:]}"


def format_invoice_no(fy: str, suffix: int) -> str:
    return f"DL/HO/{fy}/{suffix:04d}"
