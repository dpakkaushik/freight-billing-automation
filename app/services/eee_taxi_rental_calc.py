"""Rental fare calculation engine for EEE-Taxi batch invoicing.

All rates come from a ``RateCard`` (see ``app/services/eee_taxi_rates.py``),
which admins edit on the EEE-Taxi -> Rate Card page. Rules, with the
defaults in brackets:

Packages: small [950 = 4 hrs / 40 kms], large [1900 = 8 hrs / 80 kms].
    The CSV ``Package`` value must equal one of the two package fares.

Auto-upgrade small -> large:
    duration >= upgrade_minutes [6 h 31 m]  OR  total_kms (floor) >= upgrade_kms [61]

Extra time: extra_hour_rate [Rs 200] per extra hour above base hours.
    Leftover minutes >= partial_hour_minutes [31] count as one full hour.

Extra km: extra_km_rate [Rs 17] per km above base kms.
    Flooring: 41.6 km -> 41 km (meters are ignored).

Night charge: night_charge [Rs 265] fixed if pickup or drop hour falls in
    the night window [23:00 - 05:00).
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, replace as dc_replace
from decimal import Decimal
from typing import Optional

from loguru import logger

from app.services.eee_taxi_csv import EeeTaxiRow
from app.services.eee_taxi_fare_check import check_row
from app.services.eee_taxi_rates import RateCard

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
    night_charge:       Decimal   # 0 or rate card night charge
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

def calculate_rental_fare(row: EeeTaxiRow, rates: RateCard) -> RentalFareResult:
    """Compute the full rental fare breakdown for a single CSV row."""
    kms_floor    = int(row.billing_kms)  # sum of Kms Helper parts (trip + hub)
    duration_min = _parse_duration_minutes(row.trip_duration_str)

    # ── Auto-upgrade small → large package ───────────────────────────────────
    effective_pkg = row.package
    if effective_pkg == rates.small_fare:
        if duration_min >= rates.upgrade_minutes or kms_floor >= rates.upgrade_kms:
            effective_pkg = rates.large_fare
            logger.debug(
                "Row {}: auto-upgrade {}→{} (duration={}min kms={})",
                row.row_index, rates.small_fare, rates.large_fare, duration_min, kms_floor,
            )

    base_hours, base_kms = rates.package_base(effective_pkg)

    # ── Extra km ──────────────────────────────────────────────────────────────
    extra_km       = max(0, kms_floor - base_kms)
    extra_km_charge = Decimal(extra_km) * rates.extra_km_rate

    # ── Extra time ────────────────────────────────────────────────────────────
    base_minutes     = base_hours * 60
    excess_minutes   = max(0, duration_min - base_minutes)
    full_extra_hours = excess_minutes // 60
    partial_minutes  = excess_minutes % 60
    if partial_minutes >= rates.partial_hour_minutes:
        full_extra_hours += 1
    extra_time_charge = Decimal(full_extra_hours) * rates.extra_hour_rate

    # ── Night charge ──────────────────────────────────────────────────────────
    pickup_h     = _parse_hour(row.pickup_time_str)
    drop_h       = _parse_hour(row.drop_time_str)
    is_night     = rates.is_night_hour(pickup_h) or rates.is_night_hour(drop_h)
    night_charge = rates.night_charge if is_night else Decimal("0")

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
    rates: RateCard,
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
            fare = calculate_rental_fare(row, rates).trip_fare
        result.append(dc_replace(row, trip_fare=fare, tax_base=fare, total_amount=Decimal("0")))
    return result


# ── Review CSV generation & parsing ──────────────────────────────────────────

_REVIEW_HEADERS = [
    "Row", "Date", "Cab No", "DS no/Route No", "Guest Name",
    "Pickup Address", "Drop Location",
    "Pick up Time", "Drop Time", "Trip Duration", "Billing kms",
    "CSV_Package", "Effective_Package",
    "Calc_Extra_Km", "Calc_Extra_Km_Charge",
    "Calc_Extra_Time_Hours", "Calc_Extra_Time_Charge",
    "Calc_Night_Charge", "Calc_Trip_Fare",
    "CSV_Trip_Fare", "Discrepancy", "Notes",
    "Entity", "Entity Gst",
]


def generate_rental_review_csv(rows: list[EeeTaxiRow], rates: RateCard) -> bytes:
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
        res = calculate_rental_fare(row, rates)
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
    "Pickup Area", "Drop Area", "Card Fare", "Fare Check",
]


def generate_full_calc_csv(
    rows: list[EeeTaxiRow], original_headers: list[str], rates: RateCard,
) -> bytes:
    """Generate enriched CSV: all original columns preserved + calculated columns appended.

    Extra time is calculated from the Trip Duration column (H:MM format).
    The only column users need to edit is Calc_Trip_Fare — re-upload to override.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(original_headers + _CALC_EXTRA_HEADERS)

    for row in rows:
        if row.booking_type == "rental":
            res = calculate_rental_fare(row, rates)
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

        if row.booking_type == "p2p":
            chk = check_row(row, rates)
            fare_cols = [chk.from_area or "", chk.to_area or "",
                         str(chk.card_fare) if chk.card_fare is not None else "",
                         "OK" if chk.status == "ok" else chk.message]
        else:
            fare_cols = ["", "", "", ""]

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
            + fare_cols
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
    rates: RateCard,
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
            result.append(dc_replace(row, trip_fare=fare, tax_base=fare, total_amount=Decimal("0")))
        elif row.booking_type == "rental":
            fare = calculate_rental_fare(row, rates).trip_fare
            result.append(dc_replace(row, trip_fare=fare, tax_base=fare, total_amount=Decimal("0")))
        else:
            result.append(row)
    return result
