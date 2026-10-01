"""Editable rate card for EEE-Taxi invoicing.

The rate card lives in the ``eee_taxi_rate_card`` table (one row) and is
edited from the EEE-Taxi -> Rate Card page. It holds:

* spot rental rules (packages, extra hour / km, night charge, upgrade rules)
* fixed point-to-point route fares (From area -> To area -> fare)
* the mapping from CSV zone names (Pickup Zone / Drop Zone) to rate-card areas

Every fare calculation takes a ``RateCard`` explicitly, so a batch uses one
consistent snapshot even if an admin edits the rates while it is running.
Editing is protected by a separate edit password (stored as a hash).

If no row has been saved yet, ``DEFAULT_RATE_CARD`` is used.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from app.models import EeeTaxiRateCard

AIRPORT_AREA = "Airport"


@dataclass(frozen=True)
class RouteFare:
    from_area: str
    to_area: str
    fare: Decimal


@dataclass(frozen=True)
class ZoneArea:
    zone: str      # value as it appears in the CSV Pickup Zone / Drop Zone column
    area: str      # rate-card area it belongs to


@dataclass(frozen=True)
class CardFareMatch:
    from_area: Optional[str]
    to_area: Optional[str]
    fare: Optional[Decimal]
    problem: str = ""          # why no fare was found ("" when fare is set)


@dataclass(frozen=True)
class RateCard:
    # Package A (e.g. NCR 4/40)
    small_fare: int
    small_hours: int
    small_kms: int
    # Package B (e.g. NCR 8/80)
    large_fare: int
    large_hours: int
    large_kms: int
    # Extras
    extra_hour_rate: Decimal
    extra_km_rate: Decimal
    night_charge: Decimal
    night_start_hour: int        # night window start, inclusive (0-23)
    night_end_hour: int          # night window end, exclusive (0-23)
    # Rules
    partial_hour_minutes: int    # leftover minutes >= this count as a full extra hour
    upgrade_minutes: int         # small -> large upgrade when duration >= this
    upgrade_kms: int             # small -> large upgrade when kms >= this
    # Point-to-point
    routes: tuple[RouteFare, ...] = field(default_factory=tuple)
    zone_areas: tuple[ZoneArea, ...] = field(default_factory=tuple)

    @property
    def rental_packages(self) -> frozenset[int]:
        """CSV ``Package`` values that mark a row as a rental."""
        return frozenset({self.small_fare, self.large_fare})

    def package_base(self, package_fare: int) -> tuple[int, int]:
        """(base_hours, base_kms) for a package fare; unknown -> large package."""
        if package_fare == self.small_fare:
            return self.small_hours, self.small_kms
        return self.large_hours, self.large_kms

    def is_night_hour(self, hour: Optional[int]) -> bool:
        if hour is None:
            return False
        start, end = self.night_start_hour, self.night_end_hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end   # window crosses midnight

    @property
    def night_label(self) -> str:
        return f"{self.night_start_hour:02d}:00-{self.night_end_hour:02d}:00"

    def area_for_zone(self, zone: str) -> Optional[str]:
        key = _norm(zone)
        if not key:
            return None
        for za in self.zone_areas:
            if _norm(za.zone) == key:
                return za.area
        for route in self.routes:                 # zone already named like an area
            for area in (route.from_area, route.to_area):
                if _norm(area) == key:
                    return area
        return None

    def card_fare(self, pickup_zone: str, drop_zone: str) -> CardFareMatch:
        """Fixed fare for a P2P trip, matched on the CSV pickup and drop zones.

        Airport routes are listed once (Airport -> city) and match trips in
        either direction; every other route matches its exact direction.
        """
        from_area = self.area_for_zone(pickup_zone)
        to_area = self.area_for_zone(drop_zone)
        missing = [
            f"{label} zone {zone!r} is not on the rate card" if zone.strip() else f"{label} zone is blank"
            for label, zone, area in (("Pickup", pickup_zone, from_area), ("Drop", drop_zone, to_area))
            if area is None
        ]
        if missing:
            return CardFareMatch(from_area, to_area, None, "; ".join(missing))

        wanted = [(from_area, to_area)]
        if _norm(to_area) == _norm(AIRPORT_AREA):
            wanted.append((to_area, from_area))
        for want_from, want_to in wanted:
            for route in self.routes:
                if _norm(route.from_area) == _norm(want_from) and _norm(route.to_area) == _norm(want_to):
                    return CardFareMatch(from_area, to_area, route.fare)
        return CardFareMatch(from_area, to_area, None, f"No route fare for {from_area} -> {to_area}")


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _routes(*rows: tuple[str, str, int]) -> tuple[RouteFare, ...]:
    return tuple(RouteFare(f, t, Decimal(fare)) for f, t, fare in rows)


DEFAULT_RATE_CARD = RateCard(
    small_fare=950, small_hours=4, small_kms=40,
    large_fare=1900, large_hours=8, large_kms=80,
    extra_hour_rate=Decimal("200"),
    extra_km_rate=Decimal("17"),
    night_charge=Decimal("265"),
    night_start_hour=23, night_end_hour=5,
    partial_hour_minutes=31,
    upgrade_minutes=6 * 60 + 31,
    upgrade_kms=61,
    routes=_routes(
        ("Airport", "Delhi", 1050), ("Airport", "Gurgaon", 950),
        ("Airport", "Faridabad", 1100), ("Airport", "UP", 1400),
        ("Gurgaon", "Delhi", 1050), ("Gurgaon", "Gurgaon", 660),
        ("Gurgaon", "Faridabad", 1100), ("Gurgaon", "UP", 1350),
        ("Delhi", "Delhi", 1100), ("Delhi", "Gurgaon", 1100),
        ("Delhi", "Faridabad", 1100), ("Delhi", "UP", 1350),
        ("UP", "UP", 950), ("UP", "Gurgaon", 1350),
        ("UP", "Faridabad", 1300), ("UP", "Delhi", 1350),
        ("Faridabad", "Faridabad", 800), ("Faridabad", "Gurgaon", 1100),
        ("Faridabad", "Delhi", 1100), ("Faridabad", "UP", 1300),
    ),
    zone_areas=tuple(ZoneArea(z, a) for z, a in (
        ("Airport", "Airport"),
        ("Delhi", "Delhi"), ("New Delhi", "Delhi"),
        ("Gurgaon", "Gurgaon"), ("Gurugram", "Gurgaon"),
        ("Faridabad", "Faridabad"),
        ("Noida", "UP"), ("Greater Noida", "UP"), ("Ghaziabad", "UP"), ("UP", "UP"),
    )),
)


# ── Validated input ───────────────────────────────────────────────────────────

_Name = Field(min_length=1, max_length=40)


class RouteFareIn(BaseModel):
    from_area: str = _Name
    to_area: str = _Name
    fare: Decimal = Field(gt=0, le=100_000, max_digits=8, decimal_places=2)

    @field_validator("from_area", "to_area")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Area name cannot be blank.")
        return v


class ZoneAreaIn(BaseModel):
    zone: str = _Name
    area: str = _Name

    @field_validator("zone", "area")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Zone and area cannot be blank.")
        return v


class RateCardIn(BaseModel):
    """Validated input from the Rate Card page."""
    small_fare: int = Field(gt=0, le=100_000)
    small_hours: int = Field(gt=0, le=24)
    small_kms: int = Field(gt=0, le=2_000)
    large_fare: int = Field(gt=0, le=100_000)
    large_hours: int = Field(gt=0, le=24)
    large_kms: int = Field(gt=0, le=2_000)
    extra_hour_rate: Decimal = Field(ge=0, le=10_000, max_digits=8, decimal_places=2)
    extra_km_rate: Decimal = Field(ge=0, le=1_000, max_digits=8, decimal_places=2)
    night_charge: Decimal = Field(ge=0, le=10_000, max_digits=8, decimal_places=2)
    night_start_hour: int = Field(ge=0, le=23)
    night_end_hour: int = Field(ge=0, le=23)
    partial_hour_minutes: int = Field(ge=1, le=60)
    upgrade_minutes: int = Field(ge=1, le=24 * 60)
    upgrade_kms: int = Field(ge=1, le=2_000)
    routes: list[RouteFareIn] = Field(default_factory=list, max_length=300)
    zone_areas: list[ZoneAreaIn] = Field(default_factory=list, max_length=300)

    @model_validator(mode="after")
    def _check_consistency(self) -> "RateCardIn":
        if self.small_fare == self.large_fare:
            raise ValueError("The two package fares must differ; they identify the package in the CSV.")
        if self.night_start_hour == self.night_end_hour:
            raise ValueError("Night start and end hour cannot be the same.")
        seen_routes: set[tuple[str, str]] = set()
        for r in self.routes:
            key = (_norm(r.from_area), _norm(r.to_area))
            if key in seen_routes:
                raise ValueError(f"Route {r.from_area} -> {r.to_area} is listed twice.")
            seen_routes.add(key)
        seen_zones: set[str] = set()
        for z in self.zone_areas:
            if _norm(z.zone) in seen_zones:
                raise ValueError(f"Zone {z.zone!r} is listed twice.")
            seen_zones.add(_norm(z.zone))
        return self

    def to_rate_card(self) -> RateCard:
        data = self.model_dump(exclude={"routes", "zone_areas"})
        return RateCard(
            **data,
            routes=tuple(RouteFare(r.from_area, r.to_area, r.fare) for r in self.routes),
            zone_areas=tuple(ZoneArea(z.zone, z.area) for z in self.zone_areas),
        )


# ── Persistence ───────────────────────────────────────────────────────────────

def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def rate_card_to_dict(card: RateCard) -> dict:
    return _jsonable(asdict(card))


def _from_json(data: dict) -> RateCard:
    # Keys added in later versions fall back to the defaults.
    merged = {**rate_card_to_dict(DEFAULT_RATE_CARD), **(data or {})}
    return RateCardIn(**merged).to_rate_card()


@dataclass(frozen=True)
class RateCardState:
    card: RateCard
    updated_by: Optional[str]
    updated_at: Optional[datetime]
    has_edit_password: bool


def get_rate_card(db: Session) -> RateCard:
    row = db.get(EeeTaxiRateCard, 1)
    return DEFAULT_RATE_CARD if row is None or not row.rates else _from_json(row.rates)


def get_rate_card_state(db: Session) -> RateCardState:
    row = db.get(EeeTaxiRateCard, 1)
    if row is None:
        return RateCardState(DEFAULT_RATE_CARD, None, None, False)
    card = _from_json(row.rates) if row.rates else DEFAULT_RATE_CARD
    return RateCardState(card, row.updated_by, row.updated_at, bool(row.edit_password_hash))


def _get_or_create_row(db: Session) -> EeeTaxiRateCard:
    row = db.get(EeeTaxiRateCard, 1)
    if row is None:
        row = EeeTaxiRateCard(id=1, rates={})
        db.add(row)
    return row


def save_rate_card(db: Session, card: RateCard, updated_by: str) -> None:
    row = _get_or_create_row(db)
    row.rates = rate_card_to_dict(card)
    row.updated_by = updated_by
    row.updated_at = datetime.utcnow()
    db.commit()


def get_edit_password_hash(db: Session) -> Optional[str]:
    row = db.get(EeeTaxiRateCard, 1)
    return row.edit_password_hash if row else None


def set_edit_password_hash(db: Session, password_hash: str) -> None:
    row = _get_or_create_row(db)
    row.edit_password_hash = password_hash
    db.commit()
