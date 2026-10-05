"""EEE-Taxi cost centre master API (Masters page).

Reading and importing are open to every EEE-Taxi user; saving needs the same
masters edit password as the rate card. Importing only reads the Tally Excel
and returns the proposed list; the page shows it for review before saving.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from loguru import logger
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.eee_taxi_rates import check_edit_password
from app.database import get_db
from app.models import User
from app.services.auth import require_permission
from app.services.eee_taxi_cost_centres import (
    CostCentre,
    CostCentresIn,
    get_cost_centre_state,
    parse_tally_cost_centres,
    save_cost_centres,
)
from app.services.eee_taxi_rates import get_edit_password_hash

router = APIRouter(prefix="/api/eee-taxi/cost-centres", tags=["eee-taxi"])

_MAX_XLSX_BYTES = 2 * 1024 * 1024   # a 172-entry Tally export is ~13 KB

_eee_user = require_permission("eee_taxi")


class CostCentresUpdateIn(CostCentresIn):
    edit_password: str = Field(min_length=1, max_length=128)


def _row(c: CostCentre) -> dict:
    return {"vehicle_no": c.vehicle_no, "cost_centre": c.cost_centre}


def _response(db: Session) -> dict:
    state = get_cost_centre_state(db)
    return {
        "rows": [_row(c) for c in state.rows],
        "updated_by": state.updated_by,
        "updated_at": state.updated_at.isoformat() + "Z" if state.updated_at else None,
        "has_edit_password": bool(get_edit_password_hash(db)),
    }


@router.get("")
def read_cost_centres(_: User = Depends(_eee_user), db: Session = Depends(get_db)) -> dict:
    return _response(db)


@router.post("/import-tally")
def import_tally(file: UploadFile = File(...), user: User = Depends(_eee_user)) -> dict:
    """Read the "List of Cost Centres" Excel exported from Tally. Nothing is saved."""
    data = file.file.read(_MAX_XLSX_BYTES + 1)
    if len(data) > _MAX_XLSX_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "The file is larger than 2 MB.")
    try:
        result = parse_tally_cost_centres(data)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    logger.info("Tally cost centre file {!r} read by {}: {} cars", file.filename, user.email, len(result.rows))
    return {
        "rows": [_row(c) for c in result.rows],
        "not_cars": list(result.not_cars),
        "multi_car": list(result.multi_car),
        "picked": [{"vehicle_no": p.vehicle_no, "chosen": p.chosen, "others": list(p.others)} for p in result.picked],
    }


@router.put("")
def update_cost_centres(
    body: CostCentresUpdateIn,
    user: User = Depends(_eee_user),
    db: Session = Depends(get_db),
) -> dict:
    check_edit_password(db, body.edit_password, user)
    before = {c.vehicle_no: c.cost_centre for c in get_cost_centre_state(db).rows}
    rows = body.to_rows()
    after = {c.vehicle_no: c.cost_centre for c in rows}
    save_cost_centres(db, rows, updated_by=user.email)
    logger.info(
        "EEE-Taxi cost centres saved by {}: {} cars ({} added, {} changed, {} removed)",
        user.email, len(after),
        len(after.keys() - before.keys()),
        sum(1 for k in after.keys() & before.keys() if after[k] != before[k]),
        len(before.keys() - after.keys()),
    )
    return _response(db)
