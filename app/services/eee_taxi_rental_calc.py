"""Rental fare calculation engine for EEE-Taxi batch invoicing.

Packages:
    920  = 4:40  (4 hrs, 40 kms base)
    1840 = 8:80  (8 hrs, 80 kms base)

Auto-upgrade 4:40 → 8:80:
    duration >= 6 h 31 m  OR  total_kms (floor) >= 61

Extra time charge:
    Rs 180 per extra hour above base hours.
    Rounding: remaining minutes >= 31 → count as one full extra hour.

Extra km charge:
    Rs 15.5 per km above base kms.
    Flooring: 41.6 km → 41 km  (int() truncation — meters are ignored).

Night charge:
    Rs 250 fixed if pickup hour OR drop hour falls in [23:00, 05:00).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace as dc_replace
from decimal import Decimal
from typing import Optional

from loguru import logger

from app.services.eee_taxi_csv import EeeTaxiRow

# ── Constants ─────────────────────────────────────────────────────────────────

EXTRA_KM_RATE      = Decimal("15.5")
EXTRA_TIME_RATE    = Decimal("180")
NIGHT_CHARGE_FIXED = Decimal("250")

_PKG_BASE: dict[int, tuple[int, int]] = {
    920:  (4, 40),
    1840: (8, 80),
}
_UPGRADE_MINUTES = 6 * 60 + 31   # 391 — threshold for 4:40 → 8:80 upgrade
_UPGRADE_KMS     = 61             # km threshold for upgrade


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RentalFareResult:
    effective_package:  int
    base_hours:         int
    base_kms:           int
    kms_floor:          int       # int(total_kms)
    extra_km:           int
    extra_km_charge:    Decimal
    excess_minutes:     int       # total minutes above base
    extra_time_hours:   int       # hours charged (post rounding)
    extra_time_charge:  Decimal
    night_charge:       Decimal   # 0 or 250
    trip_fare:          Decimal   # base + extra km + extra time + night


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_hour(time_str: str) -> Optional[int]:
    """Return 0-23 hour from strings like '8:00 AM', '22:00', '10:00 PM'."""
    if not time_str or not time_str.strip():
        return None
    s = time_str.strip().upper()
    is_pm = s.endswith("PM")
    is_am = s.endswith("AM")
    cleaned = s.replace("AM", "").replace("PM", "").strip()
    parts = cleaned.replace(".", ":").split(":")
    try:
        hour = int(parts[0])
        if is_pm and hour != 12:
            hour += 12
        elif is_am and hour == 12:
            hour = 0
        return hour % 24
    except (ValueError, IndexError):
        return None


def _is_night(hour: Optional[int]) -> bool:
    """True if the hour is between 23:00 and 04:59 (11 PM – 5 AM)."""
    if hour is None:
        return False
    return hour >= 23 or hour < 5


def _parse_duration_minutes(s: str) -> int:
    """Parse 'H:MM' or 'HH:MM' → total minutes.  Returns 0 on failure."""
    if not s or not s.strip():
        return 0
    parts = s.strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return h * 60 + m
    except (ValueError, IndexError):
        return 0


# ── Core fare calculation ─────────────────────────────────────────────────────

def calculate_rental_fare(row: EeeTaxiRow) -> RentalFareResult:
    """Compute the full rental fare breakdown for a single CSV row."""
    kms_floor    = int(row.billing_kms)  # sum of Kms Helper parts (trip + hub)
    duration_min = _parse_duration_minutes(row.trip_duration_str)

    # ── Auto-upgrade 4:40 → 8:80 ─────────────────────────────────────────────
    effective_pkg = row.package
    if effective_pkg == 920:
        if duration_min >= _UPGRADE_MINUTES or kms_floor >= _UPGRADE_KMS:
            effective_pkg = 1840
            logger.debug(
                "Row {}: auto-upgrade 4:40→8:80 (duration={}min kms={})",
                row.row_index, duration_min, kms_floor,
            )

    base_hours, base_kms = _PKG_BASE.get(effective_pkg, (8, 80))

    # ── Extra km ──────────────────────────────────────────────────────────────
    extra_km       = max(0, kms_floor - base_kms)
    extra_km_charge = Decimal(extra_km) * EXTRA_KM_RATE

    # ── Extra time ────────────────────────────────────────────────────────────
    base_minutes     = base_hours * 60
    excess_minutes   = max(0, duration_min - base_minutes)
    full_extra_hours = excess_minutes // 60
    partial_minutes  = excess_minutes % 60
    if partial_minutes >= 31:
        full_extra_hours += 1
    extra_time_charge = Decimal(full_extra_hours) * EXTRA_TIME_RATE

    # ── Night charge ──────────────────────────────────────────────────────────
    pickup_h     = _parse_hour(row.pickup_time_str)
    drop_h       = _parse_hour(row.drop_time_str)
    night_charge = NIGHT_CHARGE_FIXED if (_is_night(pickup_h) or _is_night(drop_h)) else Decimal("0")

    trip_fare = Decimal(effective_pkg) + extra_km_charge + extra_time_charge + night_charge

    return RentalFareResult(
        effective_package=effective_pkg,
        base_hours=base_hours,
        base_kms=base_kms,
        kms_floor=kms_floor,
        extra_km=extra_km,
        extra_km_charge=extra_km_charge,
        excess_minutes=excess_minutes,
        extra_time_hours=full_extra_hours,
        extra_time_charge=extra_time_charge,
        night_charge=night_charge,
        trip_fare=trip_fare,
    )


def apply_rental_fares(
    rows: list[EeeTaxiRow],
    overrides: dict[int, Decimal],
) -> list[EeeTaxiRow]:
    """Return rows with rental trip_fare replaced by calculated (or overridden) value.

    P2P rows are passed through unchanged.
    Rental rows get their trip_fare set to overrides[row_index] if present,
    otherwise to the system-calculated fare.
    tax_base is kept in sync with trip_fare (toll stays separate).
    """
    result: list[EeeTaxiRow] = []
    for row in rows:
        if row.booking_type != "rental":
            result.append(row)
            continue
        if row.row_index in overrides:
            fare = overrides[row.row_index]
        else:
            fare = calculate_rental_fare(row).trip_fare
        result.append(dc_replace(row, trip_fare=fare, tax_base=fare))
    return result


# ── Review CSV generation & parsing ──────────────────────────────────────────

_REVIEW_HEADERS = [
    "Row", "Date", "Car No", "DS no/Route No", "Guest Name",
    "Pickup Location", "Drop Location",
    "Pick up Time", "Drop Time", "Trip Duration", "Billing kms",
    "CSV_Package", "Effective_Package",
    "Calc_Extra_Km", "Calc_Extra_Km_Charge",
    "Calc_Extra_Time_Hours", "Calc_Extra_Time_Charge",
    "Calc_Night_Charge", "Calc_Trip_Fare",
    "CSV_Trip_Fare", "Discrepancy", "Notes",
    "Entity", "Entity Gst",
]


def generate_rental_review_csv(rows: list[EeeTaxiRow]) -> bytes:
    """Generate a review CSV for rental rows so the user can verify / edit fares.

    Edit ``Calc_Trip_Fare`` in the downloaded file and re-upload to override
    the system-calculated fare.  Leave it as-is to accept the calculation.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(_REVIEW_HEADERS)

    for row in rows:
        if row.booking_type != "rental":
            continue
        res = calculate_rental_fare(row)
        csv_fare = row.trip_fare
        discrepancy = ""
        if csv_fare != Decimal("0") and csv_fare != res.trip_fare:
            diff = csv_fare - res.trip_fare
            discrepancy = f"CSV={csv_fare} Calc={res.trip_fare} Diff={diff:+}"
        writer.writerow([
            row.row_index,
            row.trip_date.strftime("%d/%m/%Y"),
            row.car_no,
            row.route_no,
            row.guest_name,
            row.pickup_location,
            row.drop_location,
            row.pickup_time_str,
            row.drop_time_str,
            row.trip_duration_str,
            str(row.billing_kms),
            row.package,
            res.effective_package,
            res.extra_km,
            str(res.extra_km_charge),
            res.extra_time_hours,
            str(res.extra_time_charge),
            str(res.night_charge),
            str(res.trip_fare),       # ← user may edit this column
            str(csv_fare),
            discrepancy,
            "",                       # Notes — free text for user
            row.entity_name,
            row.client_gstin,
        ])

    return buf.getvalue().encode("utf-8-sig")   # BOM so Excel opens correctly


def parse_review_csv(file_bytes: bytes) -> dict[int, Decimal]:
    """Parse an uploaded review CSV → {row_index: Calc_Trip_Fare}.

    Only rows with a valid, positive ``Calc_Trip_Fare`` value are returned.
    The caller merges these into the row list via ``apply_rental_fares``.
    """
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    overrides: dict[int, Decimal] = {}
    for r in reader:
        try:
            row_idx  = int(str(r.get("Row", "")).strip())
            fare_str = str(r.get("Calc_Trip_Fare", "")).strip()
            if not fare_str:
                continue
            fare = Decimal(fare_str)
            if fare > 0:
                overrides[row_idx] = fare
        except Exception:
            continue
    return overrides


# ── Full calculation CSV (P2P + rental) ──────────────────────────────────────

_EEE_STATE_CODE = "07"   # Delhi — EEE-Taxi's home state

_CALC_EXTRA_HEADERS = [
    "Booking Type",
    "Effective Package", "Billing Kms",
    "Extra Kms", "Extra Kms Charge",
    "Extra Time Hrs", "Extra Time Charge", "Night Charge",
    "Calc_Trip_Fare",
    "GST Type", "CGST (9%)", "SGST (9%)", "IGST (18%)",
    "Calc Total",
]


def generate_full_calc_csv(rows: list[EeeTaxiRow], original_headers: list[str]) -> bytes:
    """Generate enriched CSV: all original columns preserved + calculated columns appended.

    Extra time is calculated from the Trip Duration column (H:MM format).
    The only column users need to edit is Calc_Trip_Fare — re-upload to override.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(original_headers + _CALC_EXTRA_HEADERS)

    for row in rows:
        if row.booking_type == "rental":
            res = calculate_rental_fare(row)
            eff_pkg           = str(res.effective_package)
            billing_kms       = str(row.billing_kms)
            extra_km          = str(res.extra_km)
            extra_km_charge   = str(res.extra_km_charge)
            extra_time_hrs    = str(res.extra_time_hours)
            extra_time_charge = str(res.extra_time_charge)
            night_charge      = str(res.night_charge)
            calc_fare         = res.trip_fare
        else:
            eff_pkg           = str(row.package)
            billing_kms       = str(row.billing_kms)
            extra_km          = ""
            extra_km_charge   = ""
            extra_time_hrs    = ""
            extra_time_charge = ""
            night_charge      = ""
            calc_fare         = row.trip_fare

        toll    = row.parking
        taxable = calc_fare + toll
        if row.client_gstin[:2] == _EEE_STATE_CODE:
            gst_type = "Local"
            cgst = (taxable * Decimal("0.09")).quantize(Decimal("0.01"))
            sgst = cgst
            igst = Decimal("0")
        else:
            gst_type = "Interstate"
            cgst = Decimal("0")
            sgst = Decimal("0")
            igst = (taxable * Decimal("0.18")).quantize(Decimal("0.01"))
        total = taxable + cgst + sgst + igst

        writer.writerow(
            list(row.raw_values)
            + [
                row.booking_type,
                eff_pkg,
                billing_kms,
                extra_km,
                extra_km_charge,
                extra_time_hrs,
                extra_time_charge,
                night_charge,
                str(calc_fare + toll),   # trip fare + Toll/MCD; GST and Calc Total derive from this
                gst_type,
                str(cgst),
                str(sgst),
                str(igst),
                str(total),
            ]
        )

    return buf.getvalue().encode("utf-8-sig")


def parse_calc_csv(file_bytes: bytes) -> dict[int, Decimal]:
    """Parse re-uploaded calculation CSV → {row_index: Calc_Trip_Fare}.

    Works for both P2P and rental rows.
    """
    text = file_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    overrides: dict[int, Decimal] = {}
    for r in reader:
        try:
            row_idx  = int(str(r.get("Row", "")).strip())
            fare_str = str(r.get("Calc_Trip_Fare", "")).strip()
            if not fare_str:
                continue
            fare = Decimal(fare_str)
            if fare > 0:
                overrides[row_idx] = fare
        except Exception:
            continue
    return overrides


def apply_fare_overrides(
    rows: list[EeeTaxiRow],
    overrides: dict[int, Decimal],
) -> list[EeeTaxiRow]:
    """Return rows with trip_fare set correctly for invoice generation.

    - If row_index is in overrides: use the override (user-edited value).
    - Rental rows without override: auto-calculate from package formula.
    - P2P rows without override: pass through unchanged (package = flat fare).
    """
    result: list[EeeTaxiRow] = []
    for row in rows:
        if row.row_index in overrides:
            fare = overrides[row.row_index]
            result.append(dc_replace(row, trip_fare=fare, tax_base=fare))
        elif row.booking_type == "rental":
            fare = calculate_rental_fare(row).trip_fare
            result.append(dc_replace(row, trip_fare=fare, tax_base=fare))
        else:
            result.append(row)
    return result
