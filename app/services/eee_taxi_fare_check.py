"""Check point-to-point trip fares against the rate card's route fares.

By default every invoice uses the CSV ``Trip Fare``. This module finds the
P2P rows whose CSV fare differs from the rate card (or that match no route)
so the user can review them, and applies the card fare to the rows the user
chooses.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from app.services.eee_taxi_csv import EeeTaxiRow
from app.services.eee_taxi_rates import RateCard

STATUS_OK = "ok"
STATUS_MISMATCH = "mismatch"
STATUS_NO_ROUTE = "no_route"


@dataclass(frozen=True)
class FareCheck:
    row_index: int
    trip_date: date
    guest_name: str
    pickup_zone: str
    drop_zone: str
    from_area: Optional[str]
    to_area: Optional[str]
    csv_fare: Decimal
    card_fare: Optional[Decimal]
    status: str
    message: str

    def to_dict(self) -> dict:
        return {
            "row": self.row_index,
            "date": self.trip_date.strftime("%d/%m/%Y"),
            "guest_name": self.guest_name,
            "pickup_zone": self.pickup_zone,
            "drop_zone": self.drop_zone,
            "from_area": self.from_area,
            "to_area": self.to_area,
            "csv_fare": str(self.csv_fare),
            "card_fare": str(self.card_fare) if self.card_fare is not None else None,
            "status": self.status,
            "message": self.message,
        }


def check_row(row: EeeTaxiRow, rates: RateCard) -> FareCheck:
    match = rates.card_fare(row.pickup_zone, row.drop_zone)
    if match.fare is None:
        status, message = STATUS_NO_ROUTE, match.problem
    elif match.fare == row.trip_fare:
        status, message = STATUS_OK, ""
    else:
        status = STATUS_MISMATCH
        message = f"CSV fare {row.trip_fare} differs from rate card {match.fare} ({match.from_area} -> {match.to_area})"
    return FareCheck(
        row_index=row.row_index, trip_date=row.trip_date, guest_name=row.guest_name,
        pickup_zone=row.pickup_zone, drop_zone=row.drop_zone,
        from_area=match.from_area, to_area=match.to_area,
        csv_fare=row.trip_fare, card_fare=match.fare,
        status=status, message=message,
    )


def check_p2p_fares(rows: Iterable[EeeTaxiRow], rates: RateCard) -> list[FareCheck]:
    """Fare check for every P2P row (rentals are priced by the rental rules)."""
    return [check_row(r, rates) for r in rows if r.booking_type == "p2p"]


def apply_card_fares(
    rows: list[EeeTaxiRow],
    row_indexes: set[int],
    rates: RateCard,
) -> list[EeeTaxiRow]:
    """Return rows where the chosen P2P rows are billed at the rate-card fare.

    The fare is looked up again here (never taken from the browser). Rows
    with no matching route keep their CSV fare. ``total_amount`` is reset so
    the invoice total is recomputed from the new fare.
    """
    result: list[EeeTaxiRow] = []
    for row in rows:
        card = rates.card_fare(row.pickup_zone, row.drop_zone).fare
        if row.row_index in row_indexes and row.booking_type == "p2p" and card is not None:
            result.append(replace(row, trip_fare=card, tax_base=card, total_amount=Decimal("0")))
        else:
            result.append(row)
    return result
