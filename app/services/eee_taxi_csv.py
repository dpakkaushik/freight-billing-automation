"""Parse the EEE-Taxi trip CSV for batch invoice generation.

Column matching is by header name (not position — order does not matter).
Leading/trailing spaces, internal newlines, and spaces around slashes are
normalised before matching so Excel-wrapped headers work transparently.

Required headers (only those needed for invoice creation):
    Date, Car No, DS no/Route No, Guest Name, Pickup Location, Drop Location,
    Pick up Time, Drop Time, Trip Duration, Total kms,
    Package, Trip Fare, Toll/MCD,
    Entity    <- billing entity name (GSTIN looked up from internal master)

Optional headers (used when present, silently ignored when absent):
    Entity Gst    <- GSTIN; if present takes precedence over name-based lookup
    Kms Helper    <- "A+B" billing/dead-run km split; first part used for rental
    Drop Zone     <- city/zone name; used to pick correct state GSTIN

Rental-classification:
    Package = 920  -> 4:40 rental (base Rs 920)
    Package = 1840 -> 8:80 rental (base Rs 1840)
    Any other value -> P2P fixed-fare trip

If any required header is missing the parser raises ValueError listing the
missing names so the caller can return a 400 and ask for re-upload.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from loguru import logger

# ── Required column names (exact, case-sensitive after normalisation) ─────────
REQUIRED_HEADERS: frozenset[str] = frozenset({
    "date",
    "car no",
    "ds no/route no",
    "guest name",
    "pickup location",
    "drop location",
    "pick up time",
    "drop time",
    "trip duration",
    "total kms",
    "package",
    "trip fare",
    "toll/mcd",
    "entity",   # billing entity name; GSTIN resolved from internal master
})

RENTAL_PACKAGES: frozenset[int] = frozenset({920, 1840})


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


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_eee_taxi_csv(file_bytes: bytes) -> tuple[list[str], list[EeeTaxiRow]]:
    """Parse CSV bytes → (original_headers, rows).

    original_headers is the raw header row as-is (for round-trip output).
    Raises ValueError with a descriptive message if required headers are
    missing so the caller can return a 400 and ask for re-upload.
    """
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))

    rows_out: list[EeeTaxiRow] = []
    header_map: dict[str, int] = {}
    original_headers: list[str] = []
    idx = 0

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
            missing = sorted(REQUIRED_HEADERS - header_map.keys())
            if missing:
                raise ValueError(
                    "CSV is missing required column(s): "
                    + ", ".join(f'"{m}"' for m in missing)
                    + ". Please add the missing columns and re-upload."
                )
            continue

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

        entity_name = col("entity")
        if not entity_name:
            logger.debug("Row {}: skipping — Entity blank", idx)
            idx += 1
            continue

        # Resolve GSTIN: CSV column if valid, otherwise internal name lookup
        drop_zone    = col("drop zone")
        client_gstin = col("entity gst").upper().replace(" ", "")
        if not client_gstin or len(client_gstin) < 10 or not client_gstin[0].isdigit():
            from app.services.eee_taxi_clients import UnknownClientError, lookup_client_by_name
            try:
                _rec     = lookup_client_by_name(entity_name, drop_zone)
                client_gstin = _rec.gstin
            except UnknownClientError:
                logger.warning("Row {}: unknown entity {!r}, skipping", idx, entity_name)
                idx += 1
                continue

        try:
            trip_date = _parse_date(col("date"))
        except ValueError:
            logger.warning("Row {}: bad date {!r}, skipping", idx, col("date"))
            idx += 1
            continue

        # ── Booking type ──────────────────────────────────────────────────────
        pkg_str = col("package").strip()
        try:
            package = int(float(pkg_str)) if pkg_str else 0
        except (ValueError, InvalidOperation):
            package = 0
        booking_type = "rental" if package in RENTAL_PACKAGES else "p2p"

        # ── Kms: total odometer vs billing kms (first part of Kms Helper) ─────
        total_kms_val = _dec(col("total kms"))
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
        toll      = _dec(col("toll/mcd"))
        tax_base  = trip_fare
        total     = _dec(col("total trip fare"))

        rows_out.append(EeeTaxiRow(
            row_index=idx,
            trip_date=trip_date,
            car_no=col("car no"),
            route_no=col("ds no/route no"),
            entity_name=entity_name,
            client_gstin=client_gstin,
            guest_name=col("guest name"),
            pickup_location=col("pickup location"),
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
        ))
        idx += 1

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
