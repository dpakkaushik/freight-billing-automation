"""SQLAlchemy engine + session factory.

We use SQLite for the MVP. Switching to PostgreSQL later only requires
changing DATABASE_URL in `.env` — no code changes here.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Base class for all ORM models."""


# `check_same_thread=False` is required because FastAPI background tasks
# and the async worker access the connection from different threads.
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args=_connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db() -> None:
    """Create tables if they don't exist. Idempotent — safe to call on every startup."""
    from app import models  # noqa: F401

    settings.ensure_dirs()
    Base.metadata.create_all(bind=engine)
    _migrate_existing_db()


def _migrate_existing_db() -> None:
    """Add columns introduced after initial deploy without dropping existing data."""
    from sqlalchemy import inspect, text

    with engine.connect() as conn:
        inspector = inspect(conn)
        if "users" in inspector.get_table_names():
            existing = {c["name"] for c in inspector.get_columns("users")}
            if "permissions" not in existing:
                conn.execute(text("ALTER TABLE users ADD COLUMN permissions JSON"))
            if "full_name" not in existing:
                conn.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR(128)"))
            conn.commit()
        # external_api_events is created by create_all; no ALTER TABLE needed


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a session and closes it on response end."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """For use outside FastAPI (worker, scripts). Commits or rolls back automatically."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
