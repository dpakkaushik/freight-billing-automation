"""EEE-Taxi -> Tally export: preview the invoices, then download the XML.

Only finished (signed) invoices are offered. Downloading records the time on
each invoice so the next export skips it by default; re-exporting is allowed
(ticked "include already exported") because that is how a correction reaches
Tally, and Tally's "Prevent duplicates" stops a second copy of a number.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services.auth import require_permission
from app.services.eee_taxi_tally import ExportItem, find_export_items, mark_exported
from app.services.tally_export import render_tally_xml

router = APIRouter(prefix="/api/eee-taxi/tally", tags=["eee-taxi"])

_eee_user = require_permission("eee_taxi")
_MAX_INVOICES = 2_000


class ExportIn(BaseModel):
    invoice_ids: list[str] = Field(min_length=1, max_length=_MAX_INVOICES)


def _item(i: ExportItem) -> dict:
    return {
        "id": i.invoice_id,
        "invoice_no": i.invoice_no,
        "invoice_date": i.invoice_date.isoformat(),
        "entity_name": i.entity_name,
        "car_no": i.car_no,
        "route_no": i.route_no,
        "booking_type": i.booking_type,
        "total": f"{i.total:.2f}" if i.total is not None else None,
        "exported_at": i.exported_at.isoformat() + "Z" if i.exported_at else None,
        "problem": i.problem,
    }


@router.get("/preview")
def preview(
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    include_exported: bool = Query(False),
    _: User = Depends(_eee_user),
    db: Session = Depends(get_db),
) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The From date is after the To date.")
    items = find_export_items(db, date_from, date_to, include_exported)
    return {"invoices": [_item(i) for i in items]}


@router.post("/export")
def export(body: ExportIn, user: User = Depends(_eee_user), db: Session = Depends(get_db)) -> Response:
    ids = list(dict.fromkeys(body.invoice_ids))
    items = find_export_items(db, include_exported=True, invoice_ids=ids)
    found = {i.invoice_id for i in items}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"{len(missing)} selected invoice(s) are not finished invoices any more. Refresh the list.")
    blocked = [i for i in items if i.problem]
    if blocked:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Fix these first: " + "; ".join(f"{i.invoice_no}: {i.problem}" for i in blocked[:10]))

    xml = render_tally_xml([i.voucher for i in items])
    mark_exported(db, ids)
    first, last = items[0].invoice_no, items[-1].invoice_no
    logger.info("Tally export by {}: {} invoices ({} .. {})", user.email, len(items), first, last)
    filename = f"tally_{first}_to_{last}.xml".replace("/", "-")
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
