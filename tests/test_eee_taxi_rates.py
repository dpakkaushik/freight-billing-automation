"""Tests for the editable EEE-Taxi rate card and rental fare calculation."""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.eee_taxi_csv import EeeTaxiRow, parse_eee_taxi_csv
from app.services.eee_taxi_rates import DEFAULT_RATE_CARD, RateCardIn, rate_card_to_dict
from app.services.eee_taxi_rental_calc import calculate_rental_fare

RATES = DEFAULT_RATE_CARD


def _row(package=950, duration="4:00", kms="40", pickup="10:00 AM", drop="2:00 PM") -> EeeTaxiRow:
    return EeeTaxiRow(
        row_index=1, trip_date=date(2026, 9, 1), car_no="DL1", route_no="R1",
        entity_name="PRICE WATERHOUSE LLP", client_gstin="06AAEFP3641G1ZG",
        guest_name="Guest", pickup_location="A", drop_location="B",
        pickup_time_str=pickup, drop_time_str=drop, trip_duration_str=duration,
        total_kms=Decimal(kms), billing_kms=Decimal(kms), package=package,
        booking_type="rental", trip_fare=Decimal("0"), parking=Decimal("0"),
        tax_base=Decimal("0"), total_amount=Decimal("0"),
        csv_extra_km=Decimal("0"), csv_extra_time=Decimal("0"), csv_night_charge=Decimal("0"),
    )


def test_default_rate_card_matches_current_rate_list():
    assert (RATES.small_fare, RATES.large_fare) == (950, 1900)
    assert RATES.extra_hour_rate == Decimal("200")
    assert RATES.extra_km_rate == Decimal("17")
    assert RATES.night_charge == Decimal("265")


def test_base_4_40_trip_has_no_extras():
    res = calculate_rental_fare(_row(), RATES)
    assert res.trip_fare == Decimal("950")
    assert (res.base_hours, res.base_kms) == (4, 40)


def test_extra_km_and_extra_hour_use_new_rates():
    # 4:40 package, 5h45m and 50 km -> 2 extra hrs (45 >= 31 min rounds up), 10 extra km
    res = calculate_rental_fare(_row(duration="5:45", kms="50.9"), RATES)
    assert res.extra_time_hours == 2
    assert res.extra_time_charge == Decimal("400")
    assert res.extra_km == 10
    assert res.extra_km_charge == Decimal("170")
    assert res.trip_fare == Decimal("950") + 400 + 170


def test_upgrade_to_8_80_by_kms():
    res = calculate_rental_fare(_row(kms="61"), RATES)
    assert res.effective_package == 1900
    assert res.trip_fare == Decimal("1900")


def test_night_charge_when_drop_in_night_window():
    res = calculate_rental_fare(_row(pickup="8:00 PM", drop="11:30 PM", duration="3:30"), RATES)
    assert res.night_charge == Decimal("265")


def test_changing_the_rate_card_changes_the_fare():
    custom = replace(RATES, small_fare=1000, extra_km_rate=Decimal("20"))
    res = calculate_rental_fare(_row(package=1000, kms="45"), custom)
    assert res.trip_fare == Decimal("1000") + 5 * 20


def test_csv_detects_rental_by_rate_card_package_fares():
    csv = (
        "Date,Car No,DS no/Route No,Guest Name,Pickup Location,Drop Location,"
        "Pick up Time,Drop Time,Trip Duration,Total kms,Package,Trip Fare,Toll/MCD,Entity,Entity Gst\n"
        "09/01/2026,DL1,R1,G,A,B,10:00 AM,2:00 PM,4:00,40,950,950,0,PRICE WATERHOUSE LLP,06AAEFP3641G1ZG\n"
        "09/01/2026,DL1,R2,G,A,B,10:00 AM,6:00 PM,8:00,80,1900,1900,0,PRICE WATERHOUSE LLP,06AAEFP3641G1ZG\n"
        "09/01/2026,DL1,R3,G,A,B,10:00 AM,11:00 AM,1:00,20,1050,1050,0,PRICE WATERHOUSE LLP,06AAEFP3641G1ZG\n"
    ).encode()
    _, rows = parse_eee_taxi_csv(csv, RATES)
    assert [r.booking_type for r in rows] == ["rental", "rental", "p2p"]


def test_old_package_values_are_no_longer_rentals():
    csv = (
        "Date,Car No,DS no/Route No,Guest Name,Pickup Location,Drop Location,"
        "Pick up Time,Drop Time,Trip Duration,Total kms,Package,Trip Fare,Toll/MCD,Entity,Entity Gst\n"
        "09/01/2026,DL1,R1,G,A,B,10:00 AM,2:00 PM,4:00,40,920,920,0,PRICE WATERHOUSE LLP,06AAEFP3641G1ZG\n"
    ).encode()
    _, rows = parse_eee_taxi_csv(csv, RATES)
    assert rows[0].booking_type == "p2p"


def test_rate_card_input_rejects_equal_package_fares():
    data = {**rate_card_to_dict(RATES), "large_fare": 950}
    with pytest.raises(ValidationError):
        RateCardIn(**data)


def test_rate_card_input_rejects_negative_rate():
    data = {**rate_card_to_dict(RATES), "extra_km_rate": "-1"}
    with pytest.raises(ValidationError):
        RateCardIn(**data)


def test_rate_card_round_trips_through_json():
    assert RateCardIn(**rate_card_to_dict(RATES)).to_rate_card() == RATES


# ── Route fares ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pickup, drop, fare", [
    ("Gurgaon", "Delhi", "1050"),
    ("Delhi", "Gurgaon", "1100"),          # direction matters for city routes
    ("Airport", "Gurgaon", "950"),
    ("Gurgaon", "Airport", "950"),         # airport routes match both directions
    ("Noida", "Ghaziabad", "950"),         # both map to UP
    ("Faridabad", "Faridabad", "800"),
    ("gurugram", "new delhi", "1050"),     # aliases, case-insensitive
])
def test_card_fare_lookup(pickup, drop, fare):
    assert RATES.card_fare(pickup, drop).fare == Decimal(fare)


def test_unknown_zone_reports_problem():
    match = RATES.card_fare("Jaipur", "Delhi")
    assert match.fare is None
    assert "Jaipur" in match.problem


def test_fare_check_flags_mismatch_and_card_fare_is_applied_only_when_chosen():
    from app.services.eee_taxi_fare_check import apply_card_fares, check_p2p_fares

    row = replace(_row(package=1000), booking_type="p2p", trip_fare=Decimal("1000"),
                  total_amount=Decimal("1180"), pickup_zone="Delhi", drop_zone="Airport")
    [chk] = check_p2p_fares([row], RATES)
    assert chk.status == "mismatch" and chk.card_fare == Decimal("1050")

    untouched = apply_card_fares([row], set(), RATES)[0]
    assert untouched.trip_fare == Decimal("1000")      # default: CSV fare

    chosen = apply_card_fares([row], {row.row_index}, RATES)[0]
    assert chosen.trip_fare == Decimal("1050")
    assert chosen.total_amount == Decimal("0")         # pipeline recomputes the total


def test_duplicate_route_is_rejected():
    data = rate_card_to_dict(RATES)
    data["routes"] = data["routes"] + [data["routes"][0]]
    with pytest.raises(ValidationError):
        RateCardIn(**data)
