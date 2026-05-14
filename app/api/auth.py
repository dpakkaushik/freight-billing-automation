from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, UserRole
from app.schemas import TokenOut, UserCreateIn, UserLoginIn, UserOut, UserUpdateIn, ALL_MODULES
from app.services.auth import (
    authenticate_user,
    create_access_token,
    get_current_active_user,
    get_current_user_optional,
    get_password_hash,
    require_admin,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/needs-setup")
def needs_setup(db: Session = Depends(get_db)):
    """Returns true when zero users exist — lets the login page show first-run setup."""
    count = db.scalar(select(func.count()).select_from(User)) or 0
    return {"needs_setup": count == 0}


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register_user(
    body: UserCreateIn,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
) -> UserOut:
    existing_users = db.scalar(select(func.count()).select_from(User)) or 0
    role = body.role.lower().strip()
    if existing_users == 0:
        # First user is always admin with all permissions
        role = UserRole.ADMIN.value
        permissions = ALL_MODULES[:]
    elif current_user is None or current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can register new users.")
    else:
        permissions = [m for m in body.permissions if m in ALL_MODULES]

    if role not in {UserRole.ADMIN.value, UserRole.USER.value}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user role.")

    email = body.email.lower().strip()
    if db.scalar(select(func.count()).select_from(User).where(User.email == email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists.")

    user = User(
        email=email,
        full_name=body.full_name,
        hashed_password=get_password_hash(body.password),
        role=UserRole(role),
        permissions=permissions,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenOut)
def login_user(body: UserLoginIn, db: Session = Depends(get_db)) -> TokenOut:
    user = authenticate_user(db, body.email, body.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    access_token = create_access_token(data={"sub": user.email})
    user.last_login = datetime.utcnow()
    db.commit()
    return TokenOut(access_token=access_token)


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_active_user)) -> UserOut:
    return UserOut.model_validate(current_user)


@router.get("/users", response_model=list[UserOut])
def list_users(current_user: User = Depends(require_admin), db: Session = Depends(get_db)) -> list[UserOut]:
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return [UserOut.model_validate(u) for u in users]


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: str,
    body: UserUpdateIn,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UserOut:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.permissions is not None:
        user.permissions = [m for m in body.permissions if m in ALL_MODULES]
    if body.password:
        user.hashed_password = get_password_hash(body.password)
    user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> None:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if user.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete your own account.")
    db.delete(user)
    db.commit()
