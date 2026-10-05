"""Pick EEE-Taxi invoices for a Tally export and turn them into Tally vouchers.

Invoice rows do not store amounts; like the PDF, each voucher is rebuilt from
the batch's stored CSV and rate-card snapshot, so Tally gets exactly the
figures printed on the invoice. Anything that would make Tally reject the
voucher (no cost centre for the car, unknown client, a total that does not
add up) is reported as a problem and the invoice is left out of the file.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from app.models import EeeTaxiBatch, EeeTaxiInvoice, EeeTaxiInvoiceStatus
from app.services.eee_taxi_clients import UnknownClientError, is_local, lookup_client
from app.services.eee_taxi_cost_centres import get_cost_centre_state, normalize_vehicle_no
from app.services.eee_taxi_csv import EeeTaxiRow
from app.services.eee_taxi_pdf import compute_tax
from app.services.eee_taxi_pipeline import batch_rates, batch_rows
from app.services.eee_taxi_rates import RateCard
from app.services.eee_taxi_rental_calc import calculate_rental_fare
from app.services.tally_export import TallyVoucher

_TOLERANCE = Decimal("0.01")


class ExportProblem(ValueError):
    """Why an invoice cannot go into the Tally file."""


@dataclass(frozen=True)
class ExportItem:
    invoice_id: str
    invoice_no: str
    invoice_date: date
    entity_name: str
    car_no: str
    route_no: str
    booking_type: str
    total: Optional[Decimal]
    exported_at: Optional[datetime]
    problem: str = ""
    voucher: Optional[TallyVoucher] = None


def _n(v: Decimal) -> str:
    """1900 -> '1900', 119.5 -> '119.50' (as the Tally descriptions are typed)."""
    return f"{v:.0f}" if v == v.to_integral_value() else f"{v:.2f}"


def _description(row: EeeTaxiRow, rates: RateCard) -> tuple[str, ...]:
    lines = [f"Guest Name:-{row.guest_name}", f"From:-{row.pickup_location}", f"To:-{row.drop_location}"]
    lines = [line for line in lines if not line.endswith(":-")]
    if row.booking_type == "rental":
        fr = calculate_rental_fare(row, rates)
        lines += [
            f"Rental ({fr.base_hours}/{fr.base_kms}=  {Decimal(fr.effective_package):.2f})",
            f"Extra Hrs ({fr.extra_time_hours}*{_n(rates.extra_hour_rate)}=  {_n(fr.extra_time_charge)})",
            f"Extra Kms ({fr.extra_km}*{_n(rates.extra_km_rate)}=  {_n(fr.extra_km_charge)})",
        ]
        if fr.night_charge:
            lines.append(f"Night Charges ({rates.night_label}=  {_n(fr.night_charge)})")
    return tuple(lines)


def voucher_for_row(
    row: EeeTaxiRow,
    invoice_no: str,
    invoice_date: date,
    rates: RateCard,
    cost_centres: dict[str, str],
) -> TallyVoucher:
    """The Tally voucher for one invoice; raises ExportProblem when Tally would reject it."""
    try:
        party = lookup_client(row.client_gstin)
    except UnknownClientError as exc:
        raise ExportProblem(str(exc)) from exc

    cost_centre = cost_centres.get(normalize_vehicle_no(row.car_no))
    if not cost_centre:
        raise ExportProblem(f"Car {row.car_no} has no cost centre. Add it on Masters -> Cost centres.")

    local = is_local(row.client_gstin)
    cgst, sgst, igst = compute_tax(row.tax_base + row.parking, local)
    voucher = TallyVoucher(
        invoice_no=invoice_no,
        invoice_date=invoice_date,
        trip_date=row.trip_date,
        route_no=row.route_no,
        car_no=row.car_no,
        party=party,
        is_local=local,
        fare=row.tax_base,
        toll=row.parking,
        cgst=cgst,
        sgst=sgst,
        igst=igst,
        cost_centre=cost_centre,
        description=_description(row, rates),
    )
    # The PDF prints the CSV total when there is one; Tally needs the voucher to
    # balance, so the two must agree or the books would differ from the invoice.
    if row.total_amount and abs(row.total_amount - voucher.total) > _TOLERANCE:
        raise ExportProblem(
            f"Invoice total {row.total_amount:.2f} on the CSV differs from fare + toll + GST "
            f"{voucher.total:.2f}. Check this invoice before sending it to Tally."
        )
    return voucher


def _items_for_batch(batch: EeeTaxiBatch, invoices: list[EeeTaxiInvoice],
                     cost_centres: dict[str, str]) -> list[ExportItem]:
    def item(inv: EeeTaxiInvoice, row: Optional[EeeTaxiRow], **kw) -> ExportItem:
        return ExportItem(
            invoice_id=inv.id,
            invoice_no=inv.invoice_no or "",
            invoice_date=batch.invoice_date,
            entity_name=inv.entity_name or (row.entity_name if row else ""),
            car_no=row.car_no if row else "",
            route_no=row.route_no if row else "",
            booking_type=inv.booking_type or (row.booking_type if row else ""),
            exported_at=inv.tally_exported_at,
            **kw,
        )

    try:
        rates = batch_rates(batch)
        rows = {r.row_index: r for r in batch_rows(batch, rates)}
    except Exception as exc:  # a batch whose stored CSV no longer parses
        logger.warning("Tally export: batch {} could not be rebuilt: {}", batch.id, exc)
        return [item(inv, None, total=None, problem=f"The batch could not be read: {exc}") for inv in invoices]

    out: list[ExportItem] = []
    for inv in invoices:
        row = rows.get(inv.row_index)
        if row is None or not inv.invoice_no:
            out.append(item(inv, row, total=None, problem="This invoice is no longer in its batch's CSV."))
            continue
        try:
            v = voucher_for_row(row, inv.invoice_no, batch.invoice_date, rates, cost_centres)
            out.append(item(inv, row, total=v.total, voucher=v))
        except ExportProblem as exc:
            out.append(item(inv, row, total=None, problem=str(exc)))
    return out


def find_export_items(
    db: Session,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    include_exported: bool = False,
    invoice_ids: Optional[list[str]] = None,
) -> list[ExportItem]:
    """Finished (signed) invoices in the date range, oldest invoice number first."""
    q = (
        db.query(EeeTaxiInvoice)
        .join(EeeTaxiBatch, EeeTaxiInvoice.batch_id == EeeTaxiBatch.id)
        .filter(EeeTaxiInvoice.status == EeeTaxiInvoiceStatus.DONE)
    )
    if date_from:
        q = q.filter(EeeTaxiBatch.invoice_date >= date_from)
    if date_to:
        q = q.filter(EeeTaxiBatch.invoice_date <= date_to)
    if not include_exported:
        q = q.filter(EeeTaxiInvoice.tally_exported_at.is_(None))
    if invoice_ids is not None:
        q = q.filter(EeeTaxiInvoice.id.in_(invoice_ids))

    by_batch: dict[str, list[EeeTaxiInvoice]] = {}
    for inv in q.all():
        by_batch.setdefault(inv.batch_id, []).append(inv)

    cost_centres = {c.vehicle_no: c.cost_centre for c in get_cost_centre_state(db).rows}
    items: list[ExportItem] = []
    for batch_id, invoices in by_batch.items():
        items += _items_for_batch(db.get(EeeTaxiBatch, batch_id), invoices, cost_centres)
    return sorted(items, key=lambda i: (i.invoice_date, i.invoice_no))


def mark_exported(db: Session, invoice_ids: list[str], when: Optional[datetime] = None) -> None:
    when = when or datetime.utcnow()
    db.query(EeeTaxiInvoice).filter(EeeTaxiInvoice.id.in_(invoice_ids)).update(
        {EeeTaxiInvoice.tally_exported_at: when}, synchronize_session=False
    )
    db.commit()
