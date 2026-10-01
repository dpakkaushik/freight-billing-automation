"""EEE-Taxi rate card API.

Any signed-in user can read the rate card. Changing it needs an admin
account AND the separate rate-card edit password, which is checked on every
save (the "unlock" call only lets the page know the password is right).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services.auth import (
    get_current_active_user,
    get_password_hash,
    require_admin,
    verify_password,
)
from app.services.eee_taxi_rates import (
    DEFAULT_RATE_CARD,
    RateCardIn,
    get_edit_password_hash,
    get_rate_card_state,
    rate_card_to_dict,
    save_rate_card,
    set_edit_password_hash,
)

router = APIRouter(prefix="/api/eee-taxi/rates", tags=["eee-taxi"])

_MIN_PASSWORD = 6


class UnlockIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class PasswordIn(BaseModel):
    current_password: str = Field(default="", max_length=128)
    new_password: str = Field(min_length=_MIN_PASSWORD, max_length=128)


class RateCardUpdateIn(BaseModel):
    edit_password: str = Field(min_length=1, max_length=128)
    rates: RateCardIn


def _response(db: Session) -> dict:
    state = get_rate_card_state(db)
    return {
        "rates": rate_card_to_dict(state.card),
        "defaults": rate_card_to_dict(DEFAULT_RATE_CARD),
        "updated_by": state.updated_by,
        "updated_at": state.updated_at.isoformat() + "Z" if state.updated_at else None,
        "has_edit_password": state.has_edit_password,
    }


def _check_edit_password(db: Session, password: str, admin: User) -> None:
    stored = get_edit_password_hash(db)
    if not stored:
        raise HTTPException(status.HTTP_409_CONFLICT, "Set a rate card edit password first.")
    if not verify_password(password, stored):
        logger.warning("Wrong rate card edit password entered by {}", admin.email)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Wrong rate card edit password.")


@router.get("")
def read_rates(
    _: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> dict:
    return _response(db)


@router.post("/unlock")
def unlock(body: UnlockIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    _check_edit_password(db, body.password, admin)
    return {"ok": True}


@router.post("/password")
def set_password(body: PasswordIn, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    """Set the edit password the first time, or change it (needs the current one)."""
    if get_edit_password_hash(db):
        _check_edit_password(db, body.current_password, admin)
    set_edit_password_hash(db, get_password_hash(body.new_password))
    logger.info("Rate card edit password set by {}", admin.email)
    return {"ok": True}


@router.put("")
def update_rates(
    body: RateCardUpdateIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    _check_edit_password(db, body.edit_password, admin)
    before = rate_card_to_dict(get_rate_card_state(db).card)
    card = body.rates.to_rate_card()
    save_rate_card(db, card, updated_by=admin.email)
    after = rate_card_to_dict(card)
    changed = sorted(k for k in after if before.get(k) != after[k])
    logger.info("EEE-Taxi rate card updated by {}; changed: {}", admin.email, changed or "nothing")
    return _response(db)
